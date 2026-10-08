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

    return local_intents.Context(
        timers or Timers(lambda label: None), now=lambda: NOW, volume=volume, media=media, events=events
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
    ],
)
def test_anything_else_goes_to_claude(text: str) -> None:
    calls = Calls()
    assert say(text, calls) is None
    assert calls.volume == [] and calls.media == [] and calls.events == []


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
