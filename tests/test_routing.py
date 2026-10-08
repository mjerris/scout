"""Routing through a real Assistant with a fake speaker and ASR (no audio, no Claude)."""

from __future__ import annotations

import asyncio
import contextlib
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
from claude_voice.audio_io import AudioIO
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
    monkeypatch.setattr(
        assistant_mod, "analyze", lambda pcm, aggressiveness=2: LOUD
    )  # a real 1.5 s of speech

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
    p.rules += parse("STT 'zzyzx' '\\1'")
    assert p.stt("hey zzyzx") == "hey zzyzx"
    assert p.stt("hey zzyzx") == "hey zzyzx"  # rule removed after the first failure


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
    assert said == ["Okay, I'll do that next."] and says == []


def test_typed_stop_and_reset_are_handled_not_sent_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], list[int]]:
        r = make()
        sent: list[str] = []
        resets: list[int] = []

        async def fake_reset(speak: bool = True, client: str | None = None) -> None:
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


def test_reset_after_a_cancelled_turn_still_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[int]:
        r = make()
        resets: list[int] = []

        async def fake_brain_reset() -> None:
            resets.append(1)

        monkeypatch.setattr(r.a.brain, "reset", fake_brain_reset)
        turn = asyncio.create_task(asyncio.sleep(10))
        r.a._turn = turn
        turn.cancel()
        await asyncio.sleep(0)
        await r.a.reset()
        return resets

    assert run(go()) == [1]


def test_hang_on_holds_the_floor() -> None:
    async def go() -> tuple[bool, str | None]:
        r = make()
        r.a._room_command("remind me to, hang on")
        return r.a.floor.try_acquire("desk#1"), r.a.floor.owner()

    taken, owner = run(go())
    assert not taken and owner == "room"


def test_talking_over_reply_without_wake_word_is_not_queued() -> None:
    async def go() -> tuple[str | None, str | None]:
        r = make()
        now = time.monotonic()
        r.spk.played = [(now - 2, now, "Here is the weather for today.")]
        turn = asyncio.create_task(asyncio.sleep(1))
        r.a._turn = turn
        await hear(r, "Here is the weather for today. can you pass the salt", echo=True, started=now - 2)
        first = r.a._queued
        await hear(r, "Here is the weather for today. Claude, and tomorrow?", echo=True, started=now - 2)
        turn.cancel()
        return first, r.a._queued

    first, second = run(go())
    assert first is None and second == "and tomorrow"


def test_stop_releases_a_hang_on_hold() -> None:
    async def go() -> tuple[str | None, str]:
        r = make()
        r.a._room_command("remind me to, hang on")
        await r.a.stop()
        return r.a.floor.owner(), r.a.state

    owner, state = run(go())
    assert owner is None and state == "idle"


def test_quiet_reply_to_session_is_rejected() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        r.a._ref_db = -25.0  # the user's voice level
        task = asyncio.create_task(r.a.discuss("desk#1", "Deploy?", listen=True, timeout=1))
        await asyncio.sleep(0.05)
        r.asr.next = "and now the weather"
        tv = AudioStats(voiced_ms=1500, level_db=-48, floor_db=-60)  # far quieter than the user
        now = time.monotonic()
        await r.a._handle(Utterance(b"\0\0" * 1600, now, False, tv, now + 1.5))
        return await task

    assert run(go())["status"] == "no_reply"


def test_mic_cannot_answer_a_question_asked_only_on_a_phone() -> None:
    async def go() -> bool | str | None:
        r = make()
        r.a._out = {"mini": False, "client": "phone"}  # a phone-only turn
        task = asyncio.create_task(r.a._confirm("Run git push?", "run: git push --force"))
        await asyncio.sleep(0.01)
        await hear(r, "Okay.", started=time.monotonic())  # the TV, via the mini's mic
        await asyncio.sleep(0.01)
        r.a.answer_confirm(False, r.a._confirm_id)  # the phone user denies
        return await task

    assert run(go()) is False


def test_stale_web_answer_cannot_approve_the_next_question() -> None:
    async def go() -> list[bool | str | None]:
        r = make()
        first = asyncio.create_task(r.a._confirm("Run echo?", "run: echo first"))
        await asyncio.sleep(0.01)
        first_id = r.a._confirm_id
        r.a.answer_confirm(True, first_id)
        a1 = await first
        second = asyncio.create_task(r.a._confirm("Run rm?", "run: rm -rf ~/x"))
        await asyncio.sleep(0.01)
        r.a.answer_confirm(True, first_id)  # the double tap arrives late
        await asyncio.sleep(0.01)
        r.a.answer_confirm(False, r.a._confirm_id)
        return [a1, await second]

    assert run(go()) == [True, False]


def test_phone_only_turn_opens_no_mic_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()

        async def fake_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            yield "text", "Here you go."

        monkeypatch.setattr(r.a.brain, "ask", fake_ask)
        await r.a.submit_text("what's on my calendar", speak=False, client="phone")
        await asyncio.sleep(0.1)
        sent: list[str] = []
        monkeypatch.setattr(r.a, "start_turn", recorder(sent))
        await hear(r, "can you pass the salt please", started=time.monotonic())
        return sent

    assert run(go()) == []


def test_hang_on_then_never_mind_cancels_instead_of_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], list[str]]:
        r = make()
        sent: list[str] = []
        monkeypatch.setattr(r.a, "start_turn", recorder(sent))
        r.a._room_command("remind me to, hang on")
        r.a._room_command("never mind")
        await asyncio.sleep(0.05)
        return sent, r.a._held_words

    sent, held = run(go())
    assert sent == [] and held == []


