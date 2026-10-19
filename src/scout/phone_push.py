"""Push to the phone app through Apple's push service (APNs), with token auth.

Two kinds, both from the app's own Apple team and its one APNs auth key (.p8):

- VoIP (PushKit, topic "<bundle id>.voip"): Scout ringing the phone (phone_ring.py). iOS
  wakes the app, whose native hook reports a CallKit call before any JavaScript runs; a
  VoIP push must always become a call, so nothing else may use it.
- Alert (topic "<bundle id>"): a notification. Pairing requests go this way, with Approve
  and Deny actions (category SCOUT_PAIR); the app answers through the same signed
  /api/phone/pair/decide as its in-app screen.

The provider token is a JWT (ES256, kid = key id, iss = team id), made from the key and
reused for 30 minutes (Apple refuses one older than an hour, or remade more often than
every 20). A device token APNs calls invalid (410 Unregistered, BadDeviceToken) is dropped
from the device (phone_devices.drop_push_token). A sandbox token only works against the
sandbox host, so each device's token is sent to its own environment.

The key never leaves this Mac and is never logged: it is read from [phone] apns_key_file,
which must be mode 600 (no group or other access).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from . import phone_devices
from .config import DATA, PhoneConfig

log = logging.getLogger("scout.push")

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
JWT_REUSE_S = 30 * 60
SEND_TIMEOUT_S = 10.0
PAIR_CATEGORY = "SCOUT_PAIR"
# APNs' reasons that mean the token itself is no good (not a problem with this push).
_DEAD = {"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic", "ExpiredToken"}


class PushNotConfigured(Exception):
    """Why push is off, for the log and the API (never a secret)."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def key_path(cfg: PhoneConfig) -> Path:
    p = Path(cfg.apns_key_file).expanduser()
    return p if p.is_absolute() else DATA / p


def problem(cfg: PhoneConfig) -> str | None:
    """What's missing for push, or None when it's set up (the key file is checked too)."""
    missing = [
        k
        for k in ("apns_team_id", "apns_key_id", "apns_key_file", "apns_topic")
        if not getattr(cfg, k).strip()
    ]
    if missing:
        return f"Push is off: set [phone] {', '.join(missing)} in Scout's config."
    path = key_path(cfg)
    try:
        st = path.stat()
    except OSError:
        return f"Push is off: the APNs key file {path} isn't there."
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"Push is off: the APNs key file {path} can be read by others; chmod 600 it."
    return None


