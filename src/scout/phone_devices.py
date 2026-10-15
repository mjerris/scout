"""Phones paired with Scout, and the check each call from one must pass.

A paired phone holds a device id and a 32-byte secret it got once, when it paired
(phone_pairing.py: a one-time code from a QR code, or a yes in the room). Each call it makes carries, in its userVariables,

    {device, ts, nonce, sig}   sig = hex(HMAC-SHA256(key, f"{device}|{ts}|{nonce}|{address}"))

where key is the secret (base64url-decoded to its 32 bytes), ts is Unix seconds, nonce is
fresh per call, and address is the agent address the app dialed ([phone] address).

The registry is state/phone_devices.json (mode 0600, this user only):
    {device_id: {name, secret, paired_at, revoked}}
Forgetting a device deletes its secret and marks it revoked, so a lost phone stops
working at once. Manage it with `scoutctl phone pair | list | forget <device>` (pair
shows a one-time pairing code).
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
        d.update(secret="", revoked=True, revoked_at=int(time.time()))
        _save(devices, path)
    return True


def listing(path: Path | None = None) -> list[dict[str, Any]]:
    """The devices without their secrets."""
    return [
        {
            "device": k,
            "name": v.get("name", ""),
            "paired_at": v.get("paired_at"),
            "revoked": bool(v.get("revoked")),
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


class CallCheck:
    """Checks the userVariables a call arrives with. Nonces are remembered in memory for
    twice the allowed clock skew (a nonce older than that fails on its timestamp anyway)."""

    def __init__(
        self, address: str, skew_s: float, path: Path | None = None, nonces: Path | None = None
    ) -> None:
        self.address = address
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

    def check(self, uv: dict[str, Any], now: float | None = None) -> Verdict:
        now = time.time() if now is None else now
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
        secret = d.get("secret")
        if d.get("revoked") or not isinstance(secret, str) or not secret:
            return Verdict(False, device, "device revoked")
        try:
            want = sign(secret, device, t, nonce, self.address)
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
        return Verdict(True, device)


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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else ""
    if cmd == "pair" and len(args) == 1:
        return _pair_code()
    if cmd == "list" and len(args) == 1:
        for d in listing():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["paired_at"] or 0))
            print(f"{d['device']}  {d['name']}  paired {when}{'  REVOKED' if d['revoked'] else ''}")
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
    print("usage: scoutctl phone pair | list | forget <device> | calls", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
