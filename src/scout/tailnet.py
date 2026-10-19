"""What tailscaled says about this Mac and the devices that reach it.

Two questions, both answered by tailscaled itself (through the `tailscale` CLI, which
talks to its LocalAPI), never by a request's own headers:

- **Who is this Mac on the tailnet?** Its MagicDNS name, its tailnet addresses and its
  owner's login (`tailscale status --json`), kept a few minutes. web.py lets requests
  name only these (and localhost / the LAN address) in their Host header.
- **Who is that peer?** `tailscale whois --json <ip>` for the address a request really
  came from: the TCP peer on a tailnet listener, or the client address Tailscale Serve
  puts in X-Forwarded-For (web.py checks that the request came through Serve first).
  Kept a minute per address.

Everything here is blocking (a subprocess); web.py calls it from a thread.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

STATUS_TTL_S = 300.0  # this Mac's name, addresses and owner
WHOIS_TTL_S = 60.0  # one peer's identity
REFRESH_MIN_S = 30.0  # an unknown Host may force a status refresh at most this often
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's IPv4 range
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")  # Tailscale's IPv6 range

Runner = Callable[[list[str]], str]  # tailscale arguments -> stdout ("" when it failed)


def is_tailnet_ip(text: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return False
    return ip in CGNAT or ip in TAILNET6


@dataclass(frozen=True)
class SelfInfo:
    name: str = ""  # MagicDNS name, lower case, no trailing dot ("" without MagicDNS)
    ips: frozenset[str] = field(default_factory=frozenset)
    owner: str = ""  # this Mac's own Tailscale login


@dataclass(frozen=True)
class Peer:
    login: str  # "" for a tagged device (it has no user)
    node: str  # the device's MagicDNS name, or its host name


def owner_login(status: object) -> str:
    """This Mac's own Tailscale login, from `tailscale status --json`."""
    try:
        uid = status["Self"]["UserID"]  # type: ignore[index]
        return str(status["User"][str(uid)]["LoginName"]).strip()  # type: ignore[index]
    except (KeyError, TypeError):
        return ""


def self_info(status: object) -> SelfInfo:
    """This Mac's MagicDNS name and tailnet addresses (and owner) from `tailscale status --json`."""
    if not isinstance(status, dict) or not isinstance(status.get("Self"), dict):
        return SelfInfo()
    me = status["Self"]
    name = str(me.get("DNSName") or "").strip().rstrip(".").lower()
    ips = frozenset(str(ip) for ip in me.get("TailscaleIPs") or [] if is_tailnet_ip(str(ip)))
    return SelfInfo(name, ips, owner_login(status))


def peer_of(whois: object) -> Peer | None:
    """The login and device name from `tailscale whois --json`. A tagged device has no login."""
    if not isinstance(whois, dict):
        return None
    node, prof = whois.get("Node"), whois.get("UserProfile")
    node = node if isinstance(node, dict) else {}
    prof = prof if isinstance(prof, dict) else {}
    if not node and not prof:
        return None
    tagged = bool(node.get("Tags"))
    login = "" if tagged else str(prof.get("LoginName") or "").strip()
    if login == "tagged-devices":
        login = ""
    name = str(node.get("Name") or node.get("ComputedName") or "").strip().rstrip(".")
    return Peer(login, name)


def _cli(args: list[str]) -> str:
    from .web import _tailscale_exes

    for exe in _tailscale_exes():
        try:
            out = subprocess.run(  # noqa: S603 (a fixed program; the only variable is an IP we parsed)
                [exe, *args], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("tailnet: %s %s failed (%s)", exe, args[0], exc)
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    return ""


class Tailnet:
    """tailscaled's answers, cached. `run` is the CLI (tests pass a fake)."""

    def __init__(self, run: Runner | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.run = run or _cli
        self.clock = clock
        self._lock = threading.Lock()
        self._self: tuple[float, SelfInfo] | None = None
        self._whois: dict[str, tuple[float, Peer | None]] = {}

    def me(self, refresh: bool = False) -> SelfInfo:
        """This Mac's name, addresses and owner. refresh: read again, unless that was
        done within REFRESH_MIN_S (so a stream of bad Host headers can't spawn a process each)."""
        now = self.clock()
        with self._lock:
            cached = self._self
        if cached is not None:
            age = now - cached[0]
            if age < STATUS_TTL_S and not (refresh and age >= REFRESH_MIN_S):
                return cached[1]
        try:
            info = self_info(json.loads(self.run(["status", "--json"]) or "{}"))
        except ValueError:
            info = SelfInfo()
        if cached is not None and not info.name and not info.ips:
            info = cached[1]  # tailscaled didn't answer this time: keep what we knew
        with self._lock:
            self._self = (now, info)
        return info

    def whois(self, ip: str) -> Peer | None:
        """Who tailscaled says is at `ip` (a tailnet address), or None."""
        if not is_tailnet_ip(ip):
            return None
        ip = str(ipaddress.ip_address(ip.strip()))
        now = self.clock()
        with self._lock:
            hit = self._whois.get(ip)
        if hit is not None and now - hit[0] < WHOIS_TTL_S:
            return hit[1]
        try:
            peer = peer_of(json.loads(self.run(["whois", "--json", ip]) or "null"))
        except ValueError:
            peer = None
        with self._lock:
            if len(self._whois) > 256:
                self._whois.clear()
            self._whois[ip] = (now, peer)
        return peer


_default: Tailnet | None = None


def default() -> Tailnet:
    global _default
    if _default is None:
        _default = Tailnet()
    return _default
