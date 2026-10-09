"""Tier 0: everyday requests answered in plain code, and everything else left to Claude."""

import asyncio
import datetime as dt
from typing import Any

import pytest

from scout import local_intents
from scout.tools import Timers

NOW = dt.datetime(2026, 10, 8, 14, 37, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


class Calls:
    def __init__(self) -> None:
        self.volume: list[dict[str, Any]] = []
        self.media: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.event_reply: dict[str, Any] = {"events": [], "total": 0}
        self.reminders: list[tuple[Any, ...]] = []
        self.reminder_reply: dict[str, Any] | Exception = {"reminders": [], "total": 0}
        self.mail: list[tuple[Any, ...]] = []
        self.mail_reply: list[dict[str, Any]] = []


def ctx(calls: Calls, timers: Timers | None = None) -> local_intents.Context:
    async def volume(**kw: Any) -> str:
        calls.volume.append(kw)
        return "ok"

    async def media(action: str) -> str:
        calls.media.append(action)
        return "ok"

    async def events(start: str, end: str) -> dict[str, Any]:
        calls.events.append((start, end))
        return calls.event_reply

    async def reminders(*args: Any) -> dict[str, Any]:
        calls.reminders.append(args)
        if isinstance(calls.reminder_reply, Exception):
            raise calls.reminder_reply
        return calls.reminder_reply

    async def mail(*args: Any) -> list[dict[str, Any]]:
        calls.mail.append(args)
        return calls.mail_reply

    return local_intents.Context(
        timers or Timers(lambda label: None),
        now=lambda: NOW,
        volume=volume,
        media=media,
        events=events,
        reminders=reminders,
        mail=mail,
        hours=lambda: (dt.time(9), dt.time(17)),
    )


def say(text: str, calls: Calls | None = None, timers: Timers | None = None) -> str | None:
    async def go() -> str | None:
        return await local_intents.answer(text, ctx(calls or Calls(), timers))

    return asyncio.run(go())


@pytest.mark.parametrize(
    "text", ["What time is it?", "what's the time", "Can you tell me the time please?", "Time?"]
)
def test_time(text: str) -> None:
    assert say(text) == "It's 2:37 PM."


@pytest.mark.parametrize("text", ["What's the date?", "what day is it", "What's today's date?"])
def test_date(text: str) -> None:
    assert say(text) == "It's Thursday, October 8."


@pytest.mark.parametrize(
    ("text", "seconds", "spoken"),
    [
        ("Set a timer for 5 minutes.", 300, "Okay, timer for 5 minutes."),
        ("set a timer for ten minutes called pasta", 600, "Okay, pasta timer for 10 minutes."),
        ("Start a 90 second timer", 90, "Okay, timer for 1 minute and 30 seconds."),
        ("set a timer for an hour", 3600, "Okay, timer for 1 hour."),
    ],
)
def test_timers(text: str, seconds: int, spoken: str) -> None:
    async def go() -> tuple[str | None, list[str]]:
        t = Timers(lambda label: None)
        out = await local_intents.answer(text, ctx(Calls(), t))
        left = t.listing()
        t.cancel("all")
        return out, left

    out, left = asyncio.run(go())
    assert out == spoken and len(left) == 1


def test_timer_status_and_cancel() -> None:
    async def go() -> list[str | None]:
        t = Timers(lambda label: None)
        c = ctx(Calls(), t)
        return [
            await local_intents.answer("how much time is left", c),
            await local_intents.answer("set a timer for 2 minutes", c),
            await local_intents.answer("cancel the timer", c),
            await local_intents.answer("cancel the timer", c),
        ]

    out = asyncio.run(go())
    assert out[0] == "No timers are running."
    assert out[2] == "Cancelled." and out[3] == "No timers are running."


def test_volume_and_media() -> None:
    calls = Calls()
    assert say("turn it up", calls) == "Okay."
    assert say("Set the volume to 30.", calls) == "Volume 30."
    assert say("mute", calls) == "Okay."
    assert say("pause the music", calls) == ""  # the action is the answer
    assert say("next song", calls) == ""
    assert calls.volume == [{"change": 10}, {"level": 30}, {"mute": True}]
    assert calls.media == ["play_pause", "next"]
    assert say("set the volume to 300") == "Volume goes from 0 to 100."


def test_calendar_today_and_tomorrow() -> None:
    calls = Calls()
    calls.event_reply = {
        "events": [
            {"title": "JERRIS - OOO", "all_day": True, "start": "2026-10-01", "end": "2026-10-11"},
            {"title": "Team MARS Standup", "all_day": False, "start": "2026-10-08T04:00:00-04:00"},
        ]
    }
    assert say("What's on my calendar today?", calls) == (
        "Today you have JERRIS - OOO, all day, and Team MARS Standup at 4 AM."
    )
    assert calls.events[0][0].startswith("2026-10-08T00:00:00")
    calls.event_reply = {"events": []}
    assert say("what do I have tomorrow", calls) == "Nothing on your calendar tomorrow."
    assert calls.events[1][0].startswith("2026-10-09T00:00:00")


@pytest.mark.parametrize(
    "text",
    [
        "What time is it in Tokyo?",  # needs knowledge
        "what time is my dentist appointment",  # needs the calendar searched
        "set a timer for when the pasta is done",  # no duration
        "pause for a second and think about it",
        "play some jazz",  # which app, what music
        "what's on my calendar next week",  # not today/tomorrow: Claude handles ranges
        "turn up the heat",
        "tell me about the history of time",
        "next time remind me to call Sam",
        "add milk to my shopping list",  # a change: Claude's reminder_add asks first
        "remind me to call Sam at 5",
        "mark milk as done on my shopping list",
        "am I free to talk",
        "when is my free trial over",
        "what's on my mind",
    ],
)
def test_anything_else_goes_to_claude(text: str) -> None:
    calls = Calls()
    assert say(text, calls) is None
    assert calls.volume == [] and calls.media == [] and calls.events == [] and calls.reminders == []


def test_assistant_answers_locally_without_claude_and_tells_claude_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_routing import hear, make

    async def go() -> tuple[list[str], str]:
        r = make()

        async def no_claude(text: str) -> Any:
            raise AssertionError("tier 0 should have answered")
            yield  # pragma: no cover

        monkeypatch.setattr(r.a.brain, "ask", no_claude)
        await hear(r, "Hey Scout, what time is it?")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if r.spk.said:
                break
        r.a.cfg.claude.request_context = True
        return r.spk.said, await r.a._with_context("and tomorrow?")

    said, context = asyncio.run(go())
    assert said and said[0].startswith("It's ")
    assert "Just answered locally: 'what time is it?' -> \"It's" in context
    assert context.endswith("[/Context]\nand tomorrow?")


def ev(title: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
    return {"title": title, "all_day": False, "start": f"{start}-04:00", "end": f"{end}-04:00", **kw}


TOMORROW = [
    ev("Standup", "2026-10-09T09:30:00", "2026-10-09T10:00:00"),
    ev("Planning", "2026-10-09T11:00:00", "2026-10-09T12:30:00"),
    ev("Dentist", "2026-10-09T15:00:00", "2026-10-09T16:00:00"),
]


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("Am I free at 3 tomorrow?", "No, you have Dentist from 3 to 4 PM tomorrow."),
        ("am I free tomorrow at 3:30 pm", "No, you have Dentist from 3 to 4 PM tomorrow."),
        ("Am I free tomorrow at 2?", "Yes, you're free at 2 PM tomorrow. Dentist starts at 3 PM."),
        (
            "are we free on Friday at ten thirty",
            "Yes, you're free at 10:30 AM on Friday. Planning starts at 11 AM.",
        ),
        ("am I free tomorrow at 9 pm", "Yes, you're free at 9 PM tomorrow."),
        ("am I available tomorrow at noon", "No, you have Planning from 11 AM to 12:30 PM tomorrow."),
    ],
)
def test_free_at(text: str, spoken: str) -> None:
    calls = Calls()
    calls.event_reply = {"events": TOMORROW}
    assert say(text, calls) == spoken
    assert calls.events[0][0].startswith("2026-10-09T00:00:00")


