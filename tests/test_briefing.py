"""The briefing: what it says from each source, how it copes with a missing one,
and the daily schedule."""

import asyncio
import datetime as dt
from typing import Any

import pytest

from scout import briefing
from scout.mac import ToolError

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 8, 7, 30, tzinfo=TZ)
HOURS = (dt.time(9), dt.time(17))


def test_sender_names() -> None:
    assert briefing.sender_name("Alice Smith <alice@x.com>") == "Alice Smith"
    assert briefing.sender_name('"Bob" <bob@x.com>') == "Bob"
    assert briefing.sender_name("carol@x.com") == "carol"
    assert briefing.sender_name("<dave@x.com>") == "dave"


def test_mail_is_counted_by_sender_only() -> None:
    msgs = [
        {"sender": "GitHub <n@github.com>", "subject": "secret", "read": False},
        {"sender": "GitHub <n@github.com>", "read": False},
        {"sender": "Alice <a@x.com>", "read": False},
        {"sender": "Bob <b@x.com>", "read": False},
        {"sender": "Carol <c@x.com>", "read": True},
    ]
    out = briefing.mail_part(msgs)
    assert out == "You have 4 unread emails, mostly from GitHub, Alice, and Bob."
    assert "secret" not in out
    assert briefing.mail_part([]) == "No unread email."
    many = [{"sender": "x@y.com", "read": False}] * 50
    assert briefing.mail_part(many) == "You have 50 or more unread emails, mostly from x."


def test_reminders_part() -> None:
    items = [{"title": f"r{i}", "due": "2026-10-07"} for i in range(7)]
    assert (
        briefing.reminders_part(items, NOW)
        == "You have 7 reminders due, 7 overdue: r0, r1, r2, r3, r4, and 2 more."
    )
    assert briefing.reminders_part([{"title": "Bins", "due": "2026-10-09"}], NOW) == "No reminders due today."


def test_a_failing_source_is_named_not_fatal() -> None:
    async def events(start: str, end: str) -> dict[str, Any]:
        return {"events": []}

    async def reminders(*a: Any) -> dict[str, Any]:
        raise ToolError("Reminders access is denied.")

    async def mail(*a: Any) -> list[dict[str, Any]]:
        raise ToolError("Not allowed to control Mail yet.")

    out = asyncio.run(briefing.build(NOW, events, reminders, mail, HOURS))
    assert out == (
        "Good morning. It's Thursday, October 8. Today your calendar is clear. "
        "I couldn't check reminders. I couldn't check mail."
    )


def test_next_briefing_time() -> None:
    assert briefing.next_at("07:30", NOW - dt.timedelta(minutes=1)) == NOW
    assert briefing.next_at("07:30", NOW) == NOW + dt.timedelta(days=1)
    with pytest.raises(ValueError, match=r"briefing\.at"):
        briefing.next_at("7.30am", NOW)


def test_daily_runs_once_a_day_even_when_woken_early() -> None:
    clock = [NOW - dt.timedelta(hours=1)]
    slept: list[float] = []
    ran: list[dt.datetime] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) > 3:
            raise asyncio.CancelledError
        clock[0] += dt.timedelta(seconds=seconds - 0.5)  # wakes half a second early

    async def run() -> None:
        ran.append(clock[0])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(briefing.daily("07:30", run, now=lambda: clock[0], sleep=sleep))
    assert slept[0] == 3600 and len(ran) == 3
    assert [t.date() for t in ran] == [NOW.date() + dt.timedelta(days=i) for i in range(3)]


def test_a_bad_briefing_time_turns_the_schedule_off() -> None:
    async def run() -> None:
        raise AssertionError("must not run")

    asyncio.run(briefing.daily("half seven", run))  # returns instead of looping


def test_the_scheduled_briefing_is_spoken_by_the_assistant() -> None:
    from test_routing import make

    async def go() -> tuple[list[str], tuple[str, str] | None]:
        r = make()

        async def events(start: str, end: str) -> dict[str, Any]:
            return {"events": []}

        async def reminders(*a: Any) -> dict[str, Any]:
            return {"reminders": []}

        async def mail(*a: Any) -> list[dict[str, Any]]:
            return []

        c = r.a._local
        c.now, c.events, c.reminders, c.mail, c.hours = lambda: NOW, events, reminders, mail, lambda: HOURS
        await r.a._announce_briefing()
        return r.spk.said, r.a.last_spoken

    said, last = asyncio.run(go())
    text = (
        "Good morning. It's Thursday, October 8. Today your calendar is clear. "
        "No reminders due today. No unread email."
    )
    assert said == [text] and last == ("Scout", text)
