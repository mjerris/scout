"""How Scout's records point at each other and at the world (docs/links.md).

A record is anything in the store, by its id: task:15, note:3, journal:<when>,
granola:<note>, mailmeeting:<n>, mail:<n>, slack:<channel>:<ts>, memory:<hash>. Links
between records live in the store's `links` table (index.py), one row per (src, dst,
rel); this module holds the plain-code rules around them:

- which records a task came from: its `- source:` line and the "From ... (<source>)"
  paragraphs the farm adds when a repeat is merged in (`task_sources`);
- a source key as the farm writes it ("meeting:<granola note>", "meeting:mail:<n>",
  "mail:<n>", "slack:<channel>@<ts>") as a record id (`source_record`);
- a record's link out: Granola's notes.granola.ai page, Slack's permalink, Mail's
  message:// URL from the Message-ID, else Scout's own record page (`/r/<id>` on the
  web UI, at `web_base()`);
- whether an emailed meeting summary and a Granola note are the same meeting
  (`same_meeting`).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

RELS = ("found_in", "merged_from", "same_as", "based_on", "about")
# Kinds whose text came from someone else (mail, meetings, Slack): private outside open mode.
PRIVATE_KINDS = frozenset({"mail", "meeting", "slack"})
# Scout's own records: their link is their page on Scout's web UI.
OWN_KINDS = frozenset({"task", "note", "turn", "memory"})

# "From the meeting 'Budget' on Oct 9: "quote" (meeting:not_abc)": one paragraph per source.
_SOURCE_PARA = re.compile(r"\(((?:meeting|mail|slack):[^()\s]+)\)\s*$")


def task_sources(fields: dict[str, Any], context: str) -> list[str]:
    """A task's source keys, its `source` field first, then the sources merged into it
    (each "From ..." paragraph ends with its source in parentheses), without repeats."""
    out: list[str] = []
    first = str(fields.get("source") or "").strip()
    if first:
        out.append(first)
    for para in re.split(r"\n[ \t]*\n", context or ""):
        m = _SOURCE_PARA.search(para.strip())
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def source_record(key: str) -> str | None:
    """The record id a farm source key names; a Slack key ("slack:<ch>@<ts>") needs the
    store to find its message (index.Index._slack_record), so it's returned as it is.
    None for what isn't a record (a free-text source someone typed)."""
    key = key.strip()
    if key.startswith("meeting:mail:"):
        return "mailmeeting:" + key.removeprefix("meeting:mail:")
    if key.startswith("meeting:"):
        rest = key.removeprefix("meeting:")
        return rest if rest.startswith(("granola:", "mailmeeting:")) else f"granola:{rest}"
    if re.fullmatch(r"(?:mail|mailmeeting|granola|slack|task|note|journal|memory):\S+", key):
        return key
    return None


def message_url(message_id: str) -> str | None:
    """Mail's link to a message by its Message-ID header: message://%3C<id>%3E (opens it
    in Mail on the Mac, iPhone and iPad)."""
    mid = message_id.strip().removeprefix("<").removesuffix(">").strip()
    if not mid or any(c.isspace() for c in mid):
        return None
    return "message://%3C" + urllib.parse.quote(mid, safe="@!$&'()*+,;=-._~") + "%3E"


def page_url(base: str, id: str) -> str | None:
    """A record's page on Scout's web UI: {base}/r/<id, quoted>."""
    return f"{base.rstrip('/')}/r/{urllib.parse.quote(id, safe='')}" if base else None


# --- where Scout's web UI is reachable from the phone ---------------------------------------

_BASE_TTL_S = 600.0
_base: tuple[float, str] = (0.0, "")


def web_base(refresh: bool = False) -> str:
    """Where the phone reaches Scout's web UI: `[web] public_url` when set; else the
    Tailscale HTTPS name when Tailscale Serve forwards to the web port (the address
    scripts/tailscale.sh enable sets up, and where the phone's cookie is); else the
    tailnet address, the home-network address, or localhost, at the web port. Kept
    for a few minutes (asking Tailscale is a subprocess)."""
    global _base
    if not refresh and _base[1] and time.monotonic() - _base[0] < _BASE_TTL_S:
        return _base[1]
    from .config import load

    cfg = load().web
    base = (getattr(cfg, "public_url", "") or "").strip().rstrip("/")
    if not base:
        base = _served_name(cfg.port) or _address_base(cfg.hosts, cfg.port)
    _base = (time.monotonic(), base)
    return base


def _tailscale() -> str | None:
    from . import web

    exes = web._tailscale_exes()
    return exes[0] if exes else shutil.which("tailscale")


def _served_name(port: int) -> str:
    """https://<name> when Tailscale Serve forwards that name's port 443 to the web UI."""
    exe = _tailscale()
    if not exe:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 (a fixed program with fixed arguments)
            [exe, "serve", "status", "--json"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        return served_base(json.loads(out or "{}"), port)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.info("links: Tailscale Serve not read (%s)", exc)
        return ""


def served_base(status: dict[str, Any], port: int) -> str:
    """From `tailscale serve status --json`: https://<name> whose "/" proxies to the web
    UI's port on 127.0.0.1 (on 443; another port is kept in the URL)."""
    for hostport, conf in (status.get("Web") or {}).items():
        proxy = str(((conf or {}).get("Handlers") or {}).get("/", {}).get("Proxy") or "")
        if re.fullmatch(rf"https?://(?:127\.0\.0\.1|localhost):{port}/?", proxy):
            host, _, p = str(hostport).rpartition(":")
            return f"https://{host}" if p == "443" else f"https://{hostport}"
    return ""


def _address_base(hosts: Iterable[str], port: int) -> str:
    from . import web

    for name in ("tailscale", "lan"):
        if name in hosts:
            ip = web._tailscale_iface_ip() if name == "tailscale" else web._lan_ip()
            if ip:
                return f"http://{ip}:{port}"
    return f"http://127.0.0.1:{port}"


# --- emailed meeting summaries and Granola notes of the same meeting --------------------------

# A summary email comes after the meeting: Granola's time is the meeting's scheduled start.
SAME_MEETING_BEFORE_S = 6 * 3600.0  # the meeting started at most this long before the email
SAME_MEETING_AFTER_S = 15 * 60.0  # or this little after it (clocks, an email sent early)
SAME_TITLE = 0.6  # word overlap (Jaccard) of the two titles
_TITLE_STOP = frozenset({"the", "a", "an", "and", "of", "for", "with", "meeting", "call", "sync", "notes",
                         "summary", "recap", "transcript", "by", "gemini", "on", "at", "to"})  # fmt: skip


def title_words(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in _TITLE_STOP and not re.fullmatch(r"\d{1,2}|20\d\d", w)}


def title_overlap(a: str, b: str) -> float:
    wa, wb = title_words(a), title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def same_meeting(
    email_ts: float, email_title: str, candidates: Iterable[tuple[str, float, str]]
) -> str | None:
    """The Granola note (id) an emailed summary is about: started within the window
    before the email, titles overlapping by SAME_TITLE or more, and the best of them by
    a clear margin. None when nothing fits or two fit equally (better no link than a
    wrong one)."""
    scored = []
    for id, ts, title in candidates:
        if not (email_ts - SAME_MEETING_BEFORE_S <= ts <= email_ts + SAME_MEETING_AFTER_S):
            continue
        s = title_overlap(email_title, title)
        if s >= SAME_TITLE:
            scored.append((s, -abs(email_ts - ts), id))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][2]
