"""Routing through a real Assistant with a fake speaker and ASR (no audio, no Claude)."""

import asyncio
import time

import pytest

from claude_voice.assistant import Assistant
from claude_voice.config import Config
from claude_voice.gate import AudioStats, Transcript
from claude_voice.pronounce import Pronouncer, parse
from claude_voice.tts import Speaker


class FakeSpeaker:
    voice = "af_heart"

    def __init__(self):
        self.said, self.stopped, self.busy = [], 0, False
        self.played = []  # (start, end, text)

    def speak(self, text, voice=None):
        self.said.append(text)

    def chime(self, kind):
        pass

    def stop(self):
        self.stopped += 1

    async def wait_idle(self):
        await asyncio.sleep(0)

    def voices(self):
        return ["af_heart"]

    def recent_speech(self, since):
        return " ".join(t for s, e, t in self.played if e >= since - 0.5)

    def overlap(self, start, end):
        return sum(max(0.0, min(e, end) - max(s, start)) for s, e, _ in self.played)

    def last_speech_end(self, since):
        ends = [e for s, e, _ in self.played if e >= since - 0.5]
        return max(ends) if ends else None


class FakeASR:
    def __init__(self):
        self.next = ""

    async def transcribe(self, pcm, prompt=None):
        return Transcript(self.next, avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.2)


def make():
    cfg = Config()
    a = Assistant(cfg, FakeASR(), FakeSpeaker(), Pronouncer())
    a._write_transcript = lambda ev: None
    return a


LOUD = AudioStats(voiced_ms=1500, level_db=-25, floor_db=-55)


async def hear(a, text, echo=False, started=None):
    from claude_voice.audio import Utterance
    a.asr.next = text
    now = time.monotonic()
    await a._handle(Utterance(b"\0\0" * 1600, started or now - 1.5, echo, LOUD, now))


def run(coro):
    return asyncio.run(coro)


def test_stop_during_approval_stops_everything():
    async def go():
        a = make()
        task = asyncio.create_task(a._confirm("Run command?", "run a command"))
        await asyncio.sleep(0.01)
        await hear(a, "Claude, stop")
        return await task, a.speaker.stopped
    answer, stopped = run(go())
    assert answer is False and stopped >= 1


def test_unclear_push_to_talk_answer_asks_again(monkeypatch):
    import claude_voice.assistant as mod
    monkeypatch.setattr(mod, "analyze", lambda pcm: LOUD)  # a real 1.5 s of speech

    async def go():
        a = make()
        task = asyncio.create_task(a._confirm("Run command?", "run a command"))
        await asyncio.sleep(0.01)
        a.asr.next = "purple monkey dishwasher"
        r = await a.submit_audio(b"\0\0" * 16000)
        asked = list(a.speaker.said)
        a.answer_confirm(True)
        await task
        return r, asked
    r, asked = run(go())
    assert "ignored" not in r and "Sorry, was that a yes or a no?" in asked


def test_reply_in_echo_tail_that_reuses_our_words_is_kept():
    async def go():
        a = make()
        now = time.monotonic()
        # We finished asking just before the clip; nothing overlapped it.
        a.speaker.played = [(now - 3, now - 1.6, "Do you want the weather for today or tomorrow?")]
        a.follow_up_until = now + 8
        started = []
        a.start_turn = lambda text, speak=True, client=None: started.append(text) or True
        await hear(a, "for tomorrow", echo=True, started=now - 1.5)
        return started
    assert run(go()) == ["for tomorrow"]


def test_stop_while_session_message_plays_ends_discuss():
    async def go():
        a = make()

        async def slow_idle():
            await asyncio.sleep(0.2)
        a.speaker.wait_idle = slow_idle
        task = asyncio.create_task(a.discuss("desk#1", "Should I deploy now?", listen=True, timeout=5))
        await asyncio.sleep(0.05)
        await a.stop()
        r = await task
        return r, a.floor.owner()
    r, owner = run(go())
    assert r["status"] == "stopped" and owner is None


def test_listening_session_gets_one_word_reply_and_noise_is_dropped():
    async def go():
        a = make()
        task = asyncio.create_task(a.discuss("desk#1", "Deploy?", listen=True, timeout=3))
        await asyncio.sleep(0.05)
        await hear(a, "you", started=time.monotonic())  # Whisper noise phrase
        await hear(a, "Yes.", started=time.monotonic())
        return await task
    assert run(go()) == {"status": "ok", "text": "Yes."}


