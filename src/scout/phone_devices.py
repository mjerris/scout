"""Phones paired with Scout, and the check each call from one must pass.

A paired phone holds a device id and a 32-byte secret it got once, when it paired
(phone_pairing.py: a one-time code from a QR code, or a yes in the room). Each call it makes carries, in its userVariables,

    {device, ts, nonce, sig}   sig = hex(HMAC-SHA256(key, f"{device}|{ts}|{nonce}|{address}"))

where key is the secret (base64url-decoded to its 32 bytes), ts is Unix seconds, nonce is
fresh per call, and address is the agent address the app dialed ([phone] address).

A phone has two keys (docs/siri-design.md): the pairing secret, which the app reads only
while the phone is unlocked, and a second one, `locked_secret`, which stays readable after
the first unlock so Siri can reach Scout with the phone locked. A request signed with the
second says so ({key: "locked"}). What each key may do is config ([phone] unlocked_key
and locked_key: ABILITIES).

The registry is state/phone_devices.json (mode 0600, this user only):
    {device_id: {name, secret, locked_secret?, paired_at, revoked, push?}}
where push is what the app registered for push (phone_push.py): {voip, alert, env, at},
its PushKit and APNs device tokens and the APNs environment its build is signed for.
Forgetting a device deletes its secret and marks it revoked, so a lost phone stops
working at once. Manage it with `scoutctl phone pair | list | forget <device>` (pair
shows a one-time pairing code; `pair list`, `pair approve <id>` and `pair deny <id>`
answer a device asking to pair).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA

PATH = DATA / "state" / "phone_devices.json"
_DEVICE = re.compile(r"[A-Za-z0-9_-]{4,64}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{8,128}")
KEYS = ("unlocked", "locked")
ABILITIES = ("call", "ask", "push", "pair", "ring")
DEFAULT_ABILITIES: dict[str, frozenset[str]] = {
    "unlocked": frozenset(ABILITIES),
    "locked": frozenset({"ask"}),
}
_lock = threading.Lock()


def write_private(path: Path, text: str) -> None:
    """Write a file only this user can read (0600), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def load_devices(path: Path | None = None) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((path or PATH).read_text())
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise ValueError(f"{path or PATH} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path or PATH} must hold a JSON object")
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save(devices: dict[str, dict[str, Any]], path: Path | None) -> None:
    write_private(path or PATH, json.dumps(devices, indent=1) + "\n")


def pair(name: str, path: Path | None = None) -> tuple[str, str]:
    """Add a device: (device_id, secret as base64url without padding)."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("give the phone a name, e.g. 'Mike iPhone'")
    device = "d" + secrets.token_hex(8)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    with _lock:
        devices = load_devices(path)
        devices[device] = {"name": name, "secret": secret, "paired_at": int(time.time()), "revoked": False}
        _save(devices, path)
    return device, secret


def forget(device: str, path: Path | None = None) -> bool:
    """Revoke a device and delete its secret. False if there's no such device."""
    with _lock:
        devices = load_devices(path)
        d = devices.get(device)
        if d is None:
            return False
        d.update(secret="", locked_secret="", revoked=True, revoked_at=int(time.time()))
        d.pop("push", None)  # a forgotten phone is never rung or asked again
        _save(devices, path)
    return True


def new_locked_key(device: str, path: Path | None = None) -> str:
    """Give a paired phone a new second key (the old one stops working): its secret."""
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    with _lock:
        devices = load_devices(path)
        d = devices.get(device)
        if d is None or d.get("revoked") or not d.get("secret"):
            raise ValueError("unknown device")
        d["locked_secret"] = secret
        _save(devices, path)
    return secret


def abilities(unlocked: list[str], locked: list[str]) -> dict[str, frozenset[str]]:
    """[phone] unlocked_key and locked_key as CallCheck takes them. ValueError names an
    ability that isn't one."""
    out = {}
    for key, names in (("unlocked", unlocked), ("locked", locked)):
        wrong = [n for n in names if n not in ABILITIES]
        if wrong:
            raise ValueError(f"[phone] {key}_key: {', '.join(wrong)} isn't one of {', '.join(ABILITIES)}")
        out[key] = frozenset(names)
    return out


