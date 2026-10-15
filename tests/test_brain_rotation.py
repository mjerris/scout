"""The room's Claude session starts fresh after a quiet spell or once it carries too much:
Scout puts what matters (recent spoken items and their records, memories, recall) into
each request itself, so an hours-long session is only cost and stale context."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from scout.brain import Brain
from scout.config import ClaudeConfig
from scout.rules import Rules
from scout.tools import Timers, build_server


async def never(spoken: str, detail: str) -> bool | None:
    return None


def brain(tmp_path: Path, **kw: Any) -> Brain:
    server, names = build_server(Timers(lambda label: None))
    return Brain(ClaudeConfig(**kw), never, server, names, Rules(tmp_path / "r.json"), lambda k, t: None)


def test_a_quiet_spell_starts_a_fresh_session(tmp_path: Path) -> None:
    b = brain(tmp_path, session_idle_min=20)
    b._used(now=1000.0, context_tokens=5000)
    assert not b._stale(now=1000.0 + 19 * 60)
    assert b._stale(now=1000.0 + 21 * 60)


def test_a_large_session_starts_fresh(tmp_path: Path) -> None:
    b = brain(tmp_path, session_max_tokens=60000)
    b._used(now=1000.0, context_tokens=59000)
    assert not b._stale(now=1001.0)
    b._used(now=1002.0, context_tokens=61000)
    assert b._stale(now=1003.0)


def test_zero_turns_either_off(tmp_path: Path) -> None:
    b = brain(tmp_path, session_idle_min=0, session_max_tokens=0)
    b._used(now=0.0, context_tokens=10**7)
    assert not b._stale(now=10**9)


def test_a_new_session_has_nothing_to_go_stale(tmp_path: Path) -> None:
    assert not brain(tmp_path)._stale(now=10**9)


def test_ensure_drops_a_stale_client(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    from scout import brain as brain_mod

    made: list[Any] = []

    class FakeClient:
        def __init__(self, opts: Any) -> None:
            self.closed = False
            made.append(self)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.closed = True

    monkeypatch.setattr(brain_mod, "ClaudeSDKClient", FakeClient)
    b = brain(tmp_path, session_max_tokens=100)
    first: Any = asyncio.run(b._ensure())
    assert asyncio.run(b._ensure()) is first  # nothing used yet: kept
    b._used(now=time.time(), context_tokens=500)
    second: Any = asyncio.run(b._ensure())
    assert second is not first and made[0].closed and len(made) == 2 and b._context == 0