def test_free_at_today_and_a_weekday_reads_that_day() -> None:
    calls = Calls()
    assert say("am I free at 4", calls) == "Yes, you're free at 4 PM."
    assert (
        say("am I free Thursday at 4", calls) == "Yes, you're free at 4 PM on Thursday."
    )  # today is Thursday
    assert say("am I free on monday at 9 am", calls) == "Yes, you're free at 9 AM on Monday."
    assert [c[0][:10] for c in calls.events] == ["2026-10-08", "2026-10-08", "2026-10-12"]


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        (
            "When am I free tomorrow?",
            "Tomorrow you're free until 9:30 AM, 10 to 11 AM, 12:30 to 3 PM, and after 4 PM.",
        ),
        (
            "what's my first free hour tomorrow",
            "Tomorrow your first free hour starts at 10 AM; you're free until 11 AM.",
        ),
        (
            "my first free half hour tomorrow",
            "Tomorrow your first free half hour starts at 9 AM; you're free until 9:30 AM.",
        ),
        (
            "How busy is my day tomorrow?",
            "Tomorrow you have 3 events, 3 hours in all, from 9:30 AM to 4 PM. "
            "Your longest free stretch is 12:30 to 3 PM.",
        ),
        ("how busy am I tomorrow", None),  # same answer, checked below
    ],
)
def test_free_time_tomorrow(text: str, spoken: str | None) -> None:
    calls = Calls()
    calls.event_reply = {"events": TOMORROW}
    out = say(text, calls)
    assert out == (spoken or say("how busy is my day tomorrow", calls))


