"""Review findings in assistant.py, each reproduced through a real Assistant (fake speaker
and ASR, no audio, no Claude)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from test_routing import SEND, _answer, _shared, hear, make, run

import scout.assistant as assistant_mod
import scout.memory as memory_mod
from scout import ledger, local_intents
from scout.audio import Utterance
from scout.gate import AudioStats
from scout.mac import ToolError


def _no_claude_side_effects(monkeypatch: pytest.MonkeyPatch, a: Any) -> None:
    async def nothing() -> None:
        return None

    monkeypatch.setattr(a.brain, "interrupt", nothing)
    monkeypatch.setattr(a.brain, "reset", nothing)


def test_memory_service_down_still_asks_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3: scout-memory stopped: the turn goes ahead without memories."""

    async def go() -> tuple[list[str], list[str]]:
        r = make()
        r.a.cfg.claude.local_first = False
        _no_claude_side_effects(monkeypatch, r.a)
        asked: list[str] = []

        async def fake_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            asked.append(text)
            yield "text", "Sunny and 70."

        def down(request: str, limit: int = 5) -> list[str]:
            raise ToolError("The memory service isn't running.")

        monkeypatch.setattr(r.a.brain, "ask", fake_ask)
        monkeypatch.setattr(r.a.memory, "relevant", down)
        r.a.start_turn("what's the weather")
        assert r.a._turn is not None
        await r.a._turn
        return asked, r.spk.said

    asked, said = run(go())
    assert len(asked) == 1 and asked[0].endswith("what's the weather")
    assert "Sunny and 70." in said
    assert "Sorry, something went wrong talking to Claude." not in said


async def _ticks_during(coro: Any) -> int:
    """How often the event loop got to run while `coro` ran."""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    await coro
    t.cancel()
    return ticks


def test_slow_memory_service_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """#10: memories (every Claude turn) and memory.answer (every local attempt) are
    blocking HTTP calls; they must not freeze the loop ('stop' must still be heard)."""

    def slow_relevant(request: str, limit: int = 5) -> list[str]:
        time.sleep(0.3)
        return []

    def slow_answer(text: str, memory: Any) -> str | None:
        time.sleep(0.3)
        return None

    async def go() -> tuple[int, int]:
        r = make()
        monkeypatch.setattr(r.a.memory, "relevant", slow_relevant)
        monkeypatch.setattr(memory_mod, "answer", slow_answer)
        context = await _ticks_during(r.a._with_context("tell me a story about boats"))
        local = await _ticks_during(r.a._answer_locally("tell me a story about boats"))
        return context, local

    context, local = run(go())
    assert context >= 5 and local >= 5


def test_a_stopped_room_turn_does_not_decline_a_later_session_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """#4: 'Scout, stop' during a room turn left _silence_turn set, so a desk session's
    mail_send was reported declined without the user being asked."""
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        r.a.cfg.claude.local_first = False
        _no_claude_side_effects(monkeypatch, r.a)
        gate = asyncio.Event()

        async def slow_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            await gate.wait()
            if False:
                yield "text", ""

        monkeypatch.setattr(r.a.brain, "ask", slow_ask)
        r.a.start_turn("summarize the news")
        await asyncio.sleep(0.02)
        await hear(r, "Scout, stop")
        gate.set()
        assert r.a._turn is not None
        await r.a._turn
        task = asyncio.create_task(r.a.run_shared_tool("proj#42", "mail_send", SEND))
        return await _answer(r, task, "yes"), r.spk.said

    result, said = run(go())
    assert result == {"status": "ok", "text": "done"}
    assert "From proj: Send email to sam@example.com, subject Lunch?" in said
    assert ran == [SEND]


def test_a_session_question_waits_for_the_floor_so_the_yes_is_not_misrouted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#5: session A listens for its own answer; session B's mail_send must not be asked
    over it (the 'yes' went to A and B timed out)."""
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[bool, dict[str, Any], bool, dict[str, Any]]:
        r = make()
        r.a.cfg.claude.confirm_timeout_s = 1.0
        a_task = asyncio.create_task(r.a.discuss("desk#1", "Delete the branch?", listen=True, timeout=2))
        await asyncio.sleep(0.05)
        b_task = asyncio.create_task(r.a.run_shared_tool("proj#42", "mail_send", SEND))
        await asyncio.sleep(0.05)
        asked_over = any(s.startswith("From proj") for s in r.spk.said)
        await hear(r, "yes", started=time.monotonic())
        a = await a_task
        await asyncio.sleep(0.6)  # A gave up the floor: B asks now (Floor polls every 0.5 s)
        asked_after = any(s.startswith("From proj") for s in r.spk.said)
        b = await _answer(r, b_task, "yes")
        return asked_over, a, asked_after, b

    asked_over, a, asked_after, b = run(go())
    assert not asked_over
    assert a == {"status": "ok", "text": "yes"}
    assert asked_after and b == {"status": "ok", "text": "done"}
    assert ran == [SEND]


def test_echo_path_queued_request_is_answered_in_the_room() -> None:
    """#8: a stale _queued_out from an earlier phone request sent the room's request
    (said over the end of a reply) to that phone."""

    async def go() -> tuple[str | None, dict[str, Any]]:
        r = make()
        r.a._queued_out = {"mini": False, "client": "c1"}  # left by an earlier phone request
        now = time.monotonic()
        r.spk.played = [(now - 2, now, "Here is the weather for today.")]
        turn = asyncio.create_task(asyncio.sleep(1))
        r.a._turn = turn
        await hear(r, "Here is the weather for today. Scout, and tomorrow?", echo=True, started=now - 2)
        turn.cancel()
        return r.a._queued, r.a._queued_out

    queued, out = run(go())
    assert queued == "and tomorrow" and out == {"mini": True, "client": None}


def test_queued_destination_is_cleared_once_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """#8: once a queued request is started, its destination doesn't linger."""

    async def go() -> dict[str, Any]:
        r = make()
        r.a.cfg.claude.local_first = False
        r.a.cfg.claude.request_context = False

        async def fake_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            yield "text", "Okay."

        monkeypatch.setattr(r.a.brain, "ask", fake_ask)
        monkeypatch.setattr(r.a, "_start_queued", lambda text, out: None)
        r.a.start_turn("first thing")
        r.a._queued, r.a._queued_out = "second thing", {"mini": False, "client": "c1"}
        assert r.a._turn is not None
        await r.a._turn
        return r.a._queued_out

    assert run(go()) == {"mini": True, "client": None}


