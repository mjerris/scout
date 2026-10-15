"""The pulse: a check every few minutes, in plain code, that speaks only when something
needs the user. No AI, nothing sent anywhere.

    an event about to start      "Heads up: standup in 10 minutes."
    a timed reminder coming due  "Reminder: call Sam, due at 3:00 PM."
    today's overlapping events   "Today, Dentist overlaps Team sync at 3:00 PM." (once, morning)
    tasks due today              "Due today: send Pat the budget." (once, morning)
    what others owe, overdue     "Pat owes you the vendor quote, overdue since October 9;
                                  maybe follow up." (once a day)
    a sync failing or stalled    "Mail sync has been failing since 9 this morning." (once a
                                  day per source, after an hour of failing; syncstate.py)

Each thing is said once. Quiet hours skip the check altogether, so what's still ahead
when they end (the morning's overlaps and tasks, an event just after) is said then.
Checks that can't run (no calendar access) are skipped quietly.

Between checks it sleeps its interval, or less when an event's heads-up falls sooner
(the event's start minus the lead), so the heads-up comes on time without polling more.
Task titles are spoken here only, never sent to Claude. Each sentence about tasks
carries their ids (refs.Spoken), so "give me a link to that" right after it knows which.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import calendar_mac, freebusy, refs, reminders_mac, syncstate, tasks
from .freebusy import clock
from .mac import ToolError
from .starlink import in_quiet_hours

log = logging.getLogger(__name__)

Events = Callable[[str, str], Awaitable[dict[str, Any]]]
Reminders = Callable[[], Awaitable[dict[str, Any]]]
Tasks = Callable[[], Awaitable[list[tasks.Task]]]
Syncs = Callable[[], list[syncstate.Status]]


async def _events(start: str, end: str) -> dict[str, Any]:
    return await calendar_mac.event_data(start, end)


async def _reminders() -> dict[str, Any]:
    return await reminders_mac.reminder_data()


async def _tasks() -> list[tasks.Task]:
    return await asyncio.to_thread(tasks.all_tasks)


_SLACK = dt.timedelta(seconds=30)  # a wakeup this early still counts as on time


@dataclass
class Pulse:
    lead_min: float = 10.0  # announce an event this long before it starts
    every_min: float = 15.0
    events: Events = _events
    reminders: Reminders = _reminders
    tasks: Tasks = _tasks
    sync: Syncs = syncstate.statuses
    _said: set[str] = field(default_factory=set)
    _conflicts_day: dt.date | None = None
    _tasks_day: dt.date | None = None
    _starts: list[dt.datetime] = field(default_factory=list)  # today's event starts, last check

    async def check(self, now: dt.datetime) -> list[str]:
        """What to say now (each thing once)."""
        out: list[str] = []
        try:
            day_end = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            data = await self.events(
                now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), day_end.isoformat()
            )
            spans = freebusy.timed(data.get("events", []))
            self._starts = [s.start for s in spans if s.start > now]
            out += self._soon(spans, now)
            out += self._conflicts(spans, now)
        except ToolError as exc:
            self._starts = []
            log.debug("pulse: calendar skipped (%s)", exc)
        try:
            out += self._due((await self.reminders()).get("reminders", []), now)
        except ToolError as exc:
            log.debug("pulse: reminders skipped (%s)", exc)
        out += await self._task_day(now)
        out += self._syncs(now)
        return out

    def _syncs(self, now: dt.datetime) -> list[str]:
        """A sync failing for an hour or stalled: said once a day per source."""
        try:
            found = syncstate.alerts(self.sync(), now)
        except Exception as exc:  # an unreadable status file: nothing to say
            log.info("pulse: sync status skipped (%s)", exc)
            return []
        return [text for source, text in found if self._once(f"sync:{source}:{now.date()}")]

    def wait(self, now: dt.datetime) -> float:
        """Seconds until the next check: the interval, or sooner when an event's heads-up
        (its start minus the lead) comes first."""
        every = self.every_min * 60
        lead = dt.timedelta(minutes=self.lead_min)
        due = [s - lead for s in self._starts if s - lead > now + _SLACK]
        if not due:
            return every
        return max(1.0, min(every, (min(due) - now).total_seconds()))

    def _once(self, key: str) -> bool:
        if key in self._said:
            return False
        self._said.add(key)
        return True

    def _soon(self, spans: list[freebusy.Span], now: dt.datetime) -> list[str]:
        out = []
        # Only within the lead: the check wakes up for each heads-up (wait()).
        window = dt.timedelta(minutes=self.lead_min) + _SLACK
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

    async def _task_day(self, now: dt.datetime) -> list[str]:
        """Once a day, at the first check (after quiet hours): your tasks due today, and
        what others owe you that's overdue (a follow-up). Each task once a day."""
        today = now.date()
        if self._tasks_day == today:
            return []
        try:
            ts = [t for t in await self.tasks() if t.status == "open"]
        except Exception as exc:  # the store is busy or broken: try again next check
            log.info("pulse: tasks skipped (%s)", exc)
            return []
        self._tasks_day = today
        out: list[str] = []
        due = [t for t in sorted(ts, key=lambda t: t.n) if t.mine and t.due() == today]
        due = [t for t in due if self._once(f"task-due:{t.n}:{today}")]
        if due:
            titles = [tasks.spoken_title(t) for t in due[:3]]
            if len(due) > 3:
                titles.append(f"{tasks.count_word(len(due) - 3)} more")
            out.append(
                refs.Spoken(
                    f"Due today: {freebusy.join(titles)}.",
                    [f"task:{t.n}" for t in due[:3]],
                    any(tasks.found_from(t.fields) for t in due[:3]),
                )
            )
        late = sorted((t for t in ts if not t.mine and (d := t.due()) is not None and d < today),
                      key=lambda t: (t.due(), t.n))  # fmt: skip
        for t in late[:3]:
            d = t.due()
            if d is not None and self._once(f"task-owed:{t.n}:{today}"):
                out.append(
                    refs.Spoken(
                        f"{tasks.owes_you(t, tasks.spoken_title(t))}, {tasks.due_words(d, today)}; maybe follow up.",
                        [f"task:{t.n}"],
                        tasks.found_from(t.fields) is not None,
                    )
                )
        return out

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


def _quiet_left(spec: str, now: dt.datetime) -> float:
    """Seconds until quiet hours ("22:00-08:00") end, from a time inside them."""
    end = dt.time.fromisoformat(spec.split("-", 1)[1].strip())
    at = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if at <= now:
        at += dt.timedelta(days=1)
    return (at - now).total_seconds()


async def run(
    say: Callable[[str], Awaitable[None]],
    pulse: Pulse,
    quiet_hours: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
) -> None:
    """Check every pulse.every_min minutes (sooner for an event's heads-up), forever; say
    what matters outside quiet hours."""
    while True:
        wait = pulse.every_min * 60
        try:
            t = now()
            # No check in quiet hours: checking marks things said, and they'd never be.
            if not in_quiet_hours(quiet_hours, t):
                for text in await pulse.check(t):
                    await say(text)
                wait = pulse.wait(now())  # saying it took time
            else:  # check as they end, for what comes just after (an 8:05 meeting)
                wait = min(wait, max(1.0, _quiet_left(quiet_hours, t)))
        except Exception:
            log.exception("pulse check failed")
        await asyncio.sleep(wait)
