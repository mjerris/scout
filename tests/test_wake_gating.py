"""When the room may answer speech without the wake word (the follow-up window), and
what such speech must look like. Through a real Assistant with a fake speaker and ASR."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from test_routing import Rig, make, recorder, run

from scout.audio import Utterance
from scout.gate import AudioStats


async def hear(r: Rig, text: str, echo: bool = False, started: float | None = None) -> None:
    """Like test_routing.hear, with as much voiced speech as the words take (3 a second)."""
    r.asr.next = text
    now = time.monotonic()
    voiced = max(1500, int(len(text.split()) / 3 * 1000))
    stats = AudioStats(voiced_ms=voiced, level_db=-25, floor_db=-55)
    await r.a._handle(Utterance(b"\0\0" * 1600, started or now - 1.5, echo, stats, now))


def _quiet_claude(monkeypatch: pytest.MonkeyPatch, r: Rig, answer: str = "It's 58 and clear.") -> list[str]:
    """Claude answers at once; returns what it was asked."""
    asked: list[str] = []

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        asked.append(text)
        yield "text", answer

    async def nothing() -> None:
        return None

    r.a.cfg.claude.local_first = False
    r.a.cfg.claude.request_context = False
    monkeypatch.setattr(r.a.brain, "ask", ask)
    monkeypatch.setattr(r.a.brain, "interrupt", nothing)
    monkeypatch.setattr(r.a.brain, "reset", nothing)
    return asked


def test_talk_over_the_startup_pulse_does_not_open_a_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 18:49 incident, replayed: just after a restart the pulse spoke; room
    conversation talked over it (a barge-in), which stopped it and opened the no-wake
    window ([wake] follow_up_seconds = 30 there); 26 s later "It's asking me my..."
    (no wake word) started a Claude turn."""

    async def go() -> tuple[list[str], float]:
        r = make()
        r.a.cfg.wake.follow_up_seconds = 30.0
        r.a.echo_cancelled = True
        started: list[str] = []
        monkeypatch.setattr(r.a, "start_turn", recorder(started))
        r.spk.idle_delay = 0.2  # the pulse takes a moment to play
        r.spk.busy = True
        pulse = asyncio.create_task(r.a.announce("Your 7 PM meeting starts in 10 minutes.", "pulse"))
        await asyncio.sleep(0.02)
        onset = time.monotonic()
        r.a.on_speech_onset(while_speaking=True)
        await hear(r, "Why the fuck is this terrible?", echo=True, started=onset)
        r.spk.busy = False
        await pulse
        window = r.a.follow_up_until - onset
        await hear(r, "It's asking me my...", started=onset + 26)
        return started, window

    started, window = run(go())
    assert started == [] and window <= 0  # before the fix: [the request], 30 s


def test_talk_over_a_room_answer_is_the_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the answer to your request, what you say is the next request (no wake word)."""

    async def go() -> list[str]:
        r = make()
        asked = _quiet_claude(monkeypatch, r, "It's 58 and clear. Tomorrow looks warmer, with wind later.")
        r.a.echo_cancelled = True
        r.spk.idle_delay = 0.3  # the answer plays for a while
        r.spk.busy = True
        r.a.start_turn("what's the weather")
        await asyncio.sleep(0.1)  # the answer is playing
        onset = time.monotonic()
        r.a.on_speech_onset(while_speaking=True)
        await hear(r, "What about Saturday?", started=onset)
        r.spk.busy = False
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(asked) > 1 and not r.a.busy:
                break
        return asked

    assert run(go()) == ["what's the weather", "What about Saturday?"]


async def _answered(r: Rig, request: str = "what's the weather") -> None:
    """The room answered `request` (start_turn runs the real turn)."""
    r.a.start_turn(request)
    assert r.a._turn is not None
    await r.a._turn


def test_a_question_after_an_answer_needs_no_wake_word(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        asked = _quiet_claude(monkeypatch, r)
        await _answered(r)
        await hear(r, "What about tomorrow?")
        assert r.a._turn is not None
        await r.a._turn
        return asked

    assert run(go()) == ["what's the weather", "What about tomorrow?"]


@pytest.mark.parametrize(
    "said",
    [
        "It's not just memory, by the way, Brian. It's more complicated than that.",
        "You might have to ask Brian.",
        "It's asking me my...",
        "Oh, I know.",
        "We shipped that last week.",
    ],
)
def test_room_conversation_after_an_answer_is_not_a_follow_up(
    monkeypatch: pytest.MonkeyPatch, said: str
) -> None:
    async def go() -> tuple[list[str], bool]:
        r = make()
        asked = _quiet_claude(monkeypatch, r)
        await _answered(r)
        await hear(r, said)
        return asked, r.a.busy

    assert run(go()) == (["what's the weather"], False)


def test_open_follow_ups_take_any_speech_that_passes_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        r.a.cfg.wake.follow_up = "open"
        asked = _quiet_claude(monkeypatch, r)
        await _answered(r)
        await hear(r, "We shipped that last week.")
        assert r.a._turn is not None
        await r.a._turn
        return asked

    assert run(go()) == ["what's the weather", "We shipped that last week."]


def test_follow_ups_off_always_need_the_wake_word(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], float]:
        r = make()
        r.a.cfg.wake.follow_up = "off"
        asked = _quiet_claude(monkeypatch, r)
        await _answered(r)
        await hear(r, "What about tomorrow?")
        return asked, r.a.follow_up_until

    asked, until = run(go())
    assert asked == ["what's the weather"] and until <= time.monotonic()


def test_a_follow_up_turn_opens_no_window_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chains: after a follow-up answered without the wake word, the next needs it."""

    async def go() -> list[str]:
        r = make()
        asked = _quiet_claude(monkeypatch, r)
        await _answered(r)
        await hear(r, "What about tomorrow?")
        assert r.a._turn is not None
        await r.a._turn
        await hear(r, "And Saturday?")
        if r.a._turn is not None:
            await r.a._turn
        await hear(r, "Scout, and Sunday?")
        assert r.a._turn is not None
        await r.a._turn
        return asked

    assert run(go()) == ["what's the weather", "What about tomorrow?", "and Sunday?"]


