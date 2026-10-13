"""The store under load and when files move: the matrix builds in bulk, renamed and
duplicate files keep their items, an unreadable file is skipped, a sync of one file
can't lose entries, search survives a broken embedder, and embedding stays bounded."""

import datetime as dt
import sys
import threading
import time
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from scout import index, journal

from test_index import FakeEmbedder


def at(h: int, m: int, s: int = 0, day: int = 13) -> float:
    return dt.datetime(2026, 10, day, h, m, s).timestamp()


def files_ix(tmp_path: Path, embedder: Any = None) -> index.Index:
    ix = index.Index(tmp_path / "m.db", embedder)
    ix.journal_root = tmp_path / "memory"
    return ix


# --- 1: the matrix -------------------------------------------------------------------


def test_loading_the_matrix_does_not_rescan_rows_per_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ix = index.Index(tmp_path / "m.db", FakeEmbedder())
    for i in range(1500):
        ix.put(f"d{i}", "note", f"item {i}", f"body {i}", ts=i)
    while ix.embed_pending(512):
        pass
    sums = 0
    real = index.Matrix.drop

    def counting(self: index.Matrix, id: str) -> None:
        nonlocal sums
        sums += 1
        real(self, id)

    monkeypatch.setattr(index.Matrix, "drop", counting)
    ix._matrix = None
    with ix._lock:
        m = ix._loaded_matrix()
    assert len(m) == 1500 and sums == 0  # built in bulk, not set() item by item
    assert m.top(FakeEmbedder().embed(["item 7"])[0], 1)[0][0] == "d7"


def test_dropping_an_unknown_id_costs_nothing() -> None:
    m = index.Matrix(2)
    for i in range(2000):
        m.set(f"x{i}", [np.array([1.0, 0.0], np.float32)], "note", 0.0)

    m.alive = m.alive.view(_NoSum)
    m.drop("nobody")  # no rows: no check at all
    for i in range(1500):
        m.drop(f"x{i}")
    assert len(m) == 500 and len(m.ids) < 2000  # compacted once past half dead


class _NoSum(np.ndarray):
    def sum(self, *a: Any, **k: Any) -> Any:
        raise AssertionError("summed the alive rows")


# --- 2: renamed and duplicate files --------------------------------------------------


