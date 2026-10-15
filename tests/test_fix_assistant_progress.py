"""Spoken progress during a long lookup ("Checking your mail."), with short fake delays."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from test_routing import Rig, hear, make, run

import scout.assistant as assistant_mod
from scout import local_intents
from scout.progress import STILL_WORKING, progress_phrase


def _rig(again: float = 0.0) -> Rig:
    r = make()
    r.a.cfg.tts.progress_delay_s = 0.05
    r.a.cfg.tts.progress_again_s = again
    r.a.cfg.claude.request_context = False  # no calendar or mail prefetch

    async def nothing() -> None:
        return None

    r.a.brain.interrupt = nothing  # type: ignore[method-assign]
    return r


def _slow_claude(monkeypatch: pytest.MonkeyPatch, r: Rig, tool: str | None, wait: float, answer: str) -> None:
    r.a.cfg.claude.local_first = False

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        if tool:
            yield "tool", (tool, {})
        await asyncio.sleep(wait)
        yield "text", answer

    monkeypatch.setattr(r.a.brain, "ask", ask)


async def _turn(r: Rig, text: str, speak: bool = True, client: str | None = None) -> list[str]:
    r.a.start_turn(text, speak, client)
    assert r.a._turn is not None
    await r.a._turn
    return r.spk.said


@pytest.mark.parametrize(
    ("what", "said"),
    [
        ("mcp__scout__mail_recent", "Checking your mail."),
        ("mcp__scout__mail_search", "Checking your mail."),
        ("mcp__scout__calendar_events", "Looking at your calendar."),
        ("mcp__scout__weather", "Getting the weather."),
        ("mcp__scout__drive_route", "Looking that up on the map."),
        ("mcp__scout__places_near", "Looking that up on the map."),
        ("WebSearch", "Searching."),
        ("WebFetch", "Searching."),
        ("mcp__scout__set_timer", "One moment."),
        ("what's the forecast for tomorrow", "Getting the weather."),
        ("how long is the drive to Moab", "Looking that up on the map."),
    ],
)
def test_progress_phrase_follows_the_tool(what: str, said: str) -> None:
    assert progress_phrase(what) == said


def test_a_slow_claude_lookup_says_what_it_is_doing(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    _slow_claude(monkeypatch, r, "mcp__scout__mail_recent", 0.2, "You have three new emails.")
    assert run(_turn(r, "anything new for me")) == ["Checking your mail.", "You have three new emails."]


def test_a_slow_local_lookup_says_it_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 0 weather or a route can take seconds; the timer starts before it."""
    r = _rig()

    async def slow_local(text: str, ctx: Any) -> str | None:
        await asyncio.sleep(0.2)
        return "Sunny, high of 70."

    monkeypatch.setattr(local_intents, "answer", slow_local)
    assert run(_turn(r, "what's the weather")) == ["Getting the weather.", "Sunny, high of 70."]


def test_a_quick_answer_says_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    _slow_claude(monkeypatch, r, "mcp__scout__weather", 0.0, "Sunny.")

    async def go() -> list[str]:
        said = await _turn(r, "what's the weather")
        await asyncio.sleep(0.1)  # past the delay: the progress task was cancelled
        return said

    assert run(go()) == ["Sunny."]


def test_a_long_wait_gets_one_more_different_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_mod, "CLAUDE_PROGRESS_S", 0.0)  # no tool call: past the Claude wait
    r = _rig(again=0.1)
    _slow_claude(monkeypatch, r, None, 0.3, "Done.")
    assert run(_turn(r, "think hard about this")) == ["One moment.", STILL_WORKING, "Done."]


def test_nothing_once_the_answer_has_started(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig(again=0.1)
    r.a.cfg.claude.local_first = False

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        yield "speak", "First part."
        await asyncio.sleep(0.3)
        yield "speak", "Second part."

    monkeypatch.setattr(r.a.brain, "ask", ask)
    assert run(_turn(r, "tell me a story")) == ["First part.", "Second part."]


def test_a_silent_turn_gets_no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    _slow_claude(monkeypatch, r, "mcp__scout__mail_recent", 0.2, "Three emails.")
    assert run(_turn(r, "anything new", speak=False, client="phone")) == []


def test_no_progress_after_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    _slow_claude(monkeypatch, r, "mcp__scout__mail_recent", 0.2, "Three emails.")

    async def go() -> list[str]:
        r.a.start_turn("anything new")
        await asyncio.sleep(0.01)
        await hear(r, "Scout, stop")
        assert r.a._turn is not None
        await r.a._turn
        return r.spk.said

    assert run(go()) == []


def test_no_progress_during_a_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    r.a.cfg.claude.local_first = False
    r.a.cfg.claude.confirm_timeout_s = 0.2

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        yield "tool", ("WebFetch", {})
        await r.a._confirm("Fetch the page?", "fetch a page")  # nobody answers
        yield "text", "Skipped."

    monkeypatch.setattr(r.a.brain, "ask", ask)
    said = run(_turn(r, "read me that page"))
    assert said == ["Fetch the page?", "No answer, so I skipped that.", "Skipped."]


def test_progress_speech_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    r.a.cfg.tts.progress_speech = False
    _slow_claude(monkeypatch, r, "mcp__scout__mail_recent", 0.2, "Three emails.")
    assert run(_turn(r, "anything new")) == ["Three emails."]


def test_progress_is_not_what_say_that_again_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _rig()
    _slow_claude(monkeypatch, r, "mcp__scout__mail_recent", 0.2, "Three emails.")

    async def go() -> tuple[str, str] | None:
        await _turn(r, "anything new")
        return r.a.last_spoken

    assert run(go()) == ("Scout", "Three emails.")


def test_loading_a_tool_is_not_called_searching() -> None:
    from scout import progress

    # Claude loading one of Scout's tools (ToolSearch) isn't a search the user asked for:
    # "Brian West is 5'9"" loaded remember first, and Scout said "Searching."
    assert progress.progress_phrase("ToolSearch") == "One moment."
    assert progress.progress_phrase("mcp__voice_app__local_search") == "Searching."
