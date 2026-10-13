"""scout-memory's start: a file it can't index doesn't stop the service, and old
conversations move into the journal once, with Claude's answers from mail kept private."""

import asyncio
import dataclasses
import datetime as dt
import types
from pathlib import Path
from typing import Any

import pytest

from scout import config, index, journal, memory_service


def at(h: int, m: int, s: int = 0) -> float:
    return dt.datetime(2026, 10, 13, h, m, s).timestamp()


def test_the_service_keeps_running_when_syncing_files_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    cfg = dataclasses.replace(cfg, index=dataclasses.replace(cfg.index, semantic=False))
    monkeypatch.setattr(memory_service, "load", lambda: cfg)
    calls: list[int] = []

    def broken(self: index.Index) -> int:
        calls.append(1)
        raise RuntimeError("disk trouble")

    monkeypatch.setattr(index.Index, "sync_files", broken)
    real_sleep = asyncio.sleep
    fast = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("__")})
    fast.sleep = lambda s: real_sleep(0.01)
    monkeypatch.setattr(memory_service, "asyncio", fast)

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                memory_service.serve("127.0.0.1", 0, tmp_path / "m.db", tmp_path / "memory"), 0.5
            )

    asyncio.run(run())
    assert len(calls) >= 2  # at start, and again from the loop


def _old_turn(ix: index.Index, ts: float, q: str, a: str, meta: dict[str, Any]) -> str:
    id = f"turn:{ts}"
    ix.put(id, "turn", q, f"{q}\nScout: {a}", ts=ts, meta=meta)
    return id


def test_old_answers_by_claude_move_into_the_journal_as_private(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", None)
    ix.journal_root = tmp_path / "memory"
    _old_turn(ix, at(9, 0), "any mail from Pat?", "Pat says the meeting moved to 3.", {"by": "claude"})
    _old_turn(ix, at(9, 5), "is starlink down", "Starlink is online.", {"by": "local"})
    assert memory_service.migrate_turns(ix) == 2
    text = journal.path_for(tmp_path / "memory", at(9, 0)).read_text()
    assert "Pat says the meeting moved to 3." not in text
    assert "Starlink is online." in text


def test_a_migration_cut_short_doesnt_duplicate_entries(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", None)
    ix.journal_root = tmp_path / "memory"
    _old_turn(ix, at(9, 0), "is starlink down", "Starlink is online.", {"by": "local"})
    ix.journal_add(at(9, 0), "room", "is starlink down", "Starlink is online.")  # moved, not yet deleted
    memory_service.migrate_turns(ix)
    text = journal.path_for(tmp_path / "memory", at(9, 0)).read_text()
    assert text.count("is starlink down") == 1
    assert ix.get(f"turn:{at(9, 0)}") is None and ix.count("turn") == 1