def test_a_renamed_note_file_keeps_its_note(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    ix.note_add("Awning", "The left arm squeaks.")
    p = next((tmp_path / "memory" / "notes").glob("0001-*.md"))
    p.rename(p.with_name("0001-awning-arm.md"))
    ix.sync_files()
    h = ix.get("note:1")
    assert h is not None and h.title == "Awning"
    assert ix.sync_files() == 0 and ix.get("note:1") is not None


def test_a_renamed_task_file_keeps_its_task(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    ix.task_add("Send Pat the budget", {"due": "2026-10-20"})
    p = next((tmp_path / "memory" / "tasks").glob("0001-*.md"))
    p.rename(p.with_name("0001-budget.md"))
    ix.sync_files()
    assert ix.get("task:1") is not None


def test_two_note_files_with_one_number_lose_nothing_when_one_goes(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    d = tmp_path / "memory" / "notes"
    d.mkdir(parents=True)
    (d / "0003-a.md").write_text("# Alpha\n\nfirst\n")
    (d / "0003-b.md").write_text("# Beta\n\nsecond\n")
    ix.sync_files()
    assert ix.get("note:3") is not None
    (d / "0003-b.md").unlink()
    ix.sync_files()
    h = ix.get("note:3")
    assert h is not None and h.title == "Alpha"  # what's left on disk
    (d / "0003-a.md").unlink()
    ix.sync_files()
    assert ix.get("note:3") is None


def test_two_journal_files_for_one_day_keep_each_others_entries(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    root = tmp_path / "memory"
    ix.journal_add(at(9, 0), "room", "first question", "first answer")
    p = journal.path_for(root, at(9, 0))
    copy = root / "journal" / "2026" / "11" / p.name  # a misplaced copy, with one more entry
    copy.parent.mkdir(parents=True)
    copy.write_text(p.read_text() + "## 10:00:00 · room\n**Q:** second question\n**A:** ok\n\n")
    ix.sync_files()
    assert ix.get("journal:2026-10-13T09:00:00") is not None
    assert ix.get("journal:2026-10-13T10:00:00") is not None
    copy.unlink()
    ix.sync_files()
    assert ix.get("journal:2026-10-13T09:00:00") is not None  # still in the first file
    assert ix.get("journal:2026-10-13T10:00:00") is None
    p.rename(p.with_name("2026-10-13.md.bak"))
    ix.sync_files()
    assert ix.count("turn") == 0


def test_an_edit_that_drops_a_shared_entry_keeps_the_other_files_copy(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    root = tmp_path / "memory"
    ix.journal_add(at(9, 0), "room", "shared question", "answer")
    p = journal.path_for(root, at(9, 0))
    copy = root / "journal" / "2026" / "11" / p.name
    copy.parent.mkdir(parents=True)
    copy.write_text(p.read_text())
    ix.sync_files()
    copy.write_text(copy.read_text().split("## ")[0])  # the copy loses its entry
    ix.sync_files()
    assert ix.get("journal:2026-10-13T09:00:00") is not None


# --- 3: an unreadable file -----------------------------------------------------------


def test_an_unreadable_file_is_skipped_and_the_rest_indexed(tmp_path: Path) -> None:
    ix = files_ix(tmp_path)
    d = tmp_path / "memory" / "notes"
    d.mkdir(parents=True)
    (d / "0001-bad.md").write_bytes(b"# Bad \xff\xfe bytes\n\nstill a note\n")
    (d / "0002-good.md").write_text("# Good\n\nfine\n")
    day = tmp_path / "memory" / "journal" / "2026" / "10"
    day.mkdir(parents=True)
    (day / "2026-10-13.md").write_bytes(
        b"# Day\n\n## 09:00:00 \xc2\xb7 room\n**Q:** caf\xe9 hours\n**A:** 8\n\n"
    )
    ix.sync_files()
    assert ix.get("note:2") is not None
    bad = ix.get("note:1")
    assert bad is not None and "still a note" in bad.body  # read with the bad bytes replaced
    assert ix.get("journal:2026-10-13T09:00:00") is not None


def test_a_file_that_fails_is_retried_and_doesnt_stop_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ix = files_ix(tmp_path)
    d = tmp_path / "memory" / "notes"
    d.mkdir(parents=True)
    (d / "0001-a.md").write_text("# A\n\na\n")
    (d / "0002-b.md").write_text("# B\n\nb\n")
    real = index.Index.sync_file

    def flaky(self: index.Index, p: Path) -> int:
        if p.name == "0001-a.md":
            raise FileNotFoundError(p)  # deleted between the listing and the read
        return real(self, p)

    monkeypatch.setattr(index.Index, "sync_file", flaky)
    ix.sync_files()
    assert ix.get("note:2") is not None and ix.get("note:1") is None
    monkeypatch.setattr(index.Index, "sync_file", real)
    ix.sync_files()  # not recorded as done, so the next pass picks it up
    assert ix.get("note:1") is not None


# --- 4: concurrent syncs of one file -------------------------------------------------


def test_concurrent_syncs_of_one_file_lose_no_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ix = files_ix(tmp_path)
    root = tmp_path / "memory"
    ix.journal_add(at(9, 0), "room", "q1", "a1")
    p = journal.path_for(root, at(9, 0))
    gate = threading.Event()
    real_parse = journal.parse

    def slow_parse(path: Path, text: str | None = None) -> list[dict[str, Any]]:
        out = real_parse(path, text)
        if threading.current_thread().name == "slow":
            gate.wait(2)  # parsed the old file; the other writer finishes meanwhile
        return out

    monkeypatch.setattr(journal, "parse", slow_parse)
    t = threading.Thread(target=ix.sync_file, args=(p,), name="slow")
    t.start()
    time.sleep(0.1)
    done = threading.Thread(target=lambda: ix.journal_add(at(10, 0), "room", "q2", "a2"))
    done.start()
    time.sleep(0.2)
    gate.set()
    t.join(5)
    done.join(5)
    monkeypatch.setattr(journal, "parse", real_parse)
    assert ix.get("journal:2026-10-13T10:00:00") is not None
    ix.sync_files()  # and nothing is left for a later pass to find
    assert ix.get("journal:2026-10-13T10:00:00") is not None
    assert ix.count("turn") == 2


# --- 5: an embedder that can't load --------------------------------------------------


class BrokenEmbedder:
    model = "broken@1"
    loaded = False

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        raise OSError("model files missing and no network")


def test_search_without_a_working_embedder_returns_text_hits(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", BrokenEmbedder())
    ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=1)
    assert [h.id for h in ix.search("manager")] == ["m1"]


def test_a_failed_model_load_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    tries: list[int] = []

    class TextEmbedding:
        def __init__(self, *a: Any, **k: Any) -> None:
            tries.append(1)
            raise OSError("offline")

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding))
    emb = index.LocalEmbedder()
    with pytest.raises(OSError):
        emb.embed(["x"])
    first = len(tries)
    with pytest.raises(index.EmbedderUnavailable):
        emb.embed(["x"])
    assert len(tries) == first  # no new attempt within the back-off
    assert emb._failed_at is not None
    emb._failed_at -= index.LOAD_RETRY_S + 1
    with pytest.raises(OSError):
        emb.embed(["x"])
    assert len(tries) > first


# --- 6: no copy of the matrix per search ---------------------------------------------


class _Spy(np.ndarray):
    fancy = 0

    def __getitem__(self, key: Any) -> Any:
        if self.ndim == 2 and isinstance(key, (np.ndarray, list)):  # gathering rows of the matrix
            _Spy.fancy += 1
        return super().__getitem__(key)


def test_top_scores_in_place_when_the_filter_keeps_most_rows() -> None:
    m = index.Matrix(2)
    rng = np.random.default_rng(1)
    for i in range(300):
        v = rng.normal(size=2).astype(np.float32)
        m.set(f"x{i}", [v / np.linalg.norm(v)], "note" if i % 10 else "mail", float(i))
    q = np.array([1.0, 0.0], np.float32)
    want = m.top(q, 5)
    want_notes = m.top(q, 5, kinds=["note"])
    m.vecs = m.vecs.view(_Spy)
    _Spy.fancy = 0
    assert m.top(q, 5) == want and m.top(q, 5, kinds=["note"]) == want_notes
    assert _Spy.fancy == 0
    few = m.top(q, 3, kinds=["mail"], since=250)  # a small slice: gathered rows are fine
    assert few and all(int(id[1:]) % 10 == 0 and int(id[1:]) >= 250 for id, _ in few)


# --- 7: bounded embedding ------------------------------------------------------------


def test_a_search_embeds_a_bounded_number_of_windows(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    ix = index.Index(tmp_path / "m.db", emb)
    long = " ".join(f"word{i}" for i in range(4000))  # ~20 windows each
    for i in range(32):
        ix.put(f"d{i}", "note", f"canoe trip {i}", long, ts=i)
    ix.search("canoe")
    assert sum(emb.calls) <= 64 + 1  # + the query


def test_background_batches_embed_in_bounded_calls(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    ix = index.Index(tmp_path / "m.db", emb)
    long = " ".join(f"word{i}" for i in range(4000))
    for i in range(10):
        ix.put(f"d{i}", "note", f"canoe trip {i}", long, ts=i)
    assert ix.embed_pending(64) == 10
    assert max(emb.calls) <= 64
    assert ix.embed_pending(64) == 0


# --- 8: no pending scan when nothing is pending --------------------------------------


def test_a_search_skips_the_pending_scan_when_nothing_is_pending(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", FakeEmbedder())
    ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=1)
    ix.put("m2", "memory", "Our campsite is 37", "Our campsite is 37", ts=2)
    ix.search("manager")
    ix.search("manager")  # finds nothing pending
    seen: list[str] = []
    ix._conn.set_trace_callback(seen.append)
    ix.search("manager")
    assert not [s for s in seen if "LEFT JOIN vecs v0" in s]
    ix._conn.set_trace_callback(None)
    ix.put("m3", "memory", "The awning squeaks", "The awning squeaks", ts=3)
    ix.search("awning")
    assert ix.embed_pending() == 0  # the new item was embedded by that search


# --- 9: an old model's vectors go --------------------------------------------------


def test_vectors_of_an_old_model_are_removed_once_the_new_one_is_done(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", FakeEmbedder())
    for i in range(5):
        ix.put(f"d{i}", "note", f"item {i}", f"body {i}", ts=i)
    while ix.embed_pending(64):
        pass
    ix.close()

    class NewModel(FakeEmbedder):
        model = "fake2@512"

    ix = index.Index(tmp_path / "m.db", NewModel())
    while ix.embed_pending(2):
        pass
    models = {r[0] for r in ix._conn.execute("SELECT DISTINCT model FROM vecs")}
    assert models == {"fake2@512"}
