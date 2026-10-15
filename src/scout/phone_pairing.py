"""Pairing the phone app without typing Scout's web token. Three ways, all ending in the
same device id and secret (phone_devices.pair):

1. **A one-time code**: `scoutctl phone pair` or the /phone page mints one (10 minutes,
   single use) and shows it as a scout://pair?server=<https address>&code=<code> link
   and QR code; the iPhone Camera opens the link in the app, which sends the code to
   `POST /api/phone/pair` without the web token. Only a SHA-256 of the code is kept
   (state/phone_pair_codes.json, 0600), and wrong codes are rate-limited (`Limiter`).
2. **Found on the local network**: Scout advertises `_scout._tcp` over Bonjour with its
   https address in the TXT record (`Advertiser`, a managed `dns-sd -R` child); the app
   offers "Found <host>", then asks as in 3.
3. **Approved by voice**: the app asks to pair (`POST /api/phone/pair/request`, tailnet
   only); Scout asks the room "<name> wants to pair with Scout, code 47. Should I allow
   it?" with the same two-digit number on the phone and on the web UI. A yes pairs it;
   the app picks the secret up once (`POST /api/phone/pair/status`).

The web token is never in a code, a link, a QR code or the Bonjour record. A phone
paired this way doesn't hold the web token at all: it signs its SignalWire token
requests with its device secret (`TOKEN_PURPOSE`).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import DATA
from .phone_devices import write_private

log = logging.getLogger(__name__)

PATH = DATA / "state" / "phone_pair_codes.json"
TTL_S = 600.0  # a code lasts 10 minutes
KEEP_S = 86400.0  # a used or expired code is remembered this long (to say which it was)
# Unambiguous on a screen and when typed: no 0/O, 1/I/L.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
# What a phone signs, in place of the agent address, to ask for a SignalWire token: never
# a valid agent address, so a token request's signature can't be replayed as a call's.
TOKEN_PURPOSE = "scout:phone-token"  # noqa: S105 (a label, not a secret)
SERVICE = "_scout._tcp"


# --- one-time codes -------------------------------------------------------------------------


def normalize(code: str) -> str:
    """ABCD-EFGH, abcdefgh, ' abcd efgh ' all read as ABCDEFGH."""
    return "".join(c for c in code.upper() if c.isalnum())[:64]


def _hash(code: str) -> str:
    return hashlib.sha256(normalize(code).encode()).hexdigest()


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[dict[str, dict[str, Any]]]:
    """The codes file, under an exclusive lock (scoutctl mints in its own process while
    the web server spends them); written back on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            codes = (
                {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
            )
        except (FileNotFoundError, ValueError):
            codes = {}
        yield codes
        write_private(path, json.dumps(codes) + "\n")


def mint(path: Path | None = None, now: float | None = None) -> tuple[str, float]:
    """A new code, shown as ABCD-EFGH, and when it expires. It replaces any unused one,
    so only the newest QR code works."""
    now = time.time() if now is None else now
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    expires_at = now + TTL_S
    with _locked(path or PATH) as codes:
        for h in list(codes):
            c = codes[h]
            if not c.get("used_at") and float(c.get("expires_at", 0)) > now:
                del codes[h]  # replaced
            elif float(c.get("expires_at", 0)) + KEEP_S < now:
                del codes[h]
        codes[_hash(raw)] = {"expires_at": expires_at, "used_at": None}
    return f"{raw[:4]}-{raw[4:]}", expires_at


def consume(code: str, path: Path | None = None, now: float | None = None) -> str:
    """Spend a code: "ok" once, then "used"; "expired" past its 10 minutes; "unknown"
    for anything else (a typo, or a code a newer one replaced)."""
    now = time.time() if now is None else now
    if len(normalize(code)) != CODE_LEN:
        return "unknown"
    with _locked(path or PATH) as codes:
        c = codes.get(_hash(code))
        if c is None:
            return "unknown"
        if c.get("used_at"):
            return "used"
        if float(c.get("expires_at", 0)) <= now:
            return "expired"
        c["used_at"] = now
    return "ok"


def link(server: str, code: str) -> str:
    """scout://pair?server=<Scout's https address>&code=<code>: what the QR code carries."""
    return f"scout://pair?server={quote(server, safe='')}&code={quote(code, safe='')}"


class Limiter:
    """At most `limit` failures in any `window_s`; past that, everything is refused until
    the oldest failure leaves the window."""

    def __init__(self, limit: int = 5, window_s: float = 60.0) -> None:
        self.limit, self.window_s = limit, window_s
        self._at: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._at and self._at[0] <= now - self.window_s:
            self._at.popleft()

    def allowed(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._trim(now)
        return len(self._at) < self.limit

    def failed(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._trim(now)
        self._at.append(now)


def qr_svg_data_uri(text: str) -> str:
    """The QR code for `text`, as an SVG data: URI the page shows (made here: no CDN)."""
    import segno

    return str(segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#000", light="#fff"))


def print_qr(text: str) -> None:
    """The QR code for `text`, drawn in the terminal."""
    import segno

    segno.make(text, error="m").terminal(compact=True)


# --- approve by voice -----------------------------------------------------------------------


class PairRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Request:
    name: str
    match: str
    expires_at: float
    result: dict[str, Any] | None = None  # {"status": ...} once decided
    done: asyncio.Event = field(default_factory=asyncio.Event)


class PairRequests:
    """Phones asking to pair, approved by the room. One at a time; `limit` requests per
    `window_s`. `ask(name, match)` asks the room (True: yes; False: no; None: no answer);
    `pair(name)` adds the device. A decided request is kept until its secret is picked up
    (once) or a little past its expiry."""

    def __init__(
        self,
        ask: Callable[[str, str], Awaitable[bool | None]],
        pair: Callable[[str], tuple[str, str]],
        ttl_s: float = 120.0,
        limit: int = 3,
        window_s: float = 600.0,
    ) -> None:
        self.ask, self.pair, self.ttl_s = ask, pair, ttl_s
        self.limiter = Limiter(limit, window_s)
        self._requests: dict[str, _Request] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, r in self._requests.items() if r.expires_at + self.ttl_s < now]:
            del self._requests[k]

    def start(self, name: str) -> dict[str, Any]:
        self._sweep()
        if any(r.result is None for r in self._requests.values()):
            raise PairRefused(409, "Another phone is asking to pair right now; try again in a minute or two.")
        if not self.limiter.allowed():
            raise PairRefused(429, "Too many pairing requests; wait a few minutes and try again.")
        self.limiter.failed()  # every request counts toward the limit
        rid = secrets.token_urlsafe(32)
        match = str(10 + secrets.randbelow(90))
        req = _Request(name, match, time.monotonic() + self.ttl_s)
        self._requests[rid] = req
        task = asyncio.get_running_loop().create_task(self._decide(req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("phone: %r asks to pair (match %s)", name, match)
        return {"request": rid, "match": match, "expires_at": time.time() + self.ttl_s}

    async def _decide(self, req: _Request) -> None:
        result: dict[str, Any]
        try:
            answer = await asyncio.wait_for(self.ask(req.name, req.match), self.ttl_s)
        except TimeoutError:
            answer = None
        except Exception:
            log.exception("phone: asking the room about a pairing failed")
            answer = None
        if answer is True:
            try:
                device, secret = await asyncio.to_thread(self.pair, req.name)
                result = {"status": "approved", "device": device, "secret": secret}
                log.info("phone: paired %s (%s), approved in the room", device, req.name)
            except (OSError, ValueError) as exc:
                log.warning("phone: pairing %r failed: %s", req.name, exc)
                result = {"status": "error", "error": f"Couldn't pair: {exc}"}
        else:
            result = {"status": "denied" if answer is False else "expired"}
            log.info("phone: pairing %r %s", req.name, result["status"])
        req.result = result
        req.done.set()

    async def status(self, request: str, wait: float = 0.0) -> dict[str, Any]:
        """Where a request stands; waits up to `wait` seconds for the room's answer. The
        secret is handed out once."""
        req = self._requests.get(request) if request else None
        if req is None:
            return {"status": "unknown"}
        if req.result is None and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(req.done.wait(), wait)
        if req.result is None:
            return {"status": "waiting", "match": req.match}
        if req.result["status"] == "approved":
            self._requests.pop(request, None)
        return dict(req.result)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# --- auto-find: Bonjour ---------------------------------------------------------------------


def bonjour_args(url: str, port: int, label: str) -> list[str] | None:
    """`dns-sd` arguments that advertise Scout as _scout._tcp with its https address in
    the TXT record. None when there's no https address to offer (the app only talks
    https, through Tailscale)."""
    if not url.startswith("https://"):
        return None
    return ["-R", f"Scout on {label}", SERVICE, "local", str(port), f"url={url}", f"host={label}"]


class Advertiser:
    """One `dns-sd -R` child (macOS's own mDNSResponder does the advertising), following
    Scout's address: restarted when it changes, stopped when there's no https one."""

    def __init__(self, program: str, port: int, label: str) -> None:
        self.program, self.port, self.label = program, port, label
        self._proc: asyncio.subprocess.Process | None = None
        self._args: list[str] | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None and self._proc.returncode is None else None

    async def update(self, url: str) -> None:
        args = bonjour_args(url, self.port, self.label)
        if args == self._args and (args is None or self.pid is not None):
            return
        await self._stop()
        self._args = args
        if args is None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.program,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("bonjour: advertising %s for %s", SERVICE, url)
        except OSError as exc:
            log.warning("bonjour: %s not started: %s", self.program, exc)
            self._proc = None

    async def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def close(self) -> None:
        self._args = None
        await self._stop()