@dataclass
class Signer:
    """The provider token (JWT, ES256) from the APNs auth key, reused for JWT_REUSE_S."""

    team_id: str
    key_id: str
    key: ec.EllipticCurvePrivateKey
    clock: Callable[[], float] = time.time
    _token: str = ""
    _at: float = -1e18

    @classmethod
    def from_config(cls, cfg: PhoneConfig) -> Signer:
        if (why := problem(cfg)) is not None:
            raise PushNotConfigured(why)
        try:
            key = serialization.load_pem_private_key(key_path(cfg).read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise PushNotConfigured(
                f"Push is off: the APNs key file can't be read ({type(exc).__name__})."
            ) from None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise PushNotConfigured("Push is off: the APNs key file isn't an APNs auth key (P-256).")
        return cls(cfg.apns_team_id.strip(), cfg.apns_key_id.strip(), key)

    def token(self) -> str:
        now = self.clock()
        if self._token and now - self._at < JWT_REUSE_S:
            return self._token
        header = _b64(json.dumps({"alg": "ES256", "kid": self.key_id}, separators=(",", ":")).encode())
        claims = _b64(json.dumps({"iss": self.team_id, "iat": int(now)}, separators=(",", ":")).encode())
        der = self.key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        self._token, self._at = f"{header}.{claims}.{sig}", now
        return self._token


@dataclass
class Push:
    """One push to one device token."""

    token: str
    env: str  # "sandbox" | "production"
    kind: str  # "voip" | "alert"
    payload: dict[str, Any]
    expires_at: int = 0  # Unix seconds; 0: now or never
    collapse_id: str = ""
    device: str = ""  # for the log


@dataclass
class Result:
    push: Push
    status: int  # HTTP status; 0 when APNs wasn't reached
    reason: str = ""
    apns_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def dead(self) -> bool:
        """The device token is no good any more (drop it)."""
        return self.status == 410 or (self.status == 400 and self.reason in _DEAD)


Send = Callable[[list[Push]], Awaitable[list[Result]]]


@dataclass
class ApnsSender:
    """Sends pushes over HTTP/2 (APNs requires it). A connection per batch: pushes are
    rare, and a kept-open connection dies quietly behind NAT while idle."""

    signer: Signer
    topic: str
    transport: httpx.AsyncBaseTransport | None = None  # tests pass httpx.MockTransport
    devices: Path | None = None
    sent: list[Result] = field(default_factory=list)

    def headers(self, p: Push) -> dict[str, str]:
        voip = p.kind == "voip"
        h = {
            "authorization": f"bearer {self.signer.token()}",
            "apns-topic": f"{self.topic}.voip" if voip else self.topic,
            "apns-push-type": "voip" if voip else "alert",
            "apns-priority": "10",
            "apns-expiration": str(p.expires_at),
            "apns-id": str(uuid.uuid4()),
        }
        if p.collapse_id:
            h["apns-collapse-id"] = p.collapse_id
        return h

    async def __call__(self, pushes: list[Push]) -> list[Result]:
        if not pushes:
            return []
        kw: dict[str, Any] = {"timeout": SEND_TIMEOUT_S, "trust_env": False}
        if self.transport is not None:
            kw["transport"] = self.transport
        else:
            kw["http2"] = True
        async with httpx.AsyncClient(**kw) as client:
            results = await asyncio.gather(*(self._one(client, p) for p in pushes))
        for r in results:
            if r.ok:
                log.info("push: %s push to %s accepted", r.push.kind, r.push.device or "a device")
            else:
                log.warning("push: %s push to %s refused: %s %s", r.push.kind, r.push.device or "a device",
                            r.status or "unreachable", r.reason)  # fmt: skip
            if r.dead:
                await asyncio.to_thread(phone_devices.drop_push_token, r.push.token, self.devices)
        return list(results)

    async def _one(self, client: httpx.AsyncClient, p: Push) -> Result:
        url = f"{HOSTS.get(p.env, HOSTS['production'])}/3/device/{p.token}"
        headers = self.headers(p)
        try:
            r = await client.post(url, content=json.dumps(p.payload, separators=(",", ":")), headers=headers)
        except httpx.HTTPError as exc:
            return Result(p, 0, type(exc).__name__)
        reason = ""
        if r.status_code != 200:
            try:
                reason = str(r.json().get("reason", ""))
            except ValueError:
                reason = r.text[:100]
        return Result(p, r.status_code, reason, r.headers.get("apns-id", headers["apns-id"]))


def sender(cfg: PhoneConfig, devices: Path | None = None) -> ApnsSender | None:
    """The configured sender, or None (logged once, by the caller) when push is off."""
    try:
        return ApnsSender(Signer.from_config(cfg), cfg.apns_topic.strip(), devices=devices)
    except PushNotConfigured as exc:
        log.info("%s", exc)
        return None


# --- what is sent -----------------------------------------------------------------------


def ring_payload(token: str, call_uuid: str) -> dict[str, Any]:
    """A VoIP push that rings the app: the wrapper's PushKit hook reports a CallKit call
    named Scout, and passes `scoutRing` (single-use, opaque) to JavaScript as the entry's
    data. `call_id` is the same token: the call the app then places is joined to this
    entry by the app (bindBridgeCall), not by an incoming call id."""
    return {"call_id": token, "uuid": call_uuid, "from": "Scout", "from_name": "Scout", "scoutRing": token}


def pair_payload(view: dict[str, Any]) -> dict[str, Any]:
    """A notification for a device asking to pair, with Approve / Deny (category
    SCOUT_PAIR). The name is the requester's own (already cleaned by phone_pairing) and
    always follows a fixed prefix; the app shows the rest (login, device) when opened."""
    name = str(view.get("name") or "")
    lead = f'A new device calling itself "{name}"' if name else "A new device with no usable name"
    return {
        "aps": {
            "alert": {
                "title": "Pairing request",
                "body": f"{lead} wants to pair with Scout. It shows the code {view.get('match', '')}.",
            },
            "category": PAIR_CATEGORY,
            "sound": "default",
            "interruption-level": "time-sensitive",
        },
        "scoutPair": {"id": str(view.get("id", "")), "match": str(view.get("match", ""))},
    }


def ring_pushes(targets: Iterable[phone_devices.PushTarget], token: str, expires_at: int) -> list[Push]:
    """One VoIP push per device that registered a PushKit token; one CallKit uuid each."""
    return [
        Push(
            t.voip, t.env, "voip", ring_payload(token, str(uuid.uuid4()).upper()), expires_at, device=t.device
        )
        for t in targets
        if t.voip
    ]


def pair_pushes(targets: Iterable[phone_devices.PushTarget], view: dict[str, Any]) -> list[Push]:
    expires = int(float(view.get("expires_at") or 0))
    payload = pair_payload(view)
    rid = str(view.get("id", ""))
    return [
        Push(t.alert, t.env, "alert", payload, expires, collapse_id=f"pair-{rid}", device=t.device)
        for t in targets
        if t.alert
    ]
