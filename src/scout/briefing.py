"""The briefing: one short spoken summary of the day, in plain code. Today's
calendar (how full it is, the first thing, the longest free stretch), reminders
due today or overdue, and how much unread mail there is and from whom. Mail is
only counted by sender: no subjects or message text.

Asked for ("brief me") through tier 0, or spoken every day at briefing.at."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from . import freebusy, reminders_mac
from .freebusy import clock, join
from .mac import ToolError

log = logging.getLogger(__name__)
_MAIL_COUNT = 50  # unread messages looked at (the inbox's last 30 days)


def sender_name(sender: str) -> str:
    """'Alice Smith <alice@x.com>' -> 'Alice Smith'; 'bob@x.com' -> 'bob'."""
    s = sender.strip()
    name = re.sub(r"\s*<[^>]*>\s*$", "", s).strip().strip('"').strip()
    if name and "@" not in name:
        return name
    m = re.search(r"([^<\s@]+)@", s)
    return m.group(1) if m else s or "someone"


def calendar_part(events: list[dict[str, Any]], now: dt.datetime, hours: freebusy.Hours) -> str:
    out = freebusy.day_load(events, now.date(), hours, now, "Today")
    upcoming = [s for s in freebusy.timed(events) if s.start >= now and s.start.date() == now.date()]
    if upcoming:
        out += f" Next up: {upcoming[0].titles[0]} at {clock(upcoming[0].start)}."
    return out


def reminders_part(items: list[dict[str, Any]], now: dt.datetime) -> str:
    due = [r for r in items if (t := reminders_mac.due_time(r)) and t.date() <= now.date()]
    if not due:
        return "No reminders due today."
    late = sum(reminders_mac.is_overdue(r, now) for r in due)
    titles = [r.get("title") or "an untitled reminder" for r in due[:5]]
    more = f", and {len(due) - 5} more" if len(due) > 5 else ""
    head = f"You have {len(due)} reminder{'s' * (len(due) != 1)} due"
    head += f", {late} overdue" if late else " today"
    return f"{head}: {join(titles) if not more else ', '.join(titles)}{more}."


def mail_part(messages: list[dict[str, Any]]) -> str:
    unread = [m for m in messages if not m.get("read")]
    if not unread:
        return "No unread email."
    n = len(unread)
    count = f"{n} or more" if len(messages) >= _MAIL_COUNT else str(n)
    top = [name for name, _ in Counter(sender_name(str(m.get("sender", ""))) for m in unread).most_common(3)]
    who = f", from {join(top)}" if n <= 3 else f", mostly from {join(top)}"
    return f"You have {count} unread email{'s' * (n != 1)}{who}."


def greeting(now: dt.datetime) -> str:
    part = "morning" if now.hour < 12 else "afternoon" if now.hour < 18 else "evening"
    return f"Good {part}. It's {now.strftime('%A, %B %-d')}."


async def build(
    now: dt.datetime,
    events: Callable[..., Awaitable[dict[str, Any]]],
    reminders: Callable[..., Awaitable[dict[str, Any]]],
    mail: Callable[..., Awaitable[list[dict[str, Any]]]],
    hours: freebusy.Hours,
) -> str:
    """The briefing text. A source that fails is named, not fatal."""
    tomorrow = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0), now.tzinfo)

    async def part(what: str, make: Callable[[], Awaitable[str]]) -> str:
        try:
            return await make()
        except ToolError as exc:
            log.info("briefing: no %s: %s", what, exc)
            return f"I couldn't check {what}."

    async def cal() -> str:
        return calendar_part(
            (await events(*freebusy.day_range(now.date(), now))).get("events", []), now, hours
        )

    async def rem() -> str:
        return reminders_part((await reminders(None, False, tomorrow.isoformat())).get("reminders", []), now)

    async def inbox() -> str:
        return mail_part(await mail(_MAIL_COUNT, True))

    parts = await asyncio.gather(part("your calendar", cal), part("reminders", rem), part("mail", inbox))
    return " ".join([greeting(now), *parts])


def next_at(at: str, now: dt.datetime) -> dt.datetime:
    """The next time the clock reads `at` ("07:30"), strictly after now."""
    t = freebusy.parse_clock(at, "briefing.at")
    when = dt.datetime.combine(now.date(), t, now.tzinfo)
    return when if when > now else when + dt.timedelta(days=1)


async def daily(
    at: str,
    run: Callable[[], Awaitable[None]],
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Call `run` every day at `at`, forever. A failed run is logged and skipped."""
    try:
        when = next_at(at, now())
    except ValueError as exc:
        log.error("no scheduled briefing: %s", exc)
        return
    while True:
        await sleep(max((when - now()).total_seconds(), 0.0))
        try:
            await run()
        except Exception:
            log.exception("scheduled briefing failed")
        when = next_at(at, max(now(), when))  # never twice for one day, even if woken early