def test_quiet_speech_without_the_wake_word_does_not_rejoin_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """#9: the rejoin path skipped every gate check: TV speech in the first 6 s
    interrupted the turn and was asked as part of the request."""

    async def go() -> tuple[bool, str | None]:
        r = make()
        r.a.cfg.claude.local_first = False
        _no_claude_side_effects(monkeypatch, r.a)
        gate = asyncio.Event()

        async def slow_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            await gate.wait()
            if False:
                yield "text", ""

        monkeypatch.setattr(r.a.brain, "ask", slow_ask)
        r.a._ref_db = -25.0  # the user's voice level
        r.a.start_turn("What time do those winds...")
        await asyncio.sleep(0.02)
        r.asr.next = "and now the weather on channel five"
        tv = AudioStats(voiced_ms=1500, level_db=-48, floor_db=-60)  # far quieter than the user
        now = time.monotonic()
        await r.a._handle(Utterance(b"\0\0" * 1600, now, False, tv, now + 1.5))
        silenced, queued = r.a._silence_turn, r.a._queued
        gate.set()
        assert r.a._turn is not None
        await r.a._turn
        return silenced, queued

    silenced, queued = run(go())
    assert not silenced and queued is None


def test_a_local_answer_is_not_spoken_after_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """#11: 'Scout, stop' while a local tier works; its answer was spoken anyway."""

    async def go() -> list[str]:
        r = make()
        _no_claude_side_effects(monkeypatch, r.a)
        done = asyncio.Event()

        async def slow_local(text: str, ctx: Any) -> str | None:
            await done.wait()
            return "You have three new emails."

        monkeypatch.setattr(local_intents, "answer", slow_local)
        r.a.start_turn("summarize my mail")
        await asyncio.sleep(0.02)
        await hear(r, "Scout, stop")
        done.set()
        assert r.a._turn is not None
        await r.a._turn
        return r.spk.said

    assert "You have three new emails." not in run(go())


def test_a_repeat_logs_cleanly_and_is_not_a_ledger_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """#12: 'say that again' logged '(tier %d)' with None (a logging error) and wrote a
    tier 'None' row to the ledger."""
    messages: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())  # raises on a bad format

    recorded: list[str] = []
    monkeypatch.setattr(ledger, "record", lambda tier, *a, **k: recorded.append(tier))
    handler = Grab()
    logger = logging.getLogger("scout.assistant")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:

        async def go() -> list[str]:
            r = make()
            r.a.last_spoken = ("Scout", "It's sunny.")
            assert await r.a._answer_locally("say that again")
            return r.spk.said

        said = run(go())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    assert said == ["It's sunny."]
    assert any("tier None" in m for m in messages)
    assert recorded == []


def test_repeating_a_private_answer_stays_private() -> None:
    """#15: a memory answer is private; 'say that again' re-recorded it as not private,
    so strict mode passed it to Claude in the next request's context."""

    async def go() -> list[bool]:
        r = make()
        r.a.memory.add("the boat is called Blue Heron")
        assert await r.a._answer_locally("what do you remember")
        assert await r.a._answer_locally("say that again")
        return [private for _, _, _, private in r.a.recent_local]

    assert run(go()) == [True, True]


def test_slow_mail_prefetch_does_not_hold_up_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """#33: the mail listing ran unbounded (12-15 s on a big inbox) before Claude was asked."""
    monkeypatch.setattr(assistant_mod, "PREFETCH_DEADLINE_S", 0.05, raising=False)

    async def go() -> tuple[float, str]:
        r = make()

        async def slow_list(count: Any = 10, **kw: Any) -> str:
            await asyncio.sleep(30)
            return "1. Lunch from Sam"

        monkeypatch.setattr(r.a.privacy, "mail_list", slow_list)
        t0 = time.monotonic()
        ctx = await asyncio.wait_for(r.a._with_context("check my email"), 2)
        return time.monotonic() - t0, ctx

    took, ctx = run(go())
    assert took < 1.0 and "Newest email" not in ctx and ctx.endswith("check my email")


@pytest.mark.parametrize("error", [ToolError("The memory service isn't running."), OSError("refused")])
def test_memory_outage_degrades_every_memory_call(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """#3: memory_client fails fast with ToolError (or OSError): the context goes without
    memories, a local attempt falls through to Claude, and the turn record is skipped."""

    def down(*a: Any, **k: Any) -> Any:
        raise error

    async def go() -> tuple[str, bool]:
        r = make()
        monkeypatch.setattr(r.a.memory, "relevant", down)
        monkeypatch.setattr(r.a.memory, "matching", down)
        monkeypatch.setattr(r.a.index, "put", down)
        ctx = await r.a._with_context("who is Sam")
        handled = await r.a._answer_locally("who is Sam")  # the memory's "who is" rule
        await r.a._record_turn("who is Sam", "a friend", "claude", False)
        return ctx, handled

    ctx, handled = run(go())
    assert ctx.endswith("who is Sam") and "memories" not in ctx
    assert handled is False
