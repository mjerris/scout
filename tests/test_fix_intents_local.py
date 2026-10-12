"""Review fixes in tier 0 (local_intents): clock words, weather places, routes, play and
pause, unknown lists, notes, and days across a daylight saving change."""

import asyncio
import contextlib
import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_local_intents import Calls, ctx

from scout import briefing, local_intents, mac, maps, weather
from scout.mac import ToolError


def _answer(text: str, c: local_intents.Context) -> str | None:
    return asyncio.run(local_intents.answer(text, c))


@pytest.mark.parametrize(("text", "hour"), [("3pm", 15), ("11am", 11), ("3 pm", 15), ("three", 15)])
def test_parse_clock_takes_am_pm_without_a_space(text: str, hour: int) -> None:
    assert local_intents.parse_clock(text) == dt.time(hour)


def test_parse_clock_rejects_an_unknown_word() -> None:
    assert local_intents.parse_clock("soon") is None


def test_am_i_free_at_3pm_is_answered_locally() -> None:
    assert _answer("Am I free at 3pm tomorrow?", ctx(Calls())) == "Yes, you're free at 3 PM tomorrow."


def _weather_ctx(seen: list[str]) -> local_intents.Context:
    c = ctx(Calls())

    async def weather(place: str) -> dict[str, Any]:
        seen.append(place)
        raise ToolError("stop here")

    c.weather = weather
    return c


@pytest.mark.parametrize(
    "text",
    [
        "What's the weather like in the morning?",
        "What's the forecast for the weekend?",
        "what's the weather for the week",
        "what's the temperature in here",
    ],
)
def test_time_words_and_here_are_not_weather_places(text: str) -> None:
    seen: list[str] = []
    with contextlib.suppress(ToolError):
        _answer(text, _weather_ctx(seen))
    assert seen in ([], [""])  # home's weather or Claude, never a place called "the"


def test_weather_in_the_morning_is_the_morning_forecast_at_home(monkeypatch: pytest.MonkeyPatch) -> None:
    said: list[str] = []

    def speak(data: dict[str, Any], when: str) -> str:
        said.append(when)
        return "ok"

    monkeypatch.setattr(weather, "speak", speak)
    c = ctx(Calls())

    async def forecast(place: str) -> dict[str, Any]:
        assert place == ""
        return {}

    c.weather = forecast
    assert _answer("What's the weather like in the morning?", c) == "ok" and said == ["morning"]


@pytest.mark.parametrize(
    "text",
    [
        "How far is it to Boston?",
        "how far is it from here to Boston",
        "how far along is the project to launch",
    ],
)
def test_route_needs_a_real_origin(text: str) -> None:
    c = ctx(Calls())

    async def route(a: str, b: str) -> dict[str, Any]:
        raise AssertionError(f"routed from {a!r} to {b!r}")

    c.route = route
    assert _answer(text, c) is None  # Claude


def test_route_with_an_origin_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []
    c = ctx(Calls())

    async def route(a: str, b: str) -> dict[str, Any]:
        seen.append((a, b))
        return {}

    c.route = route
    monkeypatch.setattr(maps, "speak_route", lambda r: "far")
    assert _answer("how far is it from Albany to Boston", c) == "far" and seen == [("albany", "boston")]


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("resume the music", "play"),
        ("play", "play"),
        ("pause", "pause"),
        ("unpause it", "play"),
        ("pause the video", "play_pause"),
    ],
)
def test_play_and_pause_are_explicit(text: str, action: str) -> None:
    calls = Calls()
    assert _answer(text, ctx(calls)) == ""
    assert calls.media == [action]


def test_pause_never_presses_space_in_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def is_running(app: str) -> bool:
        return app == "Music"

    async def osascript(script: str, *args: str) -> str:
        ran.append(script)
        return ""

    monkeypatch.setattr(mac, "is_running", is_running)
    monkeypatch.setattr(mac, "osascript", osascript)
    assert asyncio.run(local_intents._media("pause")) == "Music: pause."
    assert ran == ['tell application "Music" to pause']

    async def nothing_running(app: str) -> bool:
        return False

    monkeypatch.setattr(mac, "is_running", nothing_running)
    with pytest.raises(ToolError):
        asyncio.run(local_intents._media("play"))
    assert len(ran) == 1


@pytest.mark.parametrize(
    "error", ["There's no guest list. Lists: Reminders.", "no reminder list matches guest"]
)
def test_an_unknown_list_goes_to_claude(error: str) -> None:
    calls = Calls()
    calls.reminder_reply = ToolError(error)
    assert _answer("check the guest list", ctx(calls)) is None


def test_note_drops_please_with_a_comma() -> None:
    saved: list[str] = []
    c = ctx(Calls())

    async def note_add(text: str) -> dict[str, Any]:
        saved.append(text)
        return {}

    c.note_add = note_add
    assert _answer("Please, take a note that the plumber comes Tuesday.", c) == "Noted."
    assert saved == ["the plumber comes Tuesday."]


def test_another_day_gets_its_own_daylight_saving_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    now = briefing.local_now()
    assert isinstance(now.tzinfo, ZoneInfo)
    # Fri Oct 30 2026 is EDT; Mon Nov 2 is EST. A 3 PM meeting Monday clashes with "Monday at 3".
    friday = dt.datetime(2026, 10, 30, 12, 0, tzinfo=now.tzinfo)
    calls = Calls()
    calls.event_reply = {
        "events": [
            {"title": "Review", "all_day": False, "start": "2026-11-02T15:00:00-05:00", "end": "2026-11-02T16:00:00-05:00"}
        ]
    }  # fmt: skip
    c = ctx(calls)
    c.now = lambda: friday
    assert _answer("am I free Monday at 3", c) == "No, you have Review from 3 to 4 PM on Monday."


def test_context_now_defaults_to_a_real_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    now = local_intents.Context(None).now()
    winter = dt.datetime.combine(dt.date(2026, 1, 15), dt.time(12), now.tzinfo)
    summer = dt.datetime.combine(dt.date(2026, 7, 15), dt.time(12), now.tzinfo)
    assert winter.utcoffset() == dt.timedelta(hours=-5) and summer.utcoffset() == dt.timedelta(hours=-4)
