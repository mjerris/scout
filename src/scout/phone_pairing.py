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
3. **Approved on a device already trusted**: the app asks to pair
   (`POST /api/phone/pair/request`, tailnet only) and gets a two-digit match number. That
   one pending request (`PairRequests`) goes to every channel at once: the open web UI
   pages, the paired phones (their app shows it while open), the room by voice ("A new
   device calling itself <name> wants to pair with Scout, code 47. Should I allow it?"),
   an alert on the Mac, and `scoutctl phone pair list | approve <id> | deny <id>`. The
   first answer wins and the others are told; unanswered, it expires in two minutes. A
   yes pairs it; the app picks the secret up once (`POST /api/phone/pair/status`).
   Nothing pairs without an answer, even from the owner's own Tailscale login: a request's
   identity only labels the prompt (web.py explains why).

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
import re
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Coroutine, Iterator, Sequence
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


# --- approval: one pending request, answered from any channel ---------------------------------

NAME_MAX = 24  # what a requester may call itself in a prompt
_NAME_OK = re.compile(r"[A-Za-z0-9 '_()-]")
# Curly apostrophes and dashes, as typed on a phone, read as plain ones.
_QUOTES = str.maketrans(
    dict.fromkeys("\u2018\u2019\u201b\u02bc", "'") | dict.fromkeys("\u2010\u2011\u2012\u2013\u2014", "-")
)


def clean_name(raw: object, limit: int = NAME_MAX) -> str:
    """A requester's name as it may appear in a prompt: NFKC-normalized, plain ASCII
    letters, digits, spaces and ' _ ( ) - only (no full stops, quotes, commas, colons or
    other punctuation a sentence could be built from), single-spaced, at most `limit`
    characters. "" when nothing usable is left. Never read or shown raw: the requester
    chooses it, so it could otherwise be written to sound like an instruction."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKC", raw).translate(_QUOTES)
    text = "".join(c if _NAME_OK.fullmatch(c) else " " for c in text)
    return " ".join(text.split())[:limit].strip(" '-_(")


def clean_text(raw: object, limit: int = 80) -> str:
    """Other requester-supplied details (its user agent): printable ASCII, single-spaced, cut short."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(c if " " <= c <= "~" else " " for c in text)
    return " ".join(text.split())[:limit]


def spoken_prompt(name: str, match: str) -> str:
    """What the room hears: always the fixed prefix, never the requester's words first."""
    name = clean_name(name)
    who = f"A new device calling itself {name}" if name else "A new device with no usable name"
    return f"{who} wants to pair with Scout, code {match}. Should I allow it?"


class PairRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Request:
    id: str  # short and public: what the CLI, the web UI and the phones answer by
    token: str  # the requester's own handle for picking up its answer (never shown)
    name: str  # clean_name
    match: str
    who: dict[str, Any]  # login, owner, peer, ip, agent (all cleaned)
    at: float  # wall clock, when it was asked
    expires_at: float  # monotonic
    ttl_s: float
    result: dict[str, Any] | None = None  # {"status": ...} once decided
    via: str = ""  # which channel decided it
    deciding: bool = False  # a decision is being written (first one wins)
    picked_up: bool = False  # the requester has its secret
    done: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


# A channel that asks someone and waits (the room by voice, an alert on the Mac): gets
# the request's view; True approves, False denies, None means no answer from it (the
# request stays open for the others). Cancelled as soon as another channel answers.
Asker = Callable[[dict[str, Any]], Coroutine[Any, Any, bool | None]]
Listener = Callable[[str, dict[str, Any]], None]  # ("pair_request" | "pair_resolved", data)


class PairRequests:
    """Devices asking to pair. Each request is one pending record that every channel
    answers: the askers (`askers`: the room's spoken question, an alert on the Mac) are
    started at once, and the web UI, the paired phones and the CLI answer it by id
    (`decide`). The first answer wins; the others are told (`listeners`; askers are
    cancelled) and a later answer gets "already". Unanswered, it expires after `ttl_s`.

    One request at a time; `limit` requests per `window_s`. `pair(name)` adds the device;
    the requester picks its secret up once (`status`)."""

    def __init__(
        self,
        pair: Callable[[str], tuple[str, str]],
        askers: Sequence[tuple[str, Asker]] = (),
        ttl_s: float = 120.0,
        limit: int = 3,
        window_s: float = 600.0,
    ) -> None:
        self.pair, self.askers, self.ttl_s = pair, list(askers), ttl_s
        self.limiter = Limiter(limit, window_s)
        self.listeners: list[Listener] = []
        self.version = 0  # bumped on every change, for long polls (`changed`)
        self._changed = asyncio.Event()
        self._requests: dict[str, _Request] = {}  # by token
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- views ---

    @staticmethod
    def view(req: _Request) -> dict[str, Any]:
        """What a prompt shows: no token, nothing raw from the requester."""
        return {"id": req.id, "name": req.name, "match": req.match, "at": req.at,
                "expires_at": req.at + req.ttl_s, **req.who}  # fmt: skip

    def pending(self) -> list[dict[str, Any]]:
        self._sweep()
        return [self.view(r) for r in self._requests.values() if r.result is None]

    def _by_id(self, rid: str) -> _Request | None:
        return next((r for r in self._requests.values() if r.id == rid), None)

    def _notify(self, kind: str, data: dict[str, Any]) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()
        for fn in list(self.listeners):
            try:
                fn(kind, data)
            except Exception:
                log.exception("phone: a pairing listener failed")

    async def changed(self, since: int, wait: float) -> int:
        """Wait up to `wait` seconds for a change after version `since`; the version now."""
        if self.version == since and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), wait)
        return self.version

    def _sweep(self) -> None:
        now = time.monotonic()
        for k in [k for k, r in self._requests.items() if r.expires_at + self.ttl_s < now]:
            del self._requests[k]

    # --- asking ---

    def start(self, name: str, who: dict[str, Any] | None = None) -> dict[str, Any]:
        self._sweep()
        if any(r.result is None for r in self._requests.values()):
            raise PairRefused(
                409, "Another device is asking to pair right now; try again in a minute or two."
            )
        if not self.limiter.allowed():
            raise PairRefused(429, "Too many pairing requests; wait a few minutes and try again.")
        self.limiter.failed()  # every request counts toward the limit
        token = secrets.token_urlsafe(32)
        rid = "".join(secrets.choice(ALPHABET) for _ in range(6))
        match = str(10 + secrets.randbelow(90))
        w = who or {}
        details = {
            "login": clean_text(w.get("login"), 100),
            "owner": bool(w.get("owner")),
            "peer": clean_text(w.get("peer"), 100),
            "ip": clean_text(w.get("ip"), 64),
            "agent": clean_text(w.get("agent"), 80),
        }
        req = _Request(
            rid,
            token,
            clean_name(name),
            match,
            details,
            time.time(),
            time.monotonic() + self.ttl_s,
            self.ttl_s,
        )
        self._requests[token] = req
        loop = asyncio.get_running_loop()
        view = self.view(req)
        for via, ask in self.askers:
            self._spawn(req, loop.create_task(self._ask(req, via, ask, dict(view))))
        self._spawn(req, loop.create_task(self._expire(req)))
        log.info("phone: %r asks to pair (request %s, match %s, from %s)", req.name, rid, match,
                 details["login"] or details["ip"] or "unknown")  # fmt: skip
        self._notify("pair_request", view)
        return {"request": token, "match": match, "expires_at": time.time() + self.ttl_s}

    def _spawn(self, req: _Request, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        req.tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(req.tasks.discard)

    async def _ask(self, req: _Request, via: str, ask: Asker, view: dict[str, Any]) -> None:
        try:
            answer = await asyncio.wait_for(ask(view), self.ttl_s)
        except TimeoutError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("phone: asking about a pairing (%s) failed", via)
            return
        if answer is not None:
            await self.decide(req.id, bool(answer), via)

    async def _expire(self, req: _Request) -> None:
        await asyncio.sleep(max(0.0, req.expires_at - time.monotonic()))
        if req.result is None and not req.deciding:
            self._resolve(req, {"status": "expired"}, "")

    def _resolve(self, req: _Request, result: dict[str, Any], via: str) -> None:
        req.result, req.via = result, via
        req.done.set()
        me = asyncio.current_task()
        for t in list(req.tasks):
            if t is not me:
                t.cancel()  # the other channels stop asking
        log.info("phone: pairing request %s (%r) %s%s", req.id, req.name, result["status"],
                 f" by {via}" if via else "")  # fmt: skip
        self._notify(
            "pair_resolved", {"id": req.id, "status": result["status"], "via": via, "name": req.name}
        )

    async def decide(self, rid: str, approved: bool, via: str) -> dict[str, Any]:
        """Answer request `rid` from channel `via`. The first answer wins: {"status":
        "approved", "device"} or {"status": "denied"}; a later one gets {"status":
        "already", "result", "via"}; {"status": "unknown"} for no such request."""
        self._sweep()
        req = self._by_id(rid) if isinstance(rid, str) and rid else None
        if req is None:
            return {"status": "unknown"}
        if req.result is not None or req.deciding:
            got = req.result["status"] if req.result else "being answered"
            return {"status": "already", "result": got, "via": req.via}
        req.deciding = True  # synchronously: no other answer can get past this point
        if not approved:
            self._resolve(req, {"status": "denied"}, via)
            return {"status": "denied"}
        try:
            device, secret = await asyncio.to_thread(self.pair, req.name or "New device")
        except (OSError, ValueError) as exc:
            log.warning("phone: pairing %r failed: %s", req.name, exc)
            self._resolve(req, {"status": "error", "error": f"Couldn't pair: {exc}"}, via)
            return {"status": "error", "error": f"Couldn't pair: {exc}"}
        self._resolve(req, {"status": "approved", "device": device, "secret": secret}, via)
        log.info("phone: paired %s (%s), approved by %s", device, req.name, via)
        return {"status": "approved", "device": device}

    async def status(self, request: str, wait: float = 0.0) -> dict[str, Any]:
        """Where a request stands, for the requester (by its token); waits up to `wait`
        seconds for an answer. The secret is handed out once."""
        req = self._requests.get(request) if request else None
        if req is None or req.picked_up:
            return {"status": "unknown"}
        if req.result is None and wait > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(req.done.wait(), wait)
        if req.result is None:
            return {"status": "waiting", "match": req.match}
        got = dict(req.result)
        if got["status"] == "approved":  # the secret goes out once; the record stays, for "already"
            req.picked_up = True
            req.result = {k: v for k, v in got.items() if k != "secret"}
        return got

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


# --- the Mac's own alert (until the phone app has push notifications) -------------------------

OSASCRIPT = "/usr/bin/osascript"
_ALERT_SCRIPT = """on run argv
  set r to display dialog (item 1 of argv) with title "Scout: pair a new device?" \
buttons {"Deny", "Approve"} default button "Deny" cancel button "Deny" \
giving up after ((item 2 of argv) as integer) with icon caution
  if gave up of r then return "timeout"
  return button returned of r
end run"""


def alert_text(view: dict[str, Any]) -> str:
    """The alert's text: every requester-supplied part cleaned, and labelled."""
    name = clean_name(view.get("name"))
    lines = [
        f"A new device calling itself {name} wants to pair with Scout."
        if name else "A new device with no usable name wants to pair with Scout.",
        f"It shows the code {view.get('match', '')}.",
    ]  # fmt: skip
    login = clean_text(view.get("login"), 100)
    if login:
        lines.append(f"Tailscale login: {login}" + (" (yours)" if view.get("owner") else ""))
    else:
        lines.append("Tailscale login: not verified")
    for label, key in (("Device", "peer"), ("Address", "ip"), ("App", "agent")):
        if text := clean_text(view.get(key), 100):
            lines.append(f"{label}: {text}")
    lines.append(
        "Asked at " + time.strftime("%H:%M:%S", time.localtime(float(view.get("at") or time.time())))
    )
    return "\n".join(lines)


def mac_alert(program: str = OSASCRIPT, give_up_s: float = 115.0) -> Asker:
    """An asker that shows an alert on the Mac with Approve and Deny (osascript's
    display dialog; the text goes in as an argument, never into the script). Closed
    when another channel answers first."""

    async def ask(view: dict[str, Any]) -> bool | None:
        proc = await asyncio.create_subprocess_exec(
            program, "-e", _ALERT_SCRIPT, alert_text(view), str(int(give_up_s)),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        said = out.decode(errors="replace").strip()
        if proc.returncode == 0:
            return {"Approve": True, "Deny": False}.get(said)
        if b"-128" in err:  # the cancel button: Deny
            return False
        log.warning("phone: the pairing alert didn't show (%s)", err.decode(errors="replace").strip()[:200])
        return None

    return ask


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