_TOKEN = re.compile(r"[0-9a-fA-F]{32,200}")
PUSH_ENVS = ("sandbox", "production")


def set_push(device: str, voip: str, alert: str, env: str, path: Path | None = None) -> None:
    """Keep the push tokens a paired app registered (either may be empty: not granted, or
    a build without push). ValueError for a malformed one or an unknown device."""
    for what, tok in (("voip", voip), ("alert", alert)):
        if tok and not _TOKEN.fullmatch(tok):
            raise ValueError(f"{what} must be a hex APNs token")
    if env not in PUSH_ENVS:
        raise ValueError(f"env must be one of {', '.join(PUSH_ENVS)}")
    with _lock:
        devices = load_devices(path)
        d = devices.get(device)
        if d is None or d.get("revoked"):
            raise ValueError("unknown device")
        if voip or alert:
            d["push"] = {"voip": voip.lower(), "alert": alert.lower(), "env": env, "at": int(time.time())}
        else:
            d.pop("push", None)
        _save(devices, path)


def drop_push_token(token: str, path: Path | None = None) -> bool:
    """APNs said this token is no longer valid (the app was removed, or the token
    rotated): forget it wherever it is. True if one was dropped."""
    token = token.lower()
    dropped = False
    with _lock:
        devices = load_devices(path)
        for d in devices.values():
            p = d.get("push")
            if not isinstance(p, dict):
                continue
            for kind in ("voip", "alert"):
                if p.get(kind) == token:
                    p[kind] = ""
                    dropped = True
            if not p.get("voip") and not p.get("alert"):
                d.pop("push", None)
        if dropped:
            _save(devices, path)
    return dropped


@dataclass(frozen=True)
class PushTarget:
    device: str
    name: str
    voip: str
    alert: str
    env: str


def push_targets(path: Path | None = None) -> list[PushTarget]:
    """The paired, unrevoked devices that registered for push."""
    out = []
    for k, v in load_devices(path).items():
        p = v.get("push")
        if v.get("revoked") or not v.get("secret") or not isinstance(p, dict):
            continue
        env = str(p["env"]) if p.get("env") in PUSH_ENVS else "production"
        out.append(
            PushTarget(k, str(v.get("name", "")), str(p.get("voip") or ""), str(p.get("alert") or ""), env)
        )
    return out


def listing(path: Path | None = None) -> list[dict[str, Any]]:
    """The devices without their secrets."""
    return [
        {
            "device": k,
            "name": v.get("name", ""),
            "paired_at": v.get("paired_at"),
            "revoked": bool(v.get("revoked")),
            "locked_key": bool(v.get("locked_secret")),
            "push": sorted(k for k in ("voip", "alert") if (v.get("push") or {}).get(k)),
        }
        for k, v in load_devices(path).items()
    ]


def _key(secret: str) -> bytes:
    return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))


def sign(secret: str, device: str, ts: int, nonce: str, address: str) -> str:
    """The signature a paired app sends (the app's side; tests use it too)."""
    msg = f"{device}|{ts}|{nonce}|{address}".encode()
    return hmac.new(_key(secret), msg, hashlib.sha256).hexdigest()


@dataclass
class Verdict:
    ok: bool
    device: str = ""
    reason: str = ""  # why not, for the log (never the secret)
    key: str = "unlocked"  # which of the phone's keys signed it


