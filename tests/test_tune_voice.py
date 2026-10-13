"""Latency and memory on the room voice path: the per-turn timing line, the room's
ledger rows, the context fetched alongside tier 1, the progress line yielding to the
answer, a long first sentence split at a comma, the segmenter off the event loop, and
MLX's cache limit. Fakes throughout; synthetic text only."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import numpy as np
import pytest
from test_routing import LOUD, FakeIO, FakeSpeaker, Rig, make, run

import scout.assistant as assistant_mod
import scout.tts as tts_mod
from scout import ledger, local_intents, memory, tier1
from scout.asr import MLX_CACHE_LIMIT, Transcriber
from scout.audio import Utterance, utterances
from scout.audio_io import AudioIO
from scout.tts import Speaker, split_first

# --- the speaker: splitting the first sentence, dropping a stale progress line ----------


class FakeKokoro:
    """0.1 s of silence per piece, synthesized instantly."""

    def __init__(self, model: str, voices: str) -> None:
        self.made: list[str] = []

    def create(self, text: str, voice: str, speed: float) -> tuple[np.ndarray, int]:
        self.made.append(text)
        return np.zeros(2400, np.float32), 24000

    def get_voices(self) -> list[str]:
        return ["af_heart"]


def _speaker(monkeypatch: pytest.MonkeyPatch) -> tuple[Speaker, FakeIO]:
    monkeypatch.setattr(tts_mod, "Kokoro", FakeKokoro)
    io = FakeIO()
    spk = Speaker("model", "voices", "af_heart", 1.0, cast(AudioIO, io), 300, chimes=False)
    spk.start()
    return spk, io


def _played(spk: Speaker) -> list[str]:
    return [text for _, _, text in spk._played]


LONG = "The forecast for tomorrow looks mostly sunny and dry, with a high near seventy and light winds."


def test_split_first_at_a_comma_past_forty_characters() -> None:
    assert split_first(LONG) == [
        "The forecast for tomorrow looks mostly sunny and dry,",
        "with a high near seventy and light winds.",
    ]


@pytest.mark.parametrize(
    "sentence",
    [
        "Short, and sweet.",  # not long
        "You spent 1,000 dollars on fuel and groceries over the last thirty days in total.",  # no ", "
        "Yes, I found the email from the hardware store about your order of lumber today.",  # comma too early
        "The forecast for tomorrow looks mostly sunny and dry today, right?",  # too little after it
    ],
)
def test_split_first_leaves_these_alone(sentence: str) -> None:
    assert split_first(sentence) == [sentence]


def test_a_long_first_sentence_plays_in_two_pieces_keeping_the_comma_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> tuple[list[str], list[float]]:
        spk, io = _speaker(monkeypatch)
        spk.speak(LONG + " Second sentence.")
        await spk.wait_idle()
        return _played(spk), io.played

    played, seconds = run(go())
    assert played == [*split_first(LONG), "Second sentence."]
    assert seconds == [pytest.approx(0.2), pytest.approx(0.1), pytest.approx(0.1)]  # + the comma's 0.1 s


def test_no_split_while_speech_is_already_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        spk, _ = _speaker(monkeypatch)
        spk.speak("First.")
        spk.speak(LONG)  # it waits its turn anyway: synthesized while "First." plays
        await spk.wait_idle()
        return _played(spk)

    assert run(go()) == ["First.", LONG]


def test_a_queued_progress_line_is_dropped_when_the_answer_comes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[list[str], bool]:
        spk, _ = _speaker(monkeypatch)
        spk.speak_progress("One moment.")
        spk.speak("Here is the answer.")
        await spk.wait_idle()
        return _played(spk), spk.busy

    assert run(go()) == (["Here is the answer."], False)


def test_a_progress_line_waiting_to_play_is_dropped_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        spk, _ = _speaker(monkeypatch)
        spk.speak("Earlier.")
        spk.speak_progress("One moment.")
        await asyncio.sleep(0.02)  # "Earlier." plays; the progress line is synthesized, waiting
        spk.speak("The answer.")
        await spk.wait_idle()
        return _played(spk)

    assert run(go()) == ["Earlier.", "The answer."]


def test_a_progress_line_already_playing_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[str]:
        spk, _ = _speaker(monkeypatch)
        spk.speak_progress("One moment.")
        await asyncio.sleep(0.02)
        spk.speak("The answer.")
        await spk.wait_idle()
        return _played(spk)

    assert run(go()) == ["One moment.", "The answer."]


def test_first_played_since(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[float, float | None, float | None]:
        spk, _ = _speaker(monkeypatch)
        t0 = time.monotonic()
        spk.speak("Hello there.")
        await spk.wait_idle()
        return t0, spk.first_played_since(t0), spk.first_played_since(time.monotonic())

    t0, first, none = run(go())
    assert first is not None and 0 <= first - t0 < 0.05 and none is None


# --- the room turn: timing line, ledger, context, progress --------------------------------


class TimedSpeaker(FakeSpeaker):
    """Its audio starts 250 ms after each answer is queued."""

    def first_played_since(self, since: float) -> float | None:
        return since + 0.25


def _timed() -> Rig:
    r = make()
    spk = TimedSpeaker()
    r.a.speaker = cast(Speaker, spk)
    return Rig(r.a, spk, r.asr)


def _answer_with(monkeypatch: pytest.MonkeyPatch, said: str | None) -> None:
    async def answer(text: str, ctx: Any, skip: Any = None) -> str | None:
        return said

    monkeypatch.setattr(local_intents, "answer", answer)
    monkeypatch.setattr(memory, "answer", lambda text, mem: None)


async def _heard_turn(r: Rig, text: str, reason: str = "turn", prob: float | None = 0.91) -> None:
    r.asr.next = text
    now = time.monotonic()
    await r.a._handle(Utterance(b"\0\0" * 1600, now - 1.5, False, LOUD, now - 0.2, reason, prob))
    assert r.a._turn is not None
    await r.a._turn


def _timing_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn timing:")]
    assert len(lines) == 1
    return lines[0]


def test_a_heard_turn_logs_its_timing_in_numbers_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    r = _timed()
    _answer_with(monkeypatch, "It is a synthetic noon.")
    with caplog.at_level(logging.INFO, logger="scout.assistant"):
        run(_heard_turn(r, "Scout, what time is it"))
    line = _timing_line(caplog)
    assert "end=turn p=0.91" in line and "tier=0" in line and "answer_to_audio_ms=250" in line
    fields = dict(f.split("=", 1) for f in line.removeprefix("turn timing: ").split())
    for key in ("speech_end_to_asr_done_ms", "asr_ms", "turn_to_answer_ms"):
        assert fields[key].isdigit(), (key, line)
    assert int(fields["speech_end_to_asr_done_ms"]) >= 200  # the clip ended 0.2 s before
    assert "noon" not in line and "time" not in line.replace("turn timing", "")


def test_a_typed_turn_logs_dashes_for_how_it_was_heard(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    r = _timed()
    _answer_with(monkeypatch, "Done.")

    async def go() -> None:
        r.a.start_turn("what time is it")
        assert r.a._turn is not None
        await r.a._turn

    with caplog.at_level(logging.INFO, logger="scout.assistant"):
        run(go())
    line = _timing_line(caplog)
    assert "end=- p=- speech_end_to_asr_done_ms=- asr_ms=-" in line and "tier=0" in line


class _Result:
    total_cost_usd, num_turns, is_error, session_id, usage = 0.01, 1, False, "s", None


def test_room_turns_write_ledger_rows_per_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[str] = []
    monkeypatch.setattr(ledger, "record", lambda tier, latency, **kw: rows.append(tier))
    r = _timed()
    r.a.cfg.claude.request_context = False
    _answer_with(monkeypatch, "Done.")
    run(_heard_turn(r, "Scout, set a timer"))
    _answer_with(monkeypatch, None)

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        yield "text", "Claude's answer."
        yield "result", _Result()

    monkeypatch.setattr(r.a.brain, "ask", ask)
    run(_heard_turn(r, "Scout, tell me something"))
    assert rows == ["0", "claude"]


class SlowTier1:
    def __init__(self, decision: dict[str, Any] | None, wait: float = 0.3) -> None:
        self.decision, self.wait = decision, wait

    async def decide(self, request: str, now: Any) -> dict[str, Any] | None:
        await asyncio.sleep(self.wait)
        return self.decision


def _slow_context(monkeypatch: pytest.MonkeyPatch, r: Rig, calls: list[str], wait: float = 0.3) -> None:
    r.a.cfg.claude.request_context = True

    def memories(mem: Any, text: str) -> list[str]:
        calls.append("memories")
        time.sleep(wait)
        return ["a synthetic fact"]

    async def calendar(*args: Any, **kw: Any) -> str:
        calls.append("calendar")
        await asyncio.sleep(wait)
        return "synthetic calendar"

    async def mail(*args: Any, **kw: Any) -> str:
        calls.append("mail")
        await asyncio.sleep(wait)
        return "synthetic mail"

    monkeypatch.setattr(r.a.privacy, "memories", memories)
    monkeypatch.setattr(r.a.privacy, "calendar_events", calendar)
    monkeypatch.setattr(r.a.privacy, "mail_list", mail)


def test_context_is_fetched_while_tier1_decides_and_all_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sequential this was decide 0.3 + memories 0.3 + calendar 0.3 + mail 0.3 = 1.2 s;
    now decide ∥ (memories, calendar), then mail: 0.6 s."""
    r = make()
    calls: list[str] = []
    _slow_context(monkeypatch, r, calls)
    _answer_with(monkeypatch, None)
    r.a.tier1 = cast(tier1.LocalModel, SlowTier1(None))
    asked: list[tuple[float, str]] = []

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        asked.append((time.monotonic(), text))
        yield "text", "Here you go."

    monkeypatch.setattr(r.a.brain, "ask", ask)

    async def go() -> float:
        t0 = time.monotonic()
        r.a.start_turn("what's on my calendar and in my email")
        assert r.a._turn is not None
        await r.a._turn
        return asked[0][0] - t0

    took = run(go())
    request = asked[0][1]
    assert "a synthetic fact" in request and "synthetic calendar" in request and "synthetic mail" in request
    assert (
        request.index("synthetic fact")
        < request.index("synthetic calendar")
        < request.index("synthetic mail")
    )
    assert 0.55 < took < 0.9, took