def test_hang_on_words_are_not_joined_across_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        r = make()
        sent: list[str] = []
        monkeypatch.setattr(r.a, "start_turn", recorder(sent))
        r.a._room_command("remind me to, hang on", speak=False, client="phone")
        r.a._room_command("the dishwasher is done now")  # someone at the mini
        return sent

    assert run(go()) == ["the dishwasher is done now"]


def test_stop_ends_a_session_floor_hold_and_queued_discuss() -> None:
    async def go() -> tuple[str | None, dict[str, Any]]:
        r = make()
        await r.a.discuss("desk#1", "step one", listen=False, hold=True)
        queued = asyncio.create_task(r.a.discuss("desk#2", "hello", listen=False, wait_for_floor=5))
        await asyncio.sleep(0.05)
        await r.a.stop()
        return r.a.floor.owner(), await queued

    owner, result = run(go())
    assert owner is None and result["status"] == "stopped"


def test_discuss_reports_a_muted_mic() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        r.a.set_mic_muted(True)
        return await r.a.discuss("desk#1", "Deploy?", listen=True, timeout=1)

    assert run(go()) == {"status": "muted"}


def test_cancelled_confirm_leaves_no_stale_question() -> None:
    async def go() -> tuple[bool, str]:
        r = make()
        r.spk.idle_delay = 0.3  # the question is still playing when the SDK withdraws it
        task = asyncio.create_task(r.a._confirm("Run command?", "run: x"))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return r.a._confirm_fut is None, r.a.state

    no_question, state = run(go())
    assert no_question and state == "idle"


def test_listening_state_times_out() -> None:
    async def go() -> tuple[str, str]:
        r = make()
        r.a._listen_for_more(0.1, speak=False)
        first = r.a.state
        await asyncio.sleep(0.3)
        return first, r.a.state

    assert run(go()) == ("listening", "idle")


class FakeIO:
    """An AudioIO stand-in: play() finishes after a short delay unless stopped."""

    name = "fake"

    def __init__(self) -> None:
        self.played: list[float] = []
        self.stopped = 0
        self._pending: list[Any] = []
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()

    def play(self, samples: Any, sample_rate: int) -> Any:
        from claude_voice.audio_io import PlayHandle

        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        h = PlayHandle(len(self.played) + 1, len(samples) / sample_rate, fut)
        self.played.append(h.seconds)
        self._pending.append(h)

        def finish() -> None:
            if not fut.done():
                fut.set_result(True)

        asyncio.get_running_loop().call_later(0.05, finish)
        return h

    def stop_playback(self) -> None:
        self.stopped += 1
        for h in self._pending:
            if not h.done.done():
                h.done.set_result(False)

    @property
    def playing(self) -> bool:
        return any(not h.done.done() for h in self._pending)


def test_speaker_plays_through_the_voice_layer_and_stop_silences_it() -> None:
    import numpy as np

    async def go() -> tuple[list[float], int, bool]:
        io = FakeIO()
        spk = Speaker.__new__(Speaker)
        # minimal init of the playback side only
        spk.io = cast("AudioIO", io)  # implements the parts the playback loop uses
        spk.sr = 24000
        spk._audio = asyncio.Queue()
        spk._gen = 0
        spk._pending = 0
        spk._playing = False
        spk._last_end = 0.0
        spk._idle = asyncio.Event()
        spk._idle.set()
        spk._played = deque(maxlen=10)
        spk._play_lock = __import__("threading").Lock()
        spk.consecutive_failures = 0
        task = asyncio.create_task(spk._play_loop())
        spk._pending = 1
        spk._idle.clear()
        spk._audio.put_nowait((0, np.zeros(2400, np.float32), "hello"))
        await asyncio.sleep(0.01)
        spk.stop()
        await asyncio.sleep(0.01)
        task.cancel()
        return io.played, io.stopped, spk._idle.is_set()

    played, stopped, idle = run(go())
    assert played == [0.1] and stopped == 1 and idle


def test_barge_in_stops_a_room_reply_and_opens_a_follow_up() -> None:
    async def go() -> tuple[int, bool]:
        r = make()
        r.a.echo_cancelled = True
        r.spk.busy = True  # a reply is playing
        r.a.on_speech_onset(while_speaking=True)
        await asyncio.sleep(0.05)
        return r.spk.stopped, r.a.follow_up_until > time.monotonic()

    stopped, follow_up = run(go())
    assert stopped >= 1 and follow_up


def test_no_barge_in_without_an_echo_canceller() -> None:
    async def go() -> int:
        r = make()
        r.spk.busy = True
        r.a.on_speech_onset(while_speaking=True)  # plain backend: this is our own echo
        await asyncio.sleep(0.05)
        return r.spk.stopped

    assert run(go()) == 0


def test_barge_into_a_session_message_becomes_its_reply() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        r.a.echo_cancelled = True
        r.spk.idle_delay = 0.3  # a long message is playing
        task = asyncio.create_task(r.a.discuss("desk#1", "Here are the three options...", timeout=2))
        await asyncio.sleep(0.05)
        r.spk.busy = True
        onset = time.monotonic()
        r.a.on_speech_onset(while_speaking=True)
        await asyncio.sleep(0.3)
        r.spk.busy = False
        await hear(r, "the second one", started=onset)
        return await task

    assert run(go()) == {"status": "ok", "text": "the second one"}