def test_timer_waits_for_session_floor():
    async def go():
        a = make()
        a.floor.try_acquire("desk#1")
        a._timer_done("tea")
        await asyncio.sleep(0.1)
        before = list(a.speaker.said)
        a.floor.release("desk#1")
        await asyncio.sleep(0.7)
        return before, a.speaker.said
    before, after = run(go())
    assert before == [] and after == ["Your tea timer is done."]


def test_broken_pronounce_replacement_is_disabled_not_fatal():
    p = Pronouncer()
    p.rules += parse("STT 'clawd' '\\1'")
    assert p.stt("hey clawd") == "hey clawd"
    assert p.stt("hey clawd") == "hey clawd"  # rule removed after the first failure


def test_speaker_overlap_math():
    s = Speaker.__new__(Speaker)
    from collections import deque
    s._played = deque([[10.0, 12.0, "a"], [13.0, 14.0, "b"]])
    assert s.overlap(11.0, 13.5) == pytest.approx(1.5)
    assert s.overlap(14.5, 16.0) == 0.0


def test_stop_while_waiting_for_floor_cancels_the_turn():
    async def go():
        a = make()
        asked = []

        async def fake_ask(text):
            asked.append(text)
            yield "result", None
        a.brain.ask = fake_ask
        a.floor.try_acquire("desk#1")
        a.start_turn("delete the build folder")
        await asyncio.sleep(0.05)
        await a.stop()
        a.floor.release("desk#1")
        await asyncio.sleep(0.8)
        return asked, a.floor.owner()
    asked, owner = run(go())
    assert asked == [] and owner is None


def test_busy_reply_goes_to_whoever_spoke():
    async def go():
        a = make()
        a._turn = asyncio.create_task(asyncio.sleep(1))  # a web client's turn is running
        a._out = {"mini": False, "client": "phone"}
        events = a.subscribe()
        await hear(a, "Claude, what time is it")  # someone in the room, via the mic
        a._turn.cancel()
        says = []
        while not events.empty():
            ev = events.get_nowait()
            if ev["type"] == "say":
                says.append(ev)
        return a.speaker.said, says
    said, says = run(go())
    assert said == ["I'm still working on the last request. Say stop to cancel it."] and says == []


def test_typed_stop_and_reset_are_handled_not_sent_to_claude():
    async def go():
        a = make()
        sent = []
        a.start_turn = lambda text, speak=True, client=None: sent.append(text) or True
        resets = []

        async def fake_reset():
            resets.append(1)
        a.reset = fake_reset
        await a.submit_text("new conversation", speak=False, client="phone")
        await asyncio.sleep(0.01)
        await a.submit_text("what's the weather", speak=False, client="phone")
        return sent, resets
    sent, resets = run(go())
    assert resets == [1] and sent == ["what's the weather"]


def test_typed_reply_reaches_a_listening_session():
    async def go():
        a = make()
        task = asyncio.create_task(a.discuss("desk#1", "Deploy?", listen=True, timeout=3))
        await asyncio.sleep(0.05)
        await a.submit_text("yes, go ahead", speak=False, client="phone")
        return await task
    assert run(go()) == {"status": "ok", "text": "yes, go ahead"}


def test_bare_stop_while_listening_releases_the_floor_hold():
    async def go():
        a = make()
        task = asyncio.create_task(a.discuss("desk#1", "Deploy?", listen=True, timeout=3, hold=True))
        await asyncio.sleep(0.05)
        await hear(a, "stop", started=time.monotonic())
        r = await task
        return r, a.floor.owner()
    r, owner = run(go())
    assert r["status"] == "stopped" and owner is None


def test_timer_does_not_take_the_rooms_name():
    async def go():
        a = make()
        a.floor.try_acquire("room")
        a.floor.release("room", hold=True, ttl=0.3)  # room's follow-up hold
        a._timer_done("tea")
        await asyncio.sleep(0.1)
        early = list(a.speaker.said)
        await asyncio.sleep(0.8)
        return early, a.speaker.said
    early, later = run(go())
    assert early == [] and later == ["Your tea timer is done."]
