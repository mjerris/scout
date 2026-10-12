"""The pulse: a check every few minutes, in plain code, that speaks only when something
needs the user. No AI, nothing sent anywhere.

    an event about to start      "Heads up: standup in 10 minutes."
    a timed reminder coming due  "Reminder: call Sam, due at 3:00 PM."
    today's overlapping events   "Today, Dentist overlaps Team sync at 3:00 PM." (once, morning)

Each thing is said once. Quiet hours skip the check altogether, so what's still ahead
when they end (the morning's overlaps, an event just after) is said then. Checks that
can't run (no calendar access) are skipped quietly.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import calendar_mac, freebusy, reminders_mac
from .freebusy import clock
from .mac import ToolError
from .starlink import in_quiet_hours

log = logging.getLogger(__name__)

Events = Callable[[str, str], Awaitable[dict[str, Any]]]
Reminders = Callable[[], Awaitable[dict[str, Any]]]


async def _events(start: str, end: str) -> dict[str, Any]:
    return await calendar_mac.event_data(start, end)


async def _reminders() -> dict[str, Any]:
    return await reminders_mac.reminder_data()


@dataclass
class Pulse:
    lead_min: float = 10.0  # announce an event this long before it starts
    every_min: float = 15.0
    events: Events = _events
    reminders: Reminders = _reminders
    _said: set[str] = field(default_factory=set)
    _conflicts_day: dt.date | None = None

    async def check(self, now: dt.datetime) -> list[str]:
        """What to say now (each thing once)."""
        out: list[str] = []
        try:
            day_end = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            data = await self.events(
                now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), day_end.isoformat()
            )
            spans = freebusy.timed(data.get("events", []))
            out += self._soon(spans, now)
            out += self._conflicts(spans, now)
        except ToolError as exc:
            log.debug("pulse: calendar skipped (%s)", exc)
        try:
            out += self._due((await self.reminders()).get("reminders", []), now)
        except ToolError as exc:
            log.debug("pulse: reminders skipped (%s)", exc)
        return out

    def _once(self, key: str) -> bool:
        if key in self._said:
            return False
        self._said.add(key)
        return True

    def _soon(self, spans: list[freebusy.Span], now: dt.datetime) -> list[str]:
        out = []
        window = dt.timedelta(minutes=self.lead_min + self.every_min)
        for s in spans:
            if now <= s.start <= now + window and self._once(f"event:{s.titles[0]}:{s.start.isoformat()}"):
                mins = max(1, round((s.start - now).total_seconds() / 60))
                out.append(f"Heads up: {s.titles[0]} in {mins} minute{'s' * (mins != 1)}.")
        return out

    def _conflicts(self, spans: list[freebusy.Span], now: dt.datetime) -> list[str]:
        if self._conflicts_day == now.date():
            return []
        self._conflicts_day = now.date()
        ahead = [s for s in spans if s.end > now]
        clashes = [(a, b) for i, a in enumerate(ahead) for b in ahead[i + 1 :] if b.start < a.end]
        out = [
            f"Today, {a.titles[0]} overlaps {b.titles[0]} at {clock(b.start)}."
            for a, b in clashes
            if self._once(f"clash:{a.titles[0]}:{b.titles[0]}:{b.start.isoformat()}")
        ]
        return out[:3]

    def _due(self, items: list[dict[str, Any]], now: dt.datetime) -> list[str]:
        out = []
        window = dt.timedelta(minutes=self.every_min)
        for r in items:
            due = reminders_mac.due_time(r)
            if due is None or "T" not in str(r.get("due")) or r.get("completed"):
                continue  # date-only reminders are the briefing's
            if now - window < due <= now + window and self._once(f"reminder:{r.get('id')}:{r.get('due')}"):
                out.append(f"Reminder: {r.get('title') or 'a reminder'}, due at {clock(due)}.")
        return out


async def run(
    say: Callable[[str], Awaitable[None]],
    pulse: Pulse,
    quiet_hours: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
) -> None:
    """Check every pulse.every_min minutes, forever; say what matters outside quiet hours."""
    while True:
        try:
            t = now()
            # No check in quiet hours: checking marks things said, and they'd never be.
            if not in_quiet_hours(quiet_hours, t):
                for text in await pulse.check(t):
                    await say(text)
        except Exception:
            log.exception("pulse check failed")
        await asyncio.sleep(pulse.every_min * 60)