def test_no_context_when_tier1_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    r = make()
    calls: list[str] = []
    _slow_context(monkeypatch, r, calls, wait=0.05)
    _answer_with(monkeypatch, None)
    r.a.tier1 = cast(tier1.LocalModel, SlowTier1({"tool": "mail_recent", "args": {}}, wait=0.1))

    async def tier1_run(decision: dict[str, Any], now: Any, summaries: Any = None) -> str:
        return "You have one synthetic email."

    monkeypatch.setattr(tier1, "run", tier1_run)

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        raise AssertionError("tier 1 answered; Claude isn't asked")
        yield "text", ""

    monkeypatch.setattr(r.a.brain, "ask", ask)

    async def go() -> None:
        r.a.start_turn("anything new in my email")
        assert r.a._turn is not None
        await r.a._turn
        await asyncio.sleep(0.1)

    run(go())
    assert r.spk.said == ["You have one synthetic email."]
    assert "mail" not in calls and r.a._context is None


def test_a_quick_claude_answer_without_tools_gets_no_progress_line(monkeypatch: pytest.MonkeyPatch) -> None:
    r = make()
    r.a.cfg.tts.progress_delay_s = 0.05
    r.a.cfg.claude.request_context = False
    r.a.cfg.claude.local_first = False
    monkeypatch.setattr(assistant_mod, "CLAUDE_PROGRESS_S", 0.5)

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        await asyncio.sleep(0.2)  # past progress_delay_s, before CLAUDE_PROGRESS_S
        yield "text", "A quick answer."

    monkeypatch.setattr(r.a.brain, "ask", ask)

    async def go() -> list[str]:
        r.a.start_turn("tell me a fact")
        assert r.a._turn is not None
        await r.a._turn
        return r.spk.said

    assert run(go()) == ["A quick answer."]


