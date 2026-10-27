"""Catching up on a Slack conversation, on request: "what are they saying?" after Scout
said "Nina and Omar are talking in your group chat" (slack_alerts.py). Announcements
only ever say who; this reads what was said, when asked.

The conversation is the one Scout last spoke about (its refs, "that"), or one named by a
ref. Its messages come from Scout's own store of Slack (slack.py keeps them), from just
after your last message there, so it's what you missed; with no message of yours in the
last CATCH_UP_DAYS days, the newest MAX_LINES lines.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from . import index, refs
from .mac import ToolError
from .taskfarm import slack_me

CATCH_UP_DAYS = 7
MAX_LINES = 60
MAX_HITS = 5000


def _channel(ref: str) -> str:
    """The conversation of a Slack item id: slack:<channel>:<ts>[#part]."""
    parts = ref.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "slack" else ""


def _ts(h: index.Hit) -> float:
    try:
        return float(h.meta.get("last_ts") or h.meta.get("ts") or h.ts)
    except (TypeError, ValueError):
        return h.ts


def lines_since_you(hits: list[index.Hit], me: str) -> tuple[list[str], float | None]:
    """The conversation's lines after your last one, oldest first (at most MAX_LINES), and
    when you last wrote there (None: not in what's kept)."""
    hits = sorted(hits, key=lambda h: (_ts(h), h.ts, int(h.meta.get("part") or 0)))
    out: list[tuple[str, bool]] = []
    yours: float | None = None
    for h in hits:
        by = h.meta.get("lines") or []
        for i, text in enumerate(h.body.split("\n")):
            mine = bool(me) and i < len(by) and bool(by[i]) and str(by[i][0]) == me
            out.append((text, mine))
            if mine:
                yours = _ts(h)
    last_mine = max((i for i, (_, mine) in enumerate(out) if mine), default=-1)
    return [t for t, _ in out[last_mine + 1 :]][-MAX_LINES:], yours


async def catch_up(a: dict[str, Any]) -> str:
    picked = await refs.pick(str(a.get("ref") or ""), str(a.get("about") or ""))
    channel = next((c for c in map(_channel, picked) if c), "")
    if not channel:
        raise ToolError(
            "nothing said recently is from Slack: give a Slack ref (slack:...) from the request "
            "context or local_search"
        )
    store = refs._store()
    since = time.time() - CATCH_UP_DAYS * 86400
    hits = await asyncio.to_thread(store.recent, ["slack"], MAX_HITS, since)
    hits = [h for h in hits if str(h.meta.get("channel") or "") == channel]
    if not hits:
        return f"No messages from that conversation in the last {CATCH_UP_DAYS} days are in Scout's store."
    me = await asyncio.to_thread(slack_me)
    lines, yours = lines_since_you(hits, me.id)
    title = str(hits[0].meta.get("conversation") or hits[0].title)
    if yours is None:
        head = (
            f"{title}: the newest {len(lines)} messages (you haven't written there in {CATCH_UP_DAYS} days)."
        )
    elif not lines:
        return f"{title}: nothing new since your last message there."
    else:
        at = dt.datetime.fromtimestamp(yours).astimezone().strftime("%a %-I:%M %p")
        head = f"{title}: {len(lines)} messages since your last one there ({at})."
    return "\n".join([head, *lines])
