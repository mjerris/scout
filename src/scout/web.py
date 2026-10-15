"""LAN control page: live transcript, typed requests, approve/deny, stop, mute."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import ctypes.util
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Coroutine, Iterable
from importlib import resources
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSCloseCode, WSMsgType, web

from . import phone_devices, phone_pairing
from .audio import SAMPLE_RATE, decode_to_pcm
from .config import DATA, PhoneConfig, WebConfig

log = logging.getLogger(__name__)
_COOKIE = "cv_token"
TOKEN_PATH = DATA / "state" / "web_token"
# scout-phone's own credential (it makes the file): only a request carrying it may act for
# the phone (agent "phone", preview, confirmed), since confirmed skips the room's spoken yes.
PHONE_TOKEN_PATH = DATA / "state" / "phone_api_token"
PHONE_HEADER = "X-Scout-Phone"
PHONE_AGENT = "phone"  # phone_service.AGENT
_MIN_TOKEN_LEN = 16  # shorter (or empty) token files are replaced, never accepted

_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_OUTPUTS = ("device", "mini", "both")
MAX_CLIP_BYTES = 8_000_000  # a push-to-talk recording (minutes of compressed audio)
MAX_CLIP_SECONDS = 120.0  # decoded length limit, so one clip can't tie up Whisper
MAX_OUTBOX = 500  # events waiting for one slow page; past this it is disconnected and reloads
MAX_REQUESTS = 3  # typed lines / clips waiting per page while one is being handled
PENDING_SECONDS = 120.0  # reply audio kept for a page that is reconnecting
PENDING_MAX = 40
REFRESH_SECONDS = 1.0  # how often the floor/state shown on pages is rechecked
REBIND_SECONDS = 30.0  # how often the lan/tailscale addresses are rechecked
ADVERTISE_SECONDS = 60.0  # how often the Bonjour advertisement follows Scout's address
DNS_SD = "/usr/bin/dns-sd"  # macOS's own; it asks mDNSResponder to advertise
SW_TOKEN_TTL_S = 3600  # a subscriber token minted for the /phone test page lasts this long
SW_TIMEOUT_S = 15.0
_SPACE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
# The /phone page: its scripts come from here and the pinned @signalwire/js on jsDelivr;
# the SDK talks to SignalWire over https and wss.
_PHONE_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; connect-src 'self' https: wss:; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
)
# Offline reference pages (/ref/...) are an archive's own HTML on this origin: no script of
# theirs runs (it could call /api with the cookie), nothing is fetched, no form is sent.
_REFERENCE_CSP = (
    "default-src 'self'; script-src 'none'; connect-src 'none'; object-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'none'"
)


# --- what the web page needs from the assistant ---------------------------------------


class _Speaker(Protocol):
    async def synth_wav(self, text: str, voice: str | None = None) -> bytes: ...


class _Rules(Protocol):
    def listing(self) -> list[dict[str, Any]]: ...


class VoiceApp(Protocol):
    """The parts of Assistant the web server uses."""

    @property
    def speaker(self) -> _Speaker: ...

    @property
    def rules(self) -> _Rules: ...

    @property
    def history(self) -> Iterable[dict[str, Any]]: ...

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]: ...

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    def voice_status(self) -> dict[str, Any]: ...

    async def run_shared_tool(
        self, agent: str, name: str, args: dict[str, Any], preview: bool = False, confirmed: bool = False
    ) -> dict[str, Any]: ...

    async def discuss(
        self,
        agent: str,
        message: str | None,
        listen: bool = True,
        timeout: float = 30.0,
        hold: bool = False,
        voice: str | None = None,
        wait_for_floor: float = 0.0,
    ) -> dict[str, Any]: ...

    async def ask(self, agent: str, text: str, deadline: float = ..., via: str = ...) -> dict[str, Any]: ...

    async def submit_text(self, text: str, speak: bool = True, client: str | None = None) -> str | None: ...

    async def submit_audio(
        self, pcm: bytes, speak: bool = True, client: str | None = None
    ) -> dict[str, Any]: ...

    def answer_confirm(self, approved: bool | str, confirm_id: int | None = None) -> object: ...

    async def approve_pairing(self, name: str, match: str) -> bool | None: ...

    async def announce(self, text: str, who: str = "announce") -> None: ...

    def remove_rule(self, rule_id: str) -> object: ...

    async def stop(self) -> None: ...

    async def reset(self, speak: bool = True, client: str | None = None) -> None: ...

    def set_mic_muted(self, muted: bool) -> object: ...


# --- token ------------------------------------------------------------------------


def load_token(path: Path | None = None) -> str:
    """The page's access token, made on first run. An empty or short file is replaced."""
    path = path or TOKEN_PATH
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        token = ""
    if len(token) >= _MIN_TOKEN_LEN:
        return token
    if path.exists():
        log.warning("web: %s is empty or too short; making a new token", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never readable by others
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    tmp.replace(path)  # atomic: the file is never seen empty
    return token


def token_ok(given: str, token: str) -> bool:
    if not given or len(token) < _MIN_TOKEN_LEN:
        return False
    return hmac.compare_digest(given.encode(), token.encode())  # bytes: non-ASCII input can't raise


def _web_base() -> str:
    """Where the phone reaches Scout (links.web_base: [web] public_url, else the Tailscale
    Serve name)."""
    from . import links

    return links.web_base()


def phone_ok(req: web.Request) -> bool:
    """The request carries scout-phone's credential (read each time: scout-phone may
    make it after Scout starts). No file, no phone."""
    try:
        token = PHONE_TOKEN_PATH.read_text().strip()
    except OSError:
        return False
    return token_ok(req.headers.get(PHONE_HEADER, ""), token)


# --- SignalWire subscriber tokens (the /phone test page) ------------------------------


class PhoneTokenError(Exception):
    """Why no token was minted; status is the HTTP status the page gets."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def phone_token_request(cfg: PhoneConfig, now: float) -> tuple[str, dict[str, str], dict[str, Any]]:
    """The REST call that mints a subscriber token: POST {space}/api/fabric/subscribers/tokens
    with the project's Basic auth. SignalWire creates the subscriber (reference) on first use."""
    if not cfg.enabled:
        raise PhoneTokenError(409, "Phone calls are off: set [phone] enabled = true in Scout's config.")
    missing = [k for k in ("space", "project_id", "api_token", "subscriber") if not getattr(cfg, k).strip()]
    if missing:
        raise PhoneTokenError(409, f"Set [phone] {', '.join(missing)} in Scout's config.")
    space = cfg.space.strip()
    space = space.split("://", 1)[-1].split("/", 1)[0]
    if not _SPACE.fullmatch(space):
        raise PhoneTokenError(
            409, f"[phone] space {cfg.space!r} is not a host name like example.signalwire.com."
        )
    body = {"reference": cfg.subscriber.strip(), "expire_at": int(now) + SW_TOKEN_TTL_S}
    headers = {
        "Authorization": aiohttp.encode_basic_auth(cfg.project_id.strip(), cfg.api_token.strip()),
        "Accept": "application/json",
    }
    return f"https://{space}/api/fabric/subscribers/tokens", headers, body


async def _post_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    """POST JSON, return (status, parsed body or its text). No proxy from the environment."""
    timeout = aiohttp.ClientTimeout(total=SW_TIMEOUT_S)
    async with (
        aiohttp.ClientSession(timeout=timeout, trust_env=False) as s,
        s.post(url, json=body, headers=headers) as r,
    ):
        text = await r.text()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text


def _sw_error(data: Any) -> str:
    """SignalWire's error messages, short (never the request)."""
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        msgs = [
            str(e.get("detail") or e.get("message") or e.get("code") or "")
            for e in data["errors"]
            if isinstance(e, dict)
        ]
        if any(msgs):
            return "; ".join(m for m in msgs if m)[:300]
    if isinstance(data, dict):
        for k in ("error", "message"):
            if isinstance(data.get(k), str):
                return str(data[k])[:300]
    return str(data)[:200]


async def mint_subscriber_token(cfg: PhoneConfig, now: float | None = None) -> dict[str, Any]:
    """A short-lived SignalWire subscriber token for the browser: {token, expires_at}."""
    url, headers, body = phone_token_request(cfg, time.time() if now is None else now)
    try:
        status, data = await _post_json(url, headers, body)
    except TimeoutError:
        raise PhoneTokenError(504, f"SignalWire didn't answer within {SW_TIMEOUT_S:.0f} s.") from None
    except aiohttp.ClientError as exc:
        raise PhoneTokenError(502, f"Couldn't reach SignalWire: {exc}") from None
    if status in (401, 403):
        raise PhoneTokenError(502, f"SignalWire refused [phone] project_id / api_token (HTTP {status}).")
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise PhoneTokenError(502, f"SignalWire couldn't make a token (HTTP {status}): {_sw_error(data)}")
    return {"token": data["token"], "expires_at": body["expire_at"]}


# --- addresses ----------------------------------------------------------------------


def _lan_ip() -> str | None:
    """The address this Mac uses on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip: str = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith(("127.", "100.")) else ip


def _tailscale_exes() -> list[str]:
    found = shutil.which("tailscale")
    exes = [found] if found else []
    for exe in (
        "/opt/homebrew/bin/tailscale",  # brew install tailscale
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if exe not in exes and Path(exe).exists():
            exes.append(exe)
    return exes


class _IfAddrs(ctypes.Structure):
    pass


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_dstaddr", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]
_IFF_UP = 0x1
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # where Tailscale hands out addresses


def _interface_ipv4s() -> list[tuple[str, str]]:
    """(interface, IPv4 address) for every interface that's up, from getifaddrs(3): no
    process spawned. Empty when the call isn't available."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return []
    head = ctypes.POINTER(_IfAddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return []
    out: list[tuple[str, str]] = []
    try:
        node = head
        while node:
            ifa = node.contents
            if ifa.ifa_addr and ifa.ifa_flags & _IFF_UP:
                raw = ctypes.string_at(ifa.ifa_addr, 8)
                # BSD sockaddr: sa_len, sa_family (one byte each); Linux: a two-byte family
                family = raw[1] if hasattr(socket, "AF_LINK") else int.from_bytes(raw[:2], "little")
                if family == socket.AF_INET:
                    out.append(((ifa.ifa_name or b"").decode(errors="replace"), socket.inet_ntoa(raw[4:8])))
            node = ifa.ifa_next
    finally:
        libc.freeifaddrs(head)
    return out


def _tailscale_iface_ip() -> str | None:
    """Tailscale's address read off the interfaces: the one 100.64.0.0/10 address on a
    tunnel (utun on the Mac, tailscale0 elsewhere). None when there isn't exactly one."""
    found = {
        ip
        for name, ip in _interface_ipv4s()
        if name.startswith(("utun", "tailscale")) and ipaddress.ip_address(ip) in _CGNAT
    }
    return found.pop() if len(found) == 1 else None


async def _tailscale_ip() -> str | None:
    from .mac import ToolError, _run

    if found := _tailscale_iface_ip():
        return found
    for exe in _tailscale_exes():
        try:
            ip = (await _run(exe, "ip", "-4", timeout=5)).splitlines()
        except ToolError:
            continue
        if ip:
            return ip[0].strip()
    return None


async def resolve_hosts(names: list[str], quiet: bool = False) -> list[str]:
    """Turn the configured names into concrete addresses, skipping any that aren't up."""
    out: list[str] = []
    for name in names:
        ip: str | None
        if name == "localhost":
            ip = "127.0.0.1"
        elif name == "lan":
            ip = _lan_ip()
        elif name == "tailscale":
            ip = await _tailscale_ip()
        else:
            ip = name
        if ip is None:
            if not quiet:
                log.warning("web: no %s address right now; will keep checking", name)
        elif ip not in out:
            out.append(ip)
    return out


# --- request checks -----------------------------------------------------------------


def _is_loopback(req: web.Request) -> bool:
    try:
        return ipaddress.ip_address(req.remote or "").is_loopback
    except ValueError:
        return False


_TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")  # Tailscale's IPv6 range


def tailnet_peer(remote: str | None) -> bool:
    """A request from this Mac (Tailscale Serve proxies from 127.0.0.1) or the tailnet,
    not from the home network."""
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    return ip.is_loopback or ip in _CGNAT or ip in _TAILNET6


def serve_login(remote: str | None, headers: Any) -> str:
    """The Tailscale login Tailscale Serve vouches for (Tailscale-User-Login), or "". Only
    on a request Serve proxied: from this Mac's loopback, as https. Serve removes any copy
    a client sends, and adds none for tagged devices or Funnel; a LAN or tailnet listener
    gets whatever the client sent, so it is never believed there."""
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return ""
    if not ip.is_loopback:
        return ""
    if headers.get("X-Forwarded-Proto", "").split(",")[0].strip() != "https":
        return ""
    return str(headers.get("Tailscale-User-Login", "")).strip()


def owner_login(status: Any) -> str:
    """This Mac's own Tailscale login, from `tailscale status --json`."""
    try:
        uid = status["Self"]["UserID"]
        return str(status["User"][str(uid)]["LoginName"]).strip()
    except (KeyError, TypeError):
        return ""


def _tailscale_owner() -> str:
    for exe in _tailscale_exes():
        try:
            out = subprocess.run(  # noqa: S603 (a fixed program with fixed arguments)
                [exe, "status", "--json"], capture_output=True, text=True, timeout=5, check=False
            ).stdout
            login = owner_login(json.loads(out or "{}"))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.info("web: tailscale status not read (%s)", exc)
            continue
        if login:
            return login
    return ""


PAIR_MODES = ("tailnet", "approve", "code")
OWNER_TTL_S = 600.0


def _netloc(scheme: str, host: str) -> str:
    host = host.strip().lower()
    for default in (":80",) if scheme == "http" else (":443",) if scheme == "https" else ():
        if host.endswith(default):
            host = host[: -len(default)]
    return host


def origin_ok(req: web.Request) -> bool:
    """A browser request must come from the page itself: same host (and port) it was
    served from, whether that's localhost, the LAN address or the Tailscale name.
    Requests without an Origin (the MCP server, curl) are not from a web page."""
    origin = req.headers.get("Origin")
    if origin is None:
        return True
    u = urlsplit(origin)
    if u.scheme not in ("http", "https") or not u.netloc:
        return False  # "null" (sandboxed frames, file: pages) and the like
    want = _netloc(u.scheme, u.netloc)
    hosts = [req.host]
    if _is_loopback(req) and (fwd := req.headers.get("X-Forwarded-Host")):
        hosts.append(fwd.split(",")[0])  # Tailscale Serve proxies from 127.0.0.1
    return any(_netloc(u.scheme, h) == want for h in hosts)


def _is_https(req: web.Request) -> bool:
    if req.secure:
        return True
    return _is_loopback(req) and req.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"


def _number(value: Any, default: float, hi: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise TypeError(f"expected a number, got {value!r}")
    n = float(value)
    if not math.isfinite(n):
        raise ValueError(f"expected a finite number, got {value!r}")
    return min(max(n, 0.0), hi)


def _shown(snap: dict[str, Any] | None) -> Any:
    """What a state refresh compares: everything but the hold countdown."""
    if snap is None:
        return None
    floor = snap.get("floor")
    if isinstance(floor, dict):
        snap = {**snap, "floor": {k: v for k, v in floor.items() if k != "held_for_s"}}
    return snap


# --- one page connection ------------------------------------------------------------


class _Conn:
    """One open page: an ordered outbox of events, and reply audio for this device."""

    def __init__(self, ws: web.WebSocketResponse, client_id: str) -> None:
        self.ws = ws
        self.client_id = client_id
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(MAX_OUTBOX)
        self.audio: deque[str] = deque()  # sentences to synthesize, in order
        self.inflight: str | None = None  # the sentence being synthesized now
        self.audio_ready = asyncio.Event()
        self.epoch = 0  # bumped by stop/reset: audio synthesized before it is dropped
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.working = False  # a typed line or clip from this page is being handled
        self._closing: asyncio.Task[Any] | None = None

    def push(self, ev: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.outbox.put_nowait(ev)
        except asyncio.QueueFull:
            log.warning("web: page %s isn't keeping up; disconnecting it", self.client_id[:8])
            self.close(WSCloseCode.TRY_AGAIN_LATER)

    def close(self, code: int = WSCloseCode.GOING_AWAY) -> None:
        self.closed = True
        if self._closing is None:
            self._closing = asyncio.create_task(self.ws.close(code=code))

    def queue_audio(self, texts: Iterable[str]) -> None:
        self.audio.extend(texts)
        if self.audio:
            self.audio_ready.set()

    def take_audio(self) -> list[str]:
        """Sentences not yet played here (the in-flight one first), for another socket."""
        texts = ([self.inflight] if self.inflight else []) + list(self.audio)
        self.audio.clear()
        self.inflight = None
        self.epoch += 1
        return texts

    def clear_audio(self) -> None:
        self.audio.clear()
        self.inflight = None
        self.epoch += 1

    async def send(self, data: dict[str, Any]) -> bool:
        if self.closed or self.ws.closed:
            return False
        try:
            async with self.send_lock:
                await self.ws.send_json(data)
        except Exception as exc:  # the socket went away mid-send
            log.debug("web: send to %s failed: %s", self.client_id[:8], exc)
            self.close()
            return False
        return True

    async def run_sender(self) -> None:
        while not self.closed:
            ev = await self.outbox.get()
            if not await self.send(ev):
                return


# --- all pages: one subscription, routed in emit order ------------------------------


class _Hub:
    """Fans the assistant's events out to the open pages. Every routing decision is
    made in emit order (attach/detach drain the queue first), so reply audio goes to
    the newest socket of the page that asked, and is kept briefly while that page
    reconnects (iOS drops sockets when the phone locks)."""

    def __init__(self, app: VoiceApp) -> None:
        self.app = app
        self.q: asyncio.Queue[dict[str, Any]] | None = None
        self.conns: set[_Conn] = set()
        self.latest: dict[str, _Conn] = {}
        self.pending: dict[str, deque[tuple[float, str]]] = {}
        self._last_state: dict[str, Any] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.bg: set[asyncio.Task[Any]] = set()  # page work that outlives its socket

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)
        return task

    def start(self) -> None:
        self.q = self.app.subscribe()
        self._tasks = [asyncio.create_task(self._run(self.q)), asyncio.create_task(self._refresh())]

    async def close(self) -> None:
        tasks = [*self._tasks, *self.bg]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.q is not None:
            self.app.unsubscribe(self.q)
            self.q = None

    async def _run(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            self._dispatch(await q.get())
            self.drain()

    async def _refresh(self) -> None:
        """Floor holds lapse and queues change without an event; re-send the state when
        it differs from what the pages last got."""
        while True:
            await asyncio.sleep(REFRESH_SECONDS)
            if self.conns:
                self.drain()
                snap = self.app.snapshot()
                if _shown(snap) != _shown(self._last_state):
                    self._last_state = snap
                    for c in self.conns:
                        c.push({"type": "state", "ts": time.time(), **snap})

    def drain(self) -> None:
        if self.q is None:
            return
        while True:
            try:
                ev = self.q.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._dispatch(ev)

    def _dispatch(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "say":
            to, text = ev.get("to"), ev.get("text")
            if isinstance(to, str) and isinstance(text, str) and text:
                conn = self.latest.get(to)
                if conn is not None:
                    conn.queue_audio([text])
                else:
                    self._keep(to, [text])
            return
        if kind in ("stopped", "reset"):  # nothing queued should play after a stop
            self.pending.clear()
            for c in self.conns:
                c.clear_audio()
        if kind == "state":
            self._last_state = {k: v for k, v in ev.items() if k not in ("type", "ts")}
        for c in list(self.conns):
            c.push(ev)

    def _keep(self, client_id: str, texts: list[str]) -> None:
        now = time.monotonic()
        q = self.pending.setdefault(client_id, deque(maxlen=PENDING_MAX))
        q.extend((now, t) for t in texts)

    def attach(self, conn: _Conn) -> dict[str, Any]:
        """Register a page and return its hello. Synchronous: nothing can be emitted
        between the history it is sent and the first event it receives."""
        self.drain()
        snap = self.app.snapshot()
        hello = {
            "type": "hello",
            "client": conn.client_id,
            "history": list(self.app.history),
            "rules": self.app.rules.listing(),
            **snap,
        }
        self.conns.add(conn)
        old = self.latest.get(conn.client_id)
        self.latest[conn.client_id] = conn
        if old is not None:
            conn.queue_audio(old.take_audio())
        kept = self.pending.pop(conn.client_id, None)
        if kept:
            cutoff = time.monotonic() - PENDING_SECONDS
            conn.queue_audio(t for at, t in kept if at >= cutoff)
        return hello

    def detach(self, conn: _Conn) -> None:
        self.drain()
        self.conns.discard(conn)
        if self.latest.get(conn.client_id) is conn:
            del self.latest[conn.client_id]
            left = conn.take_audio()
            if left:
                self._keep(conn.client_id, left)


async def _audio_worker(hub: _Hub, conn: _Conn) -> None:
    """Synthesize this page's reply audio one sentence at a time, in order, without
    holding up its other events."""
    while not conn.closed:
        if not conn.audio:
            conn.audio_ready.clear()
            await conn.audio_ready.wait()
            continue
        text = conn.audio.popleft()
        conn.inflight = text
        epoch = conn.epoch
        try:
            wav = await hub.app.speaker.synth_wav(text)
        except Exception:
            log.exception("could not synthesize reply audio")
            if conn.epoch == epoch:
                conn.inflight = None
            continue
        if conn.epoch != epoch or hub.latest.get(conn.client_id) is not conn:
            continue  # stopped, or handed to a newer socket of the same page
        sent = await conn.send({"type": "audio_reply", "data": base64.b64encode(wav).decode()})
        if sent and conn.epoch == epoch:
            conn.inflight = None


# --- the server ---------------------------------------------------------------------


async def start(cfg: WebConfig, assistant: VoiceApp, phone: PhoneConfig | None = None) -> web.AppRunner:
    token = load_token()
    phone = phone or PhoneConfig()
    files = resources.files(__package__)
    page = files.joinpath("web.html").read_text()
    phone_page = files.joinpath("phone.html").read_text()
    phone_js = {name: files.joinpath(name).read_text() for name in ("phone.js", "phone_sign.js")}
    hub = _Hub(assistant)
    pair_requests = phone_pairing.PairRequests(
        assistant.approve_pairing, lambda name: phone_devices.pair(name)
    )
    pair_mode = phone.pair_mode if phone.pair_mode in PAIR_MODES else "approve"
    if pair_mode != phone.pair_mode:
        log.warning(
            "web: [phone] pair_mode %r is not one of %s; asking in the room", phone.pair_mode, PAIR_MODES
        )
    owner_cache: list[tuple[float, str]] = []

    async def owners() -> set[str]:
        """The logins that count as the owner: this Mac's own (kept a while) and [phone] owners."""
        if not owner_cache or time.monotonic() - owner_cache[0][0] > OWNER_TTL_S:
            owner_cache[:] = [(time.monotonic(), await asyncio.to_thread(_tailscale_owner))]
        return {o.casefold() for o in [owner_cache[0][1], *phone.owners] if o.strip()}

    # A phone's signed token request: its secret over device|ts|nonce|TOKEN_PURPOSE.
    token_check = phone_devices.CallCheck(phone_pairing.TOKEN_PURPOSE, 60.0)

    def authed(req: web.Request, allow_query: bool = False) -> bool:
        bearer = req.headers.get("Authorization", "")
        given = (
            (bearer[7:] if bearer.startswith("Bearer ") else "")
            or (req.query.get("token", "") if allow_query else "")
            or req.cookies.get(_COOKIE, "")
        )
        return token_ok(given, token)

    def guard(req: web.Request) -> None:
        if not origin_ok(req):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        if not authed(req):
            raise web.HTTPUnauthorized()

    # --- API for the MCP server (and anything else holding the token) -------------

    async def api_discuss(req: web.Request) -> web.StreamResponse:
        guard(req)
        if req.content_type != "application/json":  # also forces a CORS preflight
            raise web.HTTPUnsupportedMediaType(text="Send application/json.")
        try:
            b = await req.json()
            if not isinstance(b, dict):
                raise TypeError("the body must be a JSON object")
            message, voice, agent = b.get("message"), b.get("voice"), b.get("agent")
            if message is not None and not isinstance(message, str):
                raise TypeError("message must be a string")
            if voice is not None and not isinstance(voice, str):
                raise TypeError("voice must be a string")
            if agent is not None and not isinstance(agent, str):
                raise TypeError("agent must be a string")
            listen = b.get("wait_for_response", True)
            hold = b.get("hold_floor", False)
            if not isinstance(listen, bool) or not isinstance(hold, bool):
                raise TypeError("wait_for_response and hold_floor must be true or false")
            timeout = _number(b.get("listen_timeout"), 30.0, 300.0)
            wait_for_floor = _number(b.get("wait_for_floor"), 0.0, 300.0)
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "error": str(exc)}, status=400)
        result = await assistant.discuss(
            agent=agent or "session",
            message=message,
            listen=listen,
            timeout=timeout,
            hold=hold,
            voice=voice or None,
            wait_for_floor=wait_for_floor,
        )
        return web.json_response(result)

    async def api_tool(req: web.Request) -> web.StreamResponse:
        """A mail or calendar tool call from another Claude session (the MCP server)."""
        guard(req)
        if req.content_type != "application/json":  # also forces a CORS preflight
            raise web.HTTPUnsupportedMediaType(text="Send application/json.")
        try:
            b = await req.json()
            if not isinstance(b, dict):
                raise TypeError("the body must be a JSON object")
            name, args, agent = b.get("name"), b.get("args", {}), b.get("agent")
            if not isinstance(name, str) or not isinstance(args, dict):
                raise TypeError("name must be a string and args an object")
            if agent is not None and not isinstance(agent, str):
                raise TypeError("agent must be a string")
            preview, confirmed = b.get("preview", False), b.get("confirmed", False)
            if not isinstance(preview, bool) or not isinstance(confirmed, bool):
                raise TypeError("preview and confirmed must be true or false")
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "error": str(exc)}, status=400)
        if (agent == PHONE_AGENT or preview or confirmed) and not phone_ok(req):
            raise web.HTTPForbidden(text="Only scout-phone may act for the phone.")
        return web.json_response(
            await assistant.run_shared_tool(
                agent or "session", name, args, preview=preview, confirmed=confirmed
            )
        )

    async def api_ask(req: web.Request) -> web.StreamResponse:
        """A request from the phone (scout-phone): answered as text, never spoken here."""
        guard(req)
        if req.content_type != "application/json":  # also forces a CORS preflight
            raise web.HTTPUnsupportedMediaType(text="Send application/json.")
        try:
            b = await req.json()
            if not isinstance(b, dict):
                raise TypeError("the body must be a JSON object")
            text, agent, via = b.get("text"), b.get("agent"), b.get("via", "")
            if not isinstance(via, str):
                raise TypeError("via must be a string")
            if not isinstance(text, str) or not text.strip():
                raise TypeError("text must be a non-empty string")
            if len(text) > 2000:
                raise ValueError("text is too long")
            if agent is not None and not isinstance(agent, str):
                raise TypeError("agent must be a string")
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "error": str(exc)}, status=400)
        if (agent == PHONE_AGENT or via) and not phone_ok(req):
            raise web.HTTPForbidden(text="Only scout-phone may act for the phone.")
        if via:  # the phone's tool for one of the user's MCP servers (extmcp.py)
            return web.json_response(await assistant.ask(PHONE_AGENT, text, via=via))
        return web.json_response(await assistant.ask(agent or "session", text))

    async def api_status(req: web.Request) -> web.StreamResponse:
        guard(req)
        return web.json_response(assistant.voice_status())

    def no_address() -> web.StreamResponse | None:
        if phone.address.strip():
            return None
        return web.json_response(
            {"status": "error", "error": "Set [phone] address (what the app dials) in Scout's config first."},
            status=409,
        )

    async def json_body(req: web.Request) -> dict[str, Any]:
        if req.content_type != "application/json":  # also forces a CORS preflight
            raise web.HTTPUnsupportedMediaType(text="Send application/json.")
        try:
            b = await req.json()
        except ValueError:
            b = None
        if not isinstance(b, dict):
            raise TypeError("the body must be a JSON object")
        return b

    def phone_name(b: dict[str, Any]) -> str:
        name = b.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TypeError("name must be a non-empty string")
        if len(name) > 64:
            raise ValueError("name is too long (64 characters at most)")
        return " ".join(name.split())

    def refused(status: int, error: str) -> web.StreamResponse:
        return web.json_response({"status": "error", "error": error}, status=status)

    wrong_codes = phone_pairing.Limiter(limit=5, window_s=60.0)
    new_code = "Make a new one: scoutctl phone pair, or Pair the phone app on Scout's /phone page."
    code_refusals = {
        "unknown": (401, f"That pairing code isn't right (or a newer one replaced it). {new_code}"),
        "expired": (410, f"That pairing code expired (a code lasts 10 minutes). {new_code}"),
        "used": (410, f"That pairing code was already used. {new_code}"),
    }

    async def api_phone_pair(req: web.Request) -> web.StreamResponse:
        """Pair a phone or browser: with Scout's web token (the /phone test page), or
        with a one-time pairing code instead of it (the app, from a scout://pair link).
        The secret is returned this once."""
        if not origin_ok(req):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        with_token = authed(req)
        try:
            b = await json_body(req)
        except TypeError as exc:
            if not with_token:
                raise web.HTTPUnauthorized() from None
            return refused(400, str(exc))
        code = b.get("code")
        if not with_token and not (isinstance(code, str) and code.strip()):
            raise web.HTTPUnauthorized()
        try:
            name = phone_name(b)
        except (ValueError, TypeError) as exc:
            return refused(400, str(exc))
        if (r := no_address()) is not None:
            return r
        if not with_token and isinstance(code, str):
            if not wrong_codes.allowed():
                log.warning("web: a pairing code refused: too many wrong ones")
                return refused(429, "Too many wrong pairing codes; wait a minute and try again.")
            verdict = await asyncio.to_thread(phone_pairing.consume, code)
            if verdict != "ok":
                wrong_codes.failed()
                log.warning("web: a pairing code was refused (%s)", verdict)
                return refused(*code_refusals[verdict])
        try:
            device, secret = await asyncio.to_thread(phone_devices.pair, name)
        except (OSError, ValueError) as exc:
            log.warning("web: pairing a browser failed: %s", exc)
            return web.json_response({"status": "error", "error": f"Couldn't pair: {exc}"}, status=500)
        how = "with the web token" if with_token else "with a pairing code"
        log.info("web: paired %s (%s) %s", device, name, how)
        resp = web.json_response({"device": device, "secret": secret, "address": phone.address})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def api_phone_pair_code(req: web.Request) -> web.StreamResponse:
        """A one-time pairing code for the app, with its scout://pair link and QR code
        (the /phone page's "Pair the phone app"). The web token is in none of them."""
        guard(req)
        server = await asyncio.to_thread(_web_base)
        code, expires_at = await asyncio.to_thread(phone_pairing.mint)
        link = phone_pairing.link(server, code)
        warning = (
            ""
            if server.startswith("https://")
            else f"Scout's address here is {server or 'unknown'}, but the app needs its https Tailscale"
            " name: turn on Tailscale Serve (scripts/tailscale.sh enable) or set [web] public_url."
        )
        log.info("web: a pairing code was made on the web UI")
        resp = web.json_response(
            {
                "code": code,
                "link": link,
                "server": server,
                "expires_at": expires_at,
                "ttl_s": int(phone_pairing.TTL_S),
                "qr": phone_pairing.qr_svg_data_uri(link),
                "warning": warning,
            }
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def api_phone_pair_request(req: web.Request) -> web.StreamResponse:
        """A phone asks to pair without a code; the room approves it by voice (or on the
        web UI). Only from the tailnet."""
        if not origin_ok(req):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        if not tailnet_peer(req.remote):
            raise web.HTTPForbidden(text="Pairing requests come over Tailscale only.")
        try:
            name = phone_name(await json_body(req))
        except (ValueError, TypeError) as exc:
            return refused(400, str(exc))
        if (r := no_address()) is not None:
            return r
        if pair_mode == "code":
            return refused(
                403, "Scout pairs phones by code only here: run scoutctl phone pair and scan the QR code."
            )
        login = serve_login(req.remote, req.headers)
        if pair_mode == "tailnet" and login and login.casefold() in await owners():
            try:
                device, secret = await asyncio.to_thread(phone_devices.pair, name)
            except (OSError, ValueError) as exc:
                log.warning("web: pairing %r failed: %s", name, exc)
                return refused(500, f"Couldn't pair: {exc}")
            log.info("web: paired %s (%s) at once: it is the owner's Tailscale login %s", device, name, login)
            hub.spawn(assistant.announce(f"Paired {name}."))
            resp = web.json_response(
                {"status": "approved", "device": device, "secret": secret, "address": phone.address}
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if login:
            log.info("web: pairing request from Tailscale login %s (not the owner): asking the room", login)
        try:
            started = pair_requests.start(name)
        except phone_pairing.PairRefused as exc:
            log.warning("web: a pairing request from %r refused: %s", name, exc)
            return refused(exc.status, str(exc))
        resp = web.json_response(started)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def api_phone_pair_status(req: web.Request) -> web.StreamResponse:
        """Where a pairing request stands (waits up to `wait` seconds for the answer); the
        secret comes back once, when the room said yes."""
        if not origin_ok(req):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        try:
            b = await json_body(req)
            rid = b.get("request")
            if not isinstance(rid, str):
                raise TypeError("request must be a string")
            wait = _number(b.get("wait"), 0.0, 25.0)
        except (ValueError, TypeError) as exc:
            return refused(400, str(exc))
        got = await pair_requests.status(rid, wait)
        if got.get("status") == "approved":
            got["address"] = phone.address
        resp = web.json_response(got)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def api_phone_token(req: web.Request) -> web.StreamResponse:
        """A short-lived SignalWire subscriber token: for the /phone page (the web token),
        or for a paired phone, which signs the request with its device secret."""
        if not origin_ok(req):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        if not authed(req):
            if req.content_type != "application/json":
                raise web.HTTPUnauthorized()
            try:
                b = await json_body(req)
            except TypeError:
                raise web.HTTPUnauthorized() from None
            verdict = token_check.check(b)
            if not verdict.ok:
                log.warning("web: a phone's token request refused: %s", verdict.reason)
                raise web.HTTPUnauthorized()
        try:
            minted = await mint_subscriber_token(phone)
        except PhoneTokenError as exc:
            log.warning("web: no SignalWire token: %s", exc)
            return web.json_response({"status": "error", "error": str(exc)}, status=exc.status)
        resp = web.json_response(minted)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def page_response(req: web.Request, text: str, csp: str) -> web.StreamResponse:
        if not authed(req, allow_query=True):
            return web.Response(
                status=401, text="Missing or wrong token. Use ?token=<the contents of state/web_token>."
            )
        resp = web.Response(text=text, content_type="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"  # the URL may carry the token
        resp.headers["Content-Security-Policy"] = csp
        resp.set_cookie(
            _COOKIE,
            token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="Strict",
            secure=_is_https(req),
        )
        return resp

    async def index(req: web.Request) -> web.StreamResponse:
        return page_response(req, page, "frame-ancestors 'none'")  # no clickjacking Approve

    async def phone_index(req: web.Request) -> web.StreamResponse:
        return page_response(req, phone_page, _PHONE_CSP)

    async def record_page(req: web.Request) -> web.StreamResponse:
        """A record of Scout's store (/r/<id>): what a link from Scout opens on the phone."""
        from . import index, recordpage

        if not authed(req, allow_query=True):
            return page_response(req, "", recordpage.CSP)  # the 401 page, which says how
        id = req.match_info["id"]
        if len(id) > 300:
            raise web.HTTPNotFound()
        store = index.default()

        def look() -> tuple[dict[str, Any] | None, Any]:
            rec = store.resolve(id)
            return rec, (store.get(id) if rec else None)

        try:
            rec, hit = await asyncio.to_thread(look)
        except Exception as exc:  # the memory service is down
            log.warning("web: record %s not read: %s", id, exc)
            return web.Response(status=503, text="Scout's memory service isn't answering; try again shortly.")
        if rec is None:
            resp = page_response(req, recordpage.not_found(id), recordpage.CSP)
            resp.set_status(404)
            return resp
        return page_response(req, recordpage.render(rec, hit), recordpage.CSP)

    async def reference_page(req: web.Request) -> web.StreamResponse:
        """An offline reference article, or an image or stylesheet it uses, as stored in its
        archive (/ref/<book>/<path>, reference.py): token-gated, with scripts blocked."""
        from . import reference

        if not authed(req, allow_query=True):
            return page_response(req, "", _REFERENCE_CSP)  # the 401 page, which says how
        book, path = req.match_info["book"], req.match_info["path"]
        try:
            got = await asyncio.to_thread(reference.library().content, book, path)
        except KeyError:
            raise web.HTTPNotFound() from None
        except Exception as exc:  # a damaged archive
            log.warning("web: reference %s/%s not read: %s", book, path, exc)
            raise web.HTTPNotFound() from None
        if got.redirect:  # relative, so the article's own relative links keep working
            found = web.HTTPFound(reference.relative(path, got.redirect))
            keep_cookie(req, found)
            raise found
        resp = web.Response(
            body=got.data, headers={"Content-Type": got.mimetype or "application/octet-stream"}
        )
        resp.headers["Content-Security-Policy"] = _REFERENCE_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "private, max-age=86400"
        keep_cookie(req, resp)
        return resp

    def keep_cookie(req: web.Request, resp: web.StreamResponse) -> None:
        """Opened from a link with the token: set the cookie, so its images and links load."""
        if req.query.get("token"):
            resp.set_cookie(
                _COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Strict",
                secure=_is_https(req),
            )  # fmt: skip

    async def phone_script(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            raise web.HTTPUnauthorized()
        resp = web.Response(text=phone_js[req.match_info["name"]], content_type="text/javascript")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def ws_handler(req: web.Request) -> web.StreamResponse:
        guard(req)
        cid = req.query.get("client", "")
        client_id = cid if _CLIENT_ID.fullmatch(cid) else uuid.uuid4().hex
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_CLIP_BYTES * 4 // 3 + 64 * 1024)
        await ws.prepare(req)
        conn = _Conn(ws, client_id)
        requests: asyncio.Queue[Coroutine[Any, Any, None]] = asyncio.Queue(MAX_REQUESTS)

        def reply(ev: dict[str, Any]) -> None:
            """An answer to this page only, after the broadcasts it caused."""
            hub.drain()
            conn.push(ev)

        async def say(text: str, speak: bool, to_me: str | None) -> None:
            ignored = await assistant.submit_text(text, speak=speak, client=to_me)
            if ignored and ignored != "gate":
                reply({"type": "error", "text": f"Not sent: {ignored}."})

        async def ptt(data: str, speak: bool, to_me: str | None) -> None:
            try:
                clip = base64.b64decode(data, validate=True)
                if len(clip) > MAX_CLIP_BYTES:
                    raise ValueError("the recording is too large")
                pcm = await decode_to_pcm(clip)
                if len(pcm) > MAX_CLIP_SECONDS * SAMPLE_RATE * 2:
                    raise ValueError(f"the recording is longer than {MAX_CLIP_SECONDS:.0f} seconds")
                result = await assistant.submit_audio(pcm, speak=speak, client=to_me)
                reply({"type": "ptt_result", **result})
            except Exception as exc:
                log.warning("push-to-talk failed: %s", exc)
                reply({"type": "error", "text": f"Couldn't use that recording: {exc}"})

        def handle(m: dict[str, Any]) -> None:
            kind = m.get("type")
            output = m.get("output")
            output = output if output in _OUTPUTS else "device"  # where replies play
            to_mini = output in ("mini", "both")
            to_me = client_id if output in ("device", "both") else None
            if kind in ("say", "audio"):
                field = m.get("text") if kind == "say" else m.get("data")
                if not isinstance(field, str) or not field.strip():
                    return
                job = say(field.strip(), to_mini, to_me) if kind == "say" else ptt(field, to_mini, to_me)
                try:
                    requests.put_nowait(job)
                except asyncio.QueueFull:
                    job.close()
                    reply({"type": "error", "text": "Not sent: still working on your last ones."})
            elif kind == "confirm":
                cid_ = m.get("id")
                if isinstance(cid_, bool) or not isinstance(cid_, int):
                    reply({"type": "error", "text": "That approval had no question id; reload the page."})
                    return
                answer: bool | str = "always" if m.get("always") is True else m.get("approved") is True
                assistant.answer_confirm(answer, confirm_id=cid_)
            elif kind == "remove_rule":
                rule_id = m.get("id")
                if isinstance(rule_id, str) and rule_id:
                    assistant.remove_rule(rule_id)
            elif kind == "stop":
                hub.spawn(assistant.stop())
            elif kind == "mute":
                assistant.set_mic_muted(m.get("value") is True)
            elif kind == "reset":
                hub.spawn(assistant.reset(speak=to_mini, client=to_me))

        tasks: list[asyncio.Task[Any]] = []
        worker: asyncio.Task[Any] | None = None
        try:
            conn.push(hub.attach(conn))
            tasks += [asyncio.create_task(conn.run_sender()), asyncio.create_task(_audio_worker(hub, conn))]
            worker = hub.spawn(_request_worker(conn, requests))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(m, dict):
                    continue
                try:
                    handle(m)
                except Exception:
                    log.exception("web: couldn't handle a %r message", m.get("type"))
        finally:
            conn.closed = True
            hub.detach(conn)
            for t in tasks:
                t.cancel()
            # What the page already sent still runs (iOS drops the socket when the phone
            # locks); its spoken reply waits for the page to reconnect.
            if worker is not None and not conn.working and requests.empty():
                worker.cancel()
        return ws

    app = web.Application(client_max_size=1024 * 1024)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/ws", ws_handler),
            web.post("/api/discuss", api_discuss),
            web.post("/api/tool", api_tool),
            web.post("/api/ask", api_ask),
            web.get("/api/status", api_status),
            web.get("/phone", phone_index),
            web.get("/r/{id:.+}", record_page),
            web.get("/ref/{book}/{path:.+}", reference_page),
            web.get(r"/phone/{name:phone(?:_sign)?\.js}", phone_script),
            web.post("/api/phone/pair", api_phone_pair),
            web.post("/api/phone/pair-code", api_phone_pair_code),
            web.post("/api/phone/pair/request", api_phone_pair_request),
            web.post("/api/phone/pair/status", api_phone_pair_status),
            web.post("/api/phone/token", api_phone_token),
        ]
    )

    async def close_pages(_app: web.Application) -> None:
        # Open sockets would otherwise hold graceful shutdown for the full timeout.
        for c in list(hub.conns):
            c.closed = True
            with contextlib.suppress(Exception):
                await c.ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")

    sites: dict[str, web.TCPSite] = {}
    warned: set[str] = set()

    async def bind(hosts: list[str]) -> None:
        for host in hosts:
            if host in sites:
                continue
            site = web.TCPSite(runner, host, cfg.port)
            try:
                await site.start()
            except OSError as exc:
                await site.stop()  # unregister it; the next check tries again
                if host not in warned:
                    warned.add(host)
                    log.warning("web: can't listen on %s:%d yet (will keep trying): %s", host, cfg.port, exc)
                continue
            warned.discard(host)
            sites[host] = site
            log.info("web: listening on %s:%d", host, cfg.port)
        for host in [h for h in sites if h not in hosts]:
            log.info("web: %s went away; no longer listening there", host)
            await sites.pop(host).stop()

    async def rebind() -> None:
        while True:
            await asyncio.sleep(REBIND_SECONDS)
            try:
                await bind(await resolve_hosts(cfg.hosts, quiet=True))
            except Exception:
                log.exception("web: rechecking addresses failed")

    rebind_task: list[asyncio.Task[Any]] = []

    advertiser: list[phone_pairing.Advertiser] = []

    async def advertise() -> None:
        """Bonjour: _scout._tcp with Scout's https address, so the app finds it."""
        ad = phone_pairing.Advertiser(DNS_SD, cfg.port, socket.gethostname().split(".")[0] or "Mac")
        advertiser.append(ad)
        while True:
            try:
                await ad.update(await asyncio.to_thread(_web_base))
            except Exception:
                log.exception("bonjour: updating the advertisement failed")
            await asyncio.sleep(ADVERTISE_SECONDS)

    async def cleanup(_app: web.Application) -> None:
        for t in rebind_task:
            t.cancel()
        for ad in advertiser:
            await ad.close()
        await pair_requests.close()
        await hub.close()

    app.on_shutdown.append(close_pages)
    app.on_cleanup.append(cleanup)
    # handler_cancellation: when a desk session gives up on a discuss call (Esc,
    # exit, timeout), cancel it so it stops listening and frees the floor.
    # shutdown_timeout: launchd allows 5 s to exit; pages are closed in on_shutdown.
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=2.0)
    await runner.setup()
    hub.start()
    hosts = await resolve_hosts(cfg.hosts)
    await bind(hosts)
    if cfg.port == 0:  # tests: a free port, so the lan/tailscale sites can't follow it
        log.info("web: bound %s", runner.addresses)
    else:
        rebind_task.append(asyncio.create_task(rebind()))
        if Path(DNS_SD).exists() and pair_mode != "code":
            rebind_task.append(asyncio.create_task(advertise()))
    shown = next((h for h in sites if not h.startswith("127.")), next(iter(sites), "127.0.0.1"))
    log.info("web UI: http://%s:%d/?token=<the contents of %s>", shown, cfg.port, TOKEN_PATH)
    return runner


async def _request_worker(conn: _Conn, requests: asyncio.Queue[Coroutine[Any, Any, None]]) -> None:
    """Typed lines and clips from one page, handled one at a time in order, so Stop,
    Deny and Mute never wait behind a transcription. Ends once the page is gone and
    everything it sent has been handled."""
    while True:
        job = await requests.get()
        conn.working = True
        try:
            await job
        except Exception:
            log.exception("web: request failed")
        finally:
            conn.working = False
        if conn.closed and requests.empty():
            return