def test_free_time_today_starts_now() -> None:
    calls = Calls()  # NOW is 2:37 PM
    calls.event_reply = {"events": [ev("Dentist", "2026-10-08T15:00:00", "2026-10-08T16:00:00")]}
    assert say("when am I free", calls) == "Today you're free until 3 PM and after 4 PM."
    assert say("what's my next free hour", calls) == (
        "Today your first free hour starts at 4 PM; you're free until 5 PM."
    )
    calls.event_reply = {"events": []}
    assert say("when am I free today", calls) == "You're free for the rest of the working day, until 5 PM."
    assert say("when am I free on Monday", calls) == "On Monday you're free all day, from 9 AM to 5 PM."
    assert say("how busy is today", calls) == "Today your calendar is clear."


def test_reminder_lists_are_read_aloud() -> None:
    calls = Calls()
    calls.reminder_reply = {
        "reminders": [{"title": t, "list": "Shopping"} for t in ("milk", "eggs", "bread")]
    }
    assert say("What's on my shopping list?", calls) == "Your shopping list has milk, eggs, and bread."
    assert say("read me my to-do list", calls) == "Your to-do list has milk, eggs, and bread."
    assert calls.reminders == [("shopping",), ("to-do",)]
    calls.reminder_reply = {"reminders": []}
    assert say("what's on the grocery list", calls) == "Your grocery list is empty."
    from scout.mac import ToolError

    calls.reminder_reply = ToolError("no reminder list matches bucket")
    assert say("what's on my bucket list", calls) == "You don't have a reminder list called bucket."


def test_reminders_due() -> None:
    calls = Calls()
    calls.reminder_reply = {
        "reminders": [
            {"title": "Pay rent", "due": "2026-10-07"},
            {"title": "Call Sam", "due": "2026-10-08T17:00:00-04:00"},
            {"title": "Bins", "due": "2026-10-09"},
            {"title": "Milk"},
        ]
    }
    assert (
        say("what are my reminders today", calls)
        == "You have 2 reminders due, 1 overdue: Pay rent and Call Sam."
    )
    assert calls.reminders[-1][2].startswith("2026-10-09T00:00:00")
    assert say("what's due tomorrow", calls) == "Tomorrow you have 1 reminder due: Bins."
    assert say("what reminders do I have", calls) == (
        "You have 4 open reminders, 1 overdue: Pay rent, Call Sam, Bins, and Milk."
    )
    calls.reminder_reply = {"reminders": []}
    assert say("any reminders today", calls) == "No reminders due today."


@pytest.mark.parametrize(
    "text", ["Brief me.", "morning briefing", "what's my day look like", "Give me my briefing"]
)
def test_briefing(text: str) -> None:
    calls = Calls()
    calls.event_reply = {"events": [ev("Dentist", "2026-10-08T15:00:00", "2026-10-08T16:00:00")]}
    calls.reminder_reply = {"reminders": [{"title": "Call Sam", "due": "2026-10-08T17:00:00-04:00"}]}
    calls.mail_reply = [{"sender": "GitHub <noreply@github.com>", "read": False}]
    assert say(text, calls) == (
        "Good afternoon. It's Thursday, October 8. Today you have 1 event, an hour in all, from 3 to 4 PM. "
        "Your longest free stretch is 4 to 5 PM. Next up: Dentist at 3 PM. "
        "You have 1 reminder due today: Call Sam. You have 1 unread email, from GitHub."
    )
    assert calls.mail == [(50, True)]


def test_free_answer_mentions_all_day_blocks() -> None:
    calls = Calls()
    calls.event_reply = {
        "events": [{"title": "JERRIS - OOO", "all_day": True, "start": "2026-10-01", "end": "2026-10-11"}]
    }
    out = say("am I free tomorrow at 3", calls)
    assert (
        out == "Yes, you're free at 3 PM tomorrow. You have JERRIS - OOO all day."
    )  # heard live: OOO went unmentioned
