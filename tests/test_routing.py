"""Routing through a real Assistant with a fake speaker and ASR (no audio, no Claude)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import pytest

import claude_voice.assistant as assistant_mod
from claude_voice.asr import Transcriber
from claude_voice.assistant import Assistant
from claude_voice.audio import Utterance
from claude_voice.config import Config
from claude_voice.gate import AudioStats, Transcript
from claude_voice.pronounce import Pronouncer, parse
from claude_voice.tts import Speaker

T = TypeVar("T")


class FakeSpeaker:
    voice = "af_heart"

    def __init__(self) -> None:
        self.said: list[str] = []
        self.stopped = 0
        self.busy = False
        self.played: list[tuple[float, float, str]] = []  # (start, end, text)
        self.idle_delay = 0.0

    def speak(self, text: str, voice: str | None = None) -> None:
        self.said.append(text)

    def chime(self, kind: str) -> None:
        pass

    def stop(self) -> None:
        self.stopped += 1

    async def wait_idle(self) -> None:
        await asyncio.sleep(self.idle_delay)

    def voices(self) -> list[str]:
        return ["af_heart"]

    def recent_speech(self, since: float) -> str:
        return " ".join(t for s, e, t in self.played if e >= since - 0.5)

    def overlap(self, start: float, end: float) -> float:
        return sum(max(0.0, min(e, end) - max(s, start)) for s, e, _ in self.played)

    def last_speech_end(self, since: float) -> float | None:
        ends = [e for s, e, _ in self.played if e >= since - 0.5]
        return max(ends) if ends else None


class FakeASR:
    def __init__(self) -> None:
        self.next = ""

    async def transcribe(self, pcm: bytes, prompt: str | None = None) -> Transcript:
        return Transcript(self.next, avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.2)


@dataclass
class Rig:
    a: Assistant
    spk: FakeSpeaker
    asr: FakeASR


def make() -> Rig:
    spk, asr = FakeSpeaker(), FakeASR()
    a = Assistant(Config(), cast(Transcriber, asr), cast(Speaker, spk), Pronouncer())
    return Rig(a, spk, asr)


LOUD = AudioStats(voiced_ms=1500, level_db=-25, floor_db=-55)


async def hear(r: Rig, text: str, echo: bool = False, started: float | None = None) -> None:
    r.asr.next = text
    now = time.monotonic()
    await r.a._handle(Utterance(b"\0\0" * 1600, started or now - 1.5, echo, LOUD, now))


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_stop_during_approval_stops_everything() -> None:
    async def go() -> tuple[bool | str | None, int]:
        r = make()
        task = asyncio.create_task(r.a._confirm("Run command?", "run a command"))
        await asyncio.sleep(0.01)
        await hear(r, "Claude, stop")
        return await task, r.spk.stopped

    answer, stopped = run(go())
    assert answer is False and stopped >= 1


def test_unclear_push_to_talk_answer_asks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant_mod, "analyze", lambda pcm: LOUD)  # a real 1.5 s of speech

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        task = asyncio.create_task(r.a._confirm("Run command?", "run a command"))
        await asyncio.sleep(0.01)
        r.asr.next = "purple monkey dishwasher"
        result = await r.a.submit_audio(b"\0\0" * 16000)
        asked = list(r.spk.said)
        r.a.answer_confirm(True)
        await task
        return result, asked

    result, asked = run(go())
    assert "ignored" not in result and "Sorry, was that a yes or a no?" in asked


def recorder(store: list[str]) -> Any:
    def start_turn(text: str, speak: bool = True, client: str | None = None) -> bool:
        store.append(text)
        return True

    return start_turn


def test_reply_in_echo_tail_that_reuses_our_words_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        now = time.monotonic()
        # We finished asking just before the clip; nothing overlapped it.
        r.spk.played = [(now - 3, now - 1.6, "Do you want the weather for today or tomorrow?")]
        r.a.follow_up_until = now + 8
        started: list[str] = []
        monkeypatch.setattr(r.a, "start_turn", recorder(started))
        await hear(r, "for tomorrow", echo=True, started=now - 1.5)
        return started

    assert run(go()) == ["for tomorrow"]


def test_stop_while_session_message_plays_ends_discuss() -> None:
    async def go() -> tuple[dict[str, Any], str | None]:
        r = make()
        r.spk.idle_delay = 0.2  # the message takes a moment to play
        task = asyncio.create_task(r.a.discuss("desk#1", "Should I deploy now?", listen=True, timeout=5))
        await asyncio.sleep(0.05)
        await r.a.stop()
        result = await task
        return result, r.a.floor.owner()

    result, owner = run(go())
    assert result["status"] == "stopped" and owner is None


def test_listening_session_gets_one_word_reply_and_noise_is_dropped() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        task = asyncio.create_task(r.a.discuss("desk#1", "Deploy?", listen=True, timeout=3))
        await asyncio.sleep(0.05)
        await hear(r, "you", started=time.monotonic())  # Whisper noise phrase
        await hear(r, "Yes.", started=time.monotonic())
        return await task

    assert run(go()) == {"status": "ok", "text": "Yes."}


def test_timer_waits_for_session_floor() -> None:
    async def go() -> tuple[list[str], list[str]]:
        r = make()
        r.a.floor.try_acquire("desk#1")
        r.a._timer_done("tea")
        await asyncio.sleep(0.1)
        before = list(r.spk.said)
        r.a.floor.release("desk#1")
        await asyncio.sleep(0.7)
        return before, r.spk.said

    before, after = run(go())
    assert before == [] and after == ["Your tea timer is done."]


def test_broken_pronounce_replacement_is_disabled_not_fatal() -> None:
    p = Pronouncer()
    p.rules += parse("STT 'clawd' '\\1'")
    assert p.stt("hey clawd") == "hey clawd"
    assert p.stt("hey clawd") == "hey clawd"  # rule removed after the first failure


def test_speaker_overlap_math() -> None:
    s = Speaker.__new__(Speaker)
    s._played = deque([[10.0, 12.0, "a"], [13.0, 14.0, "b"]])
    assert s.overlap(11.0, 13.5) == pytest.approx(1.5)
    assert s.overlap(14.5, 16.0) == 0.0


def test_stop_while_waiting_for_floor_cancels_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], str | None]:
        r = make()
        asked: list[str] = []

        async def fake_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            asked.append(text)
            yield "result", None

        monkeypatch.setattr(r.a.brain, "ask", fake_ask)
        r.a.floor.try_acquire("desk#1")
        r.a.start_turn("delete the build folder")
        await asyncio.sleep(0.05)
        await r.a.stop()
        r.a.floor.release("desk#1")
        await asyncio.sleep(0.8)
        return asked, r.a.floor.owner()

    asked, owner = run(go())
    assert asked == [] and owner is None


def test_busy_reply_goes_to_whoever_spoke() -> None:
    async def go() -> tuple[list[str], list[dict[str, Any]]]:
        r = make()
        turn = asyncio.create_task(asyncio.sleep(1))  # a web client's turn is running
        r.a._turn = turn
        r.a._out = {"mini": False, "client": "phone"}
        events = r.a.subscribe()
        await hear(r, "Claude, what time is it")  # someone in the room, via the mic
        turn.cancel()
        says = []
        while not events.empty():
            ev = events.get_nowait()
            if ev["type"] == "say":
                says.append(ev)
        return r.spk.said, says

    said, says = run(go())
    assert said == ["I'm still working on the last request. Say stop to cancel it."] and says == []


def test_typed_stop_and_reset_are_handled_not_sent_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], list[int]]:
        r = make()
        sent: list[str] = []
        resets: list[int] = []

        async def fake_reset() -> None:
            resets.append(1)

        monkeypatch.setattr(r.a, "start_turn", recorder(sent))
        monkeypatch.setattr(r.a, "reset", fake_reset)
        await r.a.submit_text("new conversation", speak=False, client="phone")
        await asyncio.sleep(0.01)
        await r.a.submit_text("what's the weather", speak=False, client="phone")
        return sent, resets

    sent, resets = run(go())
    assert resets == [1] and sent == ["what's the weather"]


def test_typed_reply_reaches_a_listening_session() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        task = asyncio.create_task(r.a.discuss("desk#1", "Deploy?", listen=True, timeout=3))
        await asyncio.sleep(0.05)
        await r.a.submit_text("yes, go ahead", speak=False, client="phone")
        return await task

    assert run(go()) == {"status": "ok", "text": "yes, go ahead"}


def test_bare_stop_while_listening_releases_the_floor_hold() -> None:
    async def go() -> tuple[dict[str, Any], str | None]:
        r = make()
        task = asyncio.create_task(r.a.discuss("desk#1", "Deploy?", listen=True, timeout=3, hold=True))
        await asyncio.sleep(0.05)
        await hear(r, "stop", started=time.monotonic())
        result = await task
        return result, r.a.floor.owner()

    result, owner = run(go())
    assert result["status"] == "stopped" and owner is None


def test_timer_does_not_take_the_rooms_name() -> None:
    async def go() -> tuple[list[str], list[str]]:
        r = make()
        r.a.floor.try_acquire("room")
        r.a.floor.release("room", hold=True, ttl=0.3)  # room's follow-up hold
        r.a._timer_done("tea")
        await asyncio.sleep(0.1)
        early = list(r.spk.said)
        await asyncio.sleep(0.8)
        return early, r.spk.said

    early, later = run(go())
    assert early == [] and later == ["Your tea timer is done."]
