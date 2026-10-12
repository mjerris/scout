"""The pulse: an event about to start, a timed reminder coming due, and today's
overlapping events, each said once; nothing in quiet hours; missing access skipped."""

import asyncio
import datetime as dt
from typing import Any

from scout import pulse
from scout.mac import ToolError

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 12, 14, 52, tzinfo=TZ)


def ev(title: str, h: int, m: int, mins: int = 30, **kw: Any) -> dict[str, Any]:
    s = dt.datetime(2026, 10, 12, h, m, tzinfo=TZ)
    return {"title": title, "start": s.isoformat(), "end": (s + dt.timedelta(minutes=mins)).isoformat(), **kw}


EVENTS = [
    ev("Standup", 15, 0, 15),
    ev("Dentist", 16, 0, 60),
    ev("Team sync", 16, 30),
    ev("Holiday", 0, 0, 1440, all_day=True),
]
REMINDERS = [
    {"id": "r1", "title": "Call Sam", "due": "2026-10-12T15:00:00-04:00"},
    {"id": "r2", "title": "Buy stamps", "due": "2026-10-12"},
    {"id": "r3", "title": "Later thing", "due": "2026-10-12T19:00:00-04:00"},
]


def make(events: Any = None, reminders: Any = None) -> pulse.Pulse:
    async def e(start: str, end: str) -> dict[str, Any]:
        if events is ToolError:
            raise ToolError("Calendar access is denied.")
        return {"events": events if events is not None else EVENTS}

    async def r() -> dict[str, Any]:
        return {"reminders": reminders if reminders is not None else REMINDERS}

    return pulse.Pulse(10, 15, e, r)


def test_says_what_matters_once() -> None:
    p = make()
    first = asyncio.run(p.check(NOW))
    assert first == [
        "Heads up: Standup in 8 minutes.",
        "Today, Dentist overlaps Team sync at 4:30 PM.",
        "Reminder: Call Sam, due at 3 PM.",
    ]
    assert asyncio.run(p.check(NOW + dt.timedelta(minutes=5))) == []  # each said once
    later = asyncio.run(p.check(dt.datetime(2026, 10, 12, 15, 40, tzinfo=TZ)))
    assert later == ["Heads up: Dentist in 20 minutes."]


def test_missing_calendar_access_is_skipped() -> None:
    assert asyncio.run(make(events=ToolError).check(NOW)) == ["Reminder: Call Sam, due at 3 PM."]


def test_quiet_hours_only_log() -> None:
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    p = make()
    night = dt.datetime(2026, 10, 12, 23, 0, tzinfo=TZ)

    async def go() -> None:
        task = asyncio.create_task(pulse.run(say, p, "22:00-08:00", now=lambda: night))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert said == []