def test_a_tool_call_lets_the_progress_line_speak_early(monkeypatch: pytest.MonkeyPatch) -> None:
    r = make()
    r.a.cfg.tts.progress_delay_s = 0.05
    r.a.cfg.claude.request_context = False
    r.a.cfg.claude.local_first = False
    monkeypatch.setattr(assistant_mod, "CLAUDE_PROGRESS_S", 5.0)

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        await asyncio.sleep(0.1)
        yield "tool", ("mcp__scout__weather", {})
        await asyncio.sleep(0.2)
        yield "text", "Sunny."

    monkeypatch.setattr(r.a.brain, "ask", ask)

    async def go() -> list[str]:
        r.a.start_turn("what's the weather")
        assert r.a._turn is not None
        await r.a._turn
        return r.spk.said

    assert run(go()) == ["Getting the weather.", "Sunny."]


# --- the segmenter on its own thread --------------------------------------------------------


class SlowSegmenter:
    """Onset, then five frames that each take 60 ms (smart-turn), then the end."""

    def __init__(self) -> None:
        self.triggered = False
        self.last_voiced_ms, self.last_level_db = 900, -30.0
        self.last_end_reason, self.last_turn_prob = "", cast(float | None, None)
        self.n = 0

    def feed(self, frame: bytes) -> bytes | bool | None:
        self.n += 1
        if self.n == 1:
            self.triggered = True
            return True
        if self.n < 7:
            time.sleep(0.06)
            return None
        if self.n == 7:
            self.triggered = False
            self.last_end_reason, self.last_turn_prob = "turn", 0.8
            return b"\1\0" * 4800
        return None


def test_the_segmenter_runs_off_the_event_loop() -> None:
    async def go() -> tuple[float, Utterance]:
        mic = FakeIO()
        for _ in range(10):
            mic.frames.put_nowait(b"\1\0" * 480)
        out: asyncio.Queue[Utterance] = asyncio.Queue()
        task = asyncio.create_task(utterances(mic, SlowSegmenter(), lambda: False, out))
        worst, last = 0.0, time.monotonic()
        while out.empty():
            await asyncio.sleep(0.005)
            now = time.monotonic()
            worst, last = max(worst, now - last), now
        task.cancel()
        return worst, out.get_nowait()

    worst, utt = run(go())
    assert worst < 0.04, f"the event loop stalled for {worst * 1000:.0f} ms"  # 60 ms on the loop before
    assert (utt.end_reason, utt.turn_prob) == ("turn", 0.8)


# --- MLX's buffer cache ---------------------------------------------------------------------


def test_the_transcriber_caps_mlx_s_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlx.core as mx

    limits: list[int] = []

    def set_cache_limit(n: int) -> int:
        limits.append(n)
        return 0

    monkeypatch.setattr(mx, "set_cache_limit", set_cache_limit)
    Transcriber("model", "en")
    assert limits == [MLX_CACHE_LIMIT] and MLX_CACHE_LIMIT == 256 * 1024 * 1024