class CallCheck:
    """Checks the userVariables a call arrives with. Nonces are remembered in memory for
    twice the allowed clock skew (a nonce older than that fails on its timestamp anyway)."""

    def __init__(
        self,
        address: str,
        skew_s: float,
        path: Path | None = None,
        nonces: Path | None = None,
        abilities: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.address = address
        self.abilities = abilities or DEFAULT_ABILITIES
        self.skew_s = skew_s
        self.path = path
        self.nonces = nonces  # kept on disk, so a restart can't reopen a replay window
        self._seen: dict[str, float] = {}  # nonce -> forget after (wall clock)
        self._lock = threading.Lock()
        if nonces is not None:
            try:
                data = json.loads(nonces.read_text())
                self._seen = {str(n): float(u) for n, u in data.items() if float(u) > time.time()}
            except (OSError, ValueError, AttributeError):
                pass

    def device_ok(self, device: str) -> bool:
        """Still paired and not revoked (read fresh: forgetting a phone works at once)."""
        try:
            d = load_devices(self.path).get(device)
        except ValueError:
            return False
        return bool(d and not d.get("revoked") and d.get("secret"))

    def check(
        self, uv: dict[str, Any], now: float | None = None, address: str | None = None, need: str = "call"
    ) -> Verdict:
        """`address`: what this request must be signed over, in place of `self.address`
        (a purpose naming exactly what it does, e.g. one pairing request and its answer).
        `need`: the ability (ABILITIES) the key that signed it must have; "" for none, only
        for what the unlocked key alone may do (Verdict.key says which signed)."""
        now = time.time() if now is None else now
        key = uv.get("key") or "unlocked"
        if key not in KEYS:
            return Verdict(False, reason="no such key")
        device, ts, nonce, sig = (uv.get(k) for k in ("device", "ts", "nonce", "sig"))
        if not isinstance(device, str) or not _DEVICE.fullmatch(device):
            return Verdict(False, reason="no device id")
        if isinstance(ts, bool) or not isinstance(ts, int | str) or not str(ts).isdigit():
            return Verdict(False, device, "no timestamp")
        t = int(ts)
        if abs(now - t) > self.skew_s:
            return Verdict(False, device, f"timestamp {now - t:+.0f} s off")
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return Verdict(False, device, "no nonce")
        if not isinstance(sig, str) or not sig:
            return Verdict(False, device, "no signature")
        try:
            d = load_devices(self.path).get(device)
        except ValueError as exc:
            return Verdict(False, device, str(exc))
        if d is None:
            return Verdict(False, device, "unknown device")
        if d.get("revoked") or not d.get("secret"):
            return Verdict(False, device, "device revoked")
        secret = d.get("secret" if key == "unlocked" else "locked_secret")
        if not isinstance(secret, str) or not secret:
            return Verdict(False, device, f"no {key} key")
        if need and need not in self.abilities.get(key, frozenset()):
            return Verdict(False, device, f"the {key} key may not {need}", key)
        try:
            want = sign(secret, device, t, nonce, self.address if address is None else address)
        except (binascii.Error, ValueError):
            return Verdict(False, device, "device secret unreadable")
        if not hmac.compare_digest(want.encode(), sig.strip().lower().encode()):
            return Verdict(False, device, "bad signature")
        wall = time.time()
        with self._lock:
            for n in [n for n, until in self._seen.items() if until < wall]:
                del self._seen[n]
            if nonce in self._seen:
                return Verdict(False, device, "nonce replayed")
            self._seen[nonce] = wall + 2 * self.skew_s
            if self.nonces is not None:
                write_private(self.nonces, json.dumps(self._seen))
        return Verdict(True, device, key=key)


# --- scoutctl phone ...  ----------------------------------------------------------------


def _pair_code() -> int:
    """Mint a one-time pairing code and show it as a scout://pair link and QR code."""
    from . import links, phone_pairing

    server = links.web_base(refresh=True)
    code, expires_at = phone_pairing.mint()
    url = phone_pairing.link(server, code)
    if not server.startswith("https://"):
        print(
            f"Scout's address here is {server or 'unknown'}, but the app needs its https Tailscale name:"
            " turn on Tailscale Serve (scripts/tailscale.sh enable) or set [web] public_url.",
            file=sys.stderr,
        )
    print("Scan this with the iPhone Camera and tap the Scout banner, or type the code in the app.")
    phone_pairing.print_qr(url)
    print(url)
    until = time.strftime("%H:%M", time.localtime(expires_at))
    print(f"Code {code} for {server}: works once, until {until}.")
    return 0


def _pair_api(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST to the running app's web server on this Mac, with its web token (the CLI is the
    Mac's owner: it can read the token). (0, {error}) when the app isn't answering."""
    import urllib.error
    import urllib.request

    from . import web
    from .config import load

    try:
        token = web.TOKEN_PATH.read_text().strip()
    except OSError:
        return 0, {"error": "Scout's web token isn't there yet (is the app set up?)"}
    port = load().web.port
    req = urllib.request.Request(  # a fixed http://127.0.0.1 URL
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return int(r.status), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return 0, {"error": f"Scout's app isn't answering on port {port} ({exc})"}


def _describe(r: dict[str, Any]) -> str:
    left = max(0, int(float(r.get("expires_at") or 0) - time.time()))
    who = r.get("login") or "no verified Tailscale login"
    if r.get("owner"):
        who += " (yours)"
    bits = [f"{r.get('id')}  {r.get('name') or '(no usable name)'}  code {r.get('match')}  {who}"]
    bits += [f"{k} {r[k]}" for k in ("peer", "ip", "agent") if r.get(k)]
    when = time.strftime("%H:%M:%S", time.localtime(float(r.get("at") or 0)))
    return "  ".join(bits) + f"  asked {when}, {left} s left"


def _pair_requests(args: list[str]) -> int:
    """scoutctl phone pair list | approve <id> | deny <id>: answer a device asking to pair."""
    if args[0] == "list":
        status, got = _pair_api("/api/phone/pair/pending", {})
        if status != 200:
            print(got.get("error") or f"HTTP {status}", file=sys.stderr)
            return 1
        reqs = got.get("requests") or []
        for r in reqs:
            print(_describe(r))
        if not reqs:
            print("No device is asking to pair.")
        return 0
    approve = args[0] == "approve"
    status, got = _pair_api("/api/phone/pair/decide", {"id": args[1].strip().upper(), "approve": approve})
    verdict = got.get("status")
    if status == 200 and verdict == "approved":
        print(f"Approved: paired as {got.get('device')}.")
        return 0
    if status == 200 and verdict == "denied":
        print("Denied.")
        return 0
    if verdict == "already":
        print(f"Already answered ({got.get('result')}{' by ' + got['via'] if got.get('via') else ''}).")
        return 1
    if verdict == "unknown":
        print(f"No pairing request {args[1]!r} (see scoutctl phone pair list).", file=sys.stderr)
        return 1
    print(got.get("error") or f"HTTP {status}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    if cmd == "pair" and len(args) == 1:
        return _pair_code()
    if cmd == "pair" and (args[1:] == ["list"] or (len(args) == 3 and args[1] in ("approve", "deny"))):
        return _pair_requests(args[1:])
    if cmd == "list" and len(args) == 1:
        for d in listing():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["paired_at"] or 0))
            push = f"  push: {', '.join(d['push'])}" if d["push"] else ""
            print(f"{d['device']}  {d['name']}  paired {when}{push}{'  REVOKED' if d['revoked'] else ''}")
        return 0
    if cmd == "forget" and len(args) == 2:
        if forget(args[1]):
            print(f"forgot {args[1]}: its calls are refused from now on")
            return 0
        print(f"no paired device {args[1]!r} (see scoutctl phone list)", file=sys.stderr)
        return 1
    if cmd == "calls" and len(args) == 1:  # the calls going on now (stale ones dropped)
        from .phone_service import active_calls

        calls = active_calls(PATH.parent / "phone_calls.json")
        for cid, device in calls.items():
            print(f"{cid}  device {device}")
        print(f"{len(calls)} active call{'s' * (len(calls) != 1)}")
        return 0
    if cmd == "ring":  # ring the phone now (push must be set up), for trying it on a device
        status, got = _pair_api("/api/phone/ring", {"text": " ".join(args[1:])})
        print(
            "Ringing the phone."
            if status == 200
            else got.get("detail") or got.get("error") or f"HTTP {status}"
        )
        return 0 if status == 200 else 1
    if cmd == "rings" and len(args) == 1:  # Scout's recent calls to the phone and how they went
        from .phone_ring import Rings

        for r in Rings().recent():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r.get("at") or 0)))
            print(
                f"{when}  {r.get('who')}  {r.get('status')}{' by ' + r['by'] if r.get('by') else ''}  {r.get('text', '')[:60]}"
            )
        return 0
    print(
        "usage: scoutctl phone pair | pair list | pair approve <id> | pair deny <id> | list | forget <device>"
        " | calls | ring [text] | rings",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
