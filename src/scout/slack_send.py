"""Sending a Slack message as the user, only after a yes (docs/slack.md, Sending).

Two tools, like mail's draft and send:

    slack_draft {to, text}   who or where `to` is: a person (a DM; by display, real or
                             user name, or a first name: "Luca" is "Luca Pradovera") or a
                             channel you're in ("#launch"). One match: a draft with an id.
                             More than one: their names, to ask the user which. Sends
                             nothing.
    slack_send {draft}       sends that draft, once (shared_tools: asks=True). The room
                             reads it back and waits for a spoken or clicked yes
                             (brain.ALWAYS_ASK_TOOLS, Assistant.run_shared_tool for other
                             sessions); the phone in two steps, prepare then
                             confirm_action after the caller's yes. "Yes, always" never
                             applies (trust.NEVER).

The confirmation names the resolved recipient and reads the text back (describe), so a
yes is to exactly what goes out. chat.postMessage with the user token: it's posted as
the user. Drafts live in the app (every caller's tools run there) for DRAFT_TTL_S.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import slack
from .config import load
from .mac import ToolError

DRAFT_TTL_S = 1800.0
DIRECTORY_TTL_S = 3600.0
MAX_CHARS = 4000
SPOKEN_CHARS = 300
_CODE = re.compile(r"[a-z_]+")  # an error code Slack answered with: it wasn't posted


@dataclass
class Draft:
    id: str
    channel: str  # a conversation id, or a user id (chat.postMessage opens the DM)
    label: str  # "Luca Pradovera" or "#launch"
    text: str
    made: float
    sent: bool = False


@dataclass(frozen=True)
class Target:
    channel: str
    label: str


_drafts: dict[str, Draft] = {}
_ids = itertools.count(1)
_lock = threading.RLock()
_directory: dict[str, Any] = {}  # users and conversations, with when they were read


def _fetch(post: bool = False) -> slack.Fetch:
    token = load().slack.token
    if not token:
        raise ToolError("Slack isn't set up ([slack] token in Scout's config).")
    return slack.fetcher(token, post=post)


def _people(get: slack.Fetch) -> list[dict[str, Any]]:
    users = slack._pages(get, "users.list", {"limit": slack.PAGE}, "members", 50)
    return [u for u in users if not u.get("deleted") and not u.get("is_bot") and u.get("id") != "USLACKBOT"]


def directory(
    get: slack.Fetch, now: float | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(people, the user's conversations), read at most every DIRECTORY_TTL_S."""
    now = time.time() if now is None else now
    if _directory and now - float(_directory["at"]) < DIRECTORY_TTL_S:
        return _directory["people"], _directory["convs"]
    people, convs = _people(get), slack.conversations(get)
    _directory.update(people=people, convs=convs, at=now)
    return people, convs


def _names(u: dict[str, Any]) -> list[str]:
    p = u.get("profile") or {}
    raw = (p.get("display_name"), p.get("real_name"), u.get("real_name"), u.get("name"))
    return [str(n).lower() for n in raw if n]


def _label(u: dict[str, Any]) -> str:
    p = u.get("profile") or {}
    return str(p.get("real_name") or u.get("real_name") or p.get("display_name") or u.get("name") or u["id"])


def resolve(people: list[dict[str, Any]], convs: list[dict[str, Any]], to: str) -> list[Target]:
    """Who or where `to` means: a channel you're in by name ("#launch" means only a
    channel), else people by a full name, else by first name."""
    raw = " ".join(to.split())
    q = raw.lstrip("#@").lower()
    if not q:
        return []
    chans = [
        Target(str(c["id"]), f"#{c['name']}")
        for c in convs
        if not c.get("is_im") and not c.get("is_mpim") and str(c.get("name") or "").lower() == q
    ]
    if raw.startswith("#"):
        return chans
    found = [u for u in people if u.get("id") == raw.upper() or q in _names(u)]
    if not found and " " not in q:
        found = [u for u in people if any(n.split()[0] == q for n in _names(u))]
    ims = {str(c.get("user")): str(c["id"]) for c in convs if c.get("is_im")}
    return chans + [Target(ims.get(str(u["id"]), str(u["id"])), _label(u)) for u in found]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


async def draft(a: dict[str, Any]) -> str:
    to = str(a.get("to") or "").strip()
    text = str(a.get("text") or "").strip()
    if not to:
        raise ToolError("to is required: a person's name or a channel you're in")
    if not text:
        raise ToolError("text is required")
    if len(text) > MAX_CHARS:
        raise ToolError(f"text is too long for one message (at most {MAX_CHARS} characters)")
    get = _fetch()
    people, convs = await asyncio.to_thread(directory, get)
    found = resolve(people, convs, to)
    if not found:
        raise ToolError(f"Nobody on Slack, and no channel you're in, is called {to}.")
    if len(found) > 1:
        names = ", ".join(t.label for t in found[:8])
        return (
            f"More than one match for {to}: {names}. Ask the user which one, then call "
            "slack_draft again with that full name or channel. Nothing was sent."
        )
    t = found[0]
    with _lock:
        now = time.time()
        for k in [k for k, d in _drafts.items() if now - d.made > DRAFT_TTL_S]:
            del _drafts[k]
        d = Draft(f"s{next(_ids)}", t.channel, t.label, text, now)
        _drafts[d.id] = d
    return (
        f"Draft {d.id}: to {d.label} on Slack: {d.text}\n"
        f"Not sent. To send it, call slack_send with draft {d.id}; the user confirms it first."
    )


def _draft(a: dict[str, Any]) -> Draft | None:
    with _lock:
        d = _drafts.get(str(a.get("draft") or "").strip())
    if d is None or d.sent or time.time() - d.made > DRAFT_TTL_S:
        return None
    return d


def describe(a: dict[str, Any]) -> tuple[str, str]:
    """The confirmation: (spoken question with the text read back, the page's detail)."""
    d = _draft(a)
    if d is None:
        return "Send a Slack message?", f"send Slack draft {a.get('draft')!r} (no such draft)"
    return (
        f"Send to {d.label} on Slack: {_clip(d.text, SPOKEN_CHARS)}?",
        f"send Slack message to {d.label} ({d.channel})\n\n{d.text}",
    )


def _escape(text: str) -> str:
    """Slack's three control characters, so the text goes out as written."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send(a: dict[str, Any]) -> str:
    """Only ever after the user's yes (asks=True): post the draft, once."""
    with _lock:
        d = _draft(a)
        if d is None:
            raise ToolError(
                "There's no such draft (or it was sent or is too old): make it again with slack_draft."
            )
        d.sent = True  # taken now: a second call can't post it twice
    post = _fetch(post=True)
    try:
        out = await asyncio.to_thread(
            post, "chat.postMessage", {"channel": d.channel, "text": _escape(d.text)}
        )
    except slack.SlackError as exc:
        code = str(exc).split(": ", 1)[-1]
        if not _CODE.fullmatch(code):  # no answer from Slack: it may have gone out
            raise ToolError(
                f"Slack didn't answer ({code}), so it may have been sent: check Slack before sending it again."
            ) from None
        with _lock:
            d.sent = False  # Slack refused it: nothing went out
        raise ToolError(f"Slack didn't take it: {code}") from None
    return f"Sent to {d.label} on Slack (ref slack:{out.get('channel') or d.channel}:{out.get('ts')})."