def test_a_session_message_that_wants_no_reply_opens_no_window() -> None:
    async def go() -> bool:
        r = make()
        await r.a.discuss(agent="proj#1", message="Build finished.", listen=False)
        return r.a.follow_up_until > time.monotonic()

    assert not run(go())


@pytest.mark.parametrize("who", ["pulse", "slack", "starlink"])
def test_an_announcement_opens_no_window(who: str) -> None:
    async def go() -> list[str]:
        r = make()
        started: list[str] = []
        r.a.start_turn = recorder(started)  # type: ignore[method-assign]
        await r.a.announce("Your 7 PM meeting starts in 10 minutes.", who)
        await hear(r, "What about tomorrow?")
        return started

    assert run(go()) == []


def test_speech_right_after_startup_needs_the_wake_word() -> None:
    async def go() -> list[str]:
        r = make()
        started: list[str] = []
        r.a.start_turn = recorder(started)  # type: ignore[method-assign]
        await hear(r, "What about tomorrow?")
        return started

    assert run(go()) == []


def _slow_claude(monkeypatch: pytest.MonkeyPatch, r: Rig) -> tuple[list[str], asyncio.Event]:
    """Claude takes until the event is set to answer; returns what it was asked."""
    asked: list[str] = []
    release = asyncio.Event()

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        asked.append(text)
        await release.wait()
        release.clear()
        yield "text", "Okay."

    async def interrupt() -> None:
        release.set()

    async def nothing() -> None:
        return None

    r.a.cfg.claude.local_first = False
    r.a.cfg.claude.request_context = False
    monkeypatch.setattr(r.a.brain, "ask", ask)
    monkeypatch.setattr(r.a.brain, "interrupt", interrupt)
    monkeypatch.setattr(r.a.brain, "reset", nothing)
    return asked, release


async def _wake_turn(r: Rig, text: str) -> float:
    """Hear `text` (with the wake word) as a turn's request; returns when its clip ended."""
    now = time.monotonic()
    await hear(r, text, started=now - 1.5)
    await asyncio.sleep(0.02)
    return now


async def _finish(r: Rig, release: asyncio.Event) -> None:
    """Let every turn answer, queued ones included."""
    for _ in range(100):
        release.set()
        await asyncio.sleep(0.02)  # a queued request may start
        if r.a._turn is None or r.a._turn.done():
            return


def test_a_request_split_by_a_pause_is_asked_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        asked, release = _slow_claude(monkeypatch, r)
        ended = await _wake_turn(r, "Scout, what time do those winds...")
        await hear(r, "forecast to start", started=ended + 0.8)
        await _finish(r, release)
        return asked

    assert run(go())[-1] == "what time do those winds forecast to start"


def test_speech_long_after_the_request_does_not_join_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        asked, release = _slow_claude(monkeypatch, r)
        ended = await _wake_turn(r, "Scout, what time do those winds...")
        await hear(r, "forecast to start", started=ended + 4.0)
        await _finish(r, release)
        return asked

    assert run(go()) == ["what time do those winds..."]


def test_a_statement_does_not_join_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        asked, release = _slow_claude(monkeypatch, r)
        ended = await _wake_turn(r, "Scout, what's on my calendar...")
        await hear(r, "It's more complicated than that.", started=ended + 0.8)
        await _finish(r, release)
        return asked

    assert run(go()) == ["what's on my calendar..."]


def test_a_request_joins_one_split_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heard live: three utterances of someone else's conversation glued onto one turn."""

    async def go() -> list[str]:
        r = make()
        asked, release = _slow_claude(monkeypatch, r)
        ended = await _wake_turn(r, "Scout, what time do those winds...")
        await hear(r, "forecast to start", started=ended + 0.8)
        await asyncio.sleep(0.05)  # the whole request is being asked
        await hear(r, "and the rain", started=time.monotonic() - 0.5)
        await _finish(r, release)
        return asked

    assert run(go()) == ["what time do those winds...", "what time do those winds forecast to start"]


def test_words_begun_over_the_end_of_an_answer_are_its_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer finished while the words were still being heard: its window holds."""

    async def go() -> list[str]:
        r = make()
        asked = _quiet_claude(monkeypatch, r)
        r.a.echo_cancelled = True
        r.spk.busy = True  # the answer's last words are playing
        r.a.start_turn("what's the weather")
        onset = time.monotonic()
        r.a.on_speech_onset(while_speaking=True)
        assert r.a._turn is not None
        await r.a._turn  # done playing
        r.spk.busy = False
        await hear(r, "What about tomorrow?", started=onset)
        if r.a._turn is not None:
            await r.a._turn
        return asked

    assert run(go()) == ["what's the weather", "What about tomorrow?"]
