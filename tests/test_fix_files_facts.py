"""facts.md fixes: the user's own text survives a save, a deleted file is noticed, a hand
edit during an add isn't lost, old JSON with odd times loads, and the oldest facts go
first when there are too many."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scout import memory
from scout.memory import Memory, parse_facts

HAND = (
    "# My facts\n\n"
    "Things about the RV, kept by hand.\n\n"
    "## People\n"
    "- Pat is my manager · 2026-10-01 09:00\n"
    "  - sub point about Pat\n"
    "- Sam likes tea · 2026-10-02 09:00\n"
    "\n"
    "Free text at the end\n"
)


def later(p: Path, by: float = 5) -> None:
    t = time.time() + by
    os.utime(p, (t, t))


def test_only_column_zero_bullets_are_facts() -> None:
    assert [f["text"] for f in parse_facts(HAND)] == ["Pat is my manager", "Sam likes tea"]


def test_add_and_forget_keep_the_users_own_text(tmp_path: Path) -> None:
    p = tmp_path / "facts.md"
    p.write_text(HAND)
    mem = Memory(p)
    mem.add("the RV is 36 feet")
    text = p.read_text()
    assert text.startswith(HAND) and text[len(HAND) :].startswith("- the RV is 36 feet · ")
    assert mem.forget("Sam") == ["Sam likes tea"]
    text = p.read_text()
    assert "Sam likes tea" not in text
    for kept in ("# My facts", "Things about the RV", "## People", "  - sub point about Pat", "Free text at the end",
                 "- Pat is my manager · 2026-10-01 09:00"):  # fmt: skip
        assert kept in text
    assert [f["text"] for f in Memory(p).facts] == ["Pat is my manager", "the RV is 36 feet"]


def test_a_deleted_facts_file_is_noticed(tmp_path: Path) -> None:
    p = tmp_path / "facts.md"
    mem = Memory(p)
    mem.add("Pat is my manager")
    seen: list[int] = []
    mem.on_change = lambda: seen.append(len(mem.facts))
    p.unlink()
    assert mem.newest() == [] and seen == [0]
    mem.add("Sam likes tea")
    assert "Pat" not in p.read_text()


def test_a_hand_edit_during_an_add_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "facts.md"
    mem = Memory(p)
    mem.add("Pat is my manager")
    p.write_text(p.read_text() + "- My dentist is Dr. Okafor\n")
    later(p)
    monkeypatch.setattr(mem, "refresh", lambda: None)  # the edit lands after the add's refresh
    mem.add("Sam likes tea")
    text = p.read_text()
    assert "Dr. Okafor" in text and "Sam likes tea" in text and "Pat is my manager" in text


def test_old_json_with_odd_times_loads(tmp_path: Path) -> None:
    old = tmp_path / "memory.json"
    old.write_text(json.dumps({"facts": [{"text": "Sam likes tea", "ts": "yesterday"},
                                         {"text": "Pat is my manager", "ts": [1]},
                                         {"text": "The RV is 36 feet", "ts": "1760000000"}]}))  # fmt: skip
    mem = Memory(tmp_path / "facts.md", legacy=old)
    assert [f["text"] for f in mem.facts] == ["Sam likes tea", "Pat is my manager", "The RV is 36 feet"]
    plain = Memory(old.with_suffix(".json.moved"))  # read as JSON directly
    assert [d["ts"] for d in plain.as_documents()] == [0.0, 0.0, 1760000000.0]


def test_too_many_facts_drop_the_oldest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "MAX_FACTS", 3)
    p = tmp_path / "facts.md"
    # File order isn't age order: the newest fact is first.
    p.write_text(
        "- alpha fact · 2026-10-05 09:00\n- bravo fact · 2026-10-01 09:00\n- charlie fact · 2026-10-03 09:00\n"
    )
    mem = Memory(p)
    mem.add("delta fact")
    assert [f["text"] for f in mem.facts] == ["alpha fact", "charlie fact", "delta fact"]
