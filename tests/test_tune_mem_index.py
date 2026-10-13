"""Memory-service performance: pending work kept in a table (no scan of the store), a
search's embedding ahead of the background's, recent() and count() on indexes, the
matrix read without the lock, SQLite settings, the embedder's batch size, the model
unloading by default, and the memory login item not being a Background process."""

import asyncio
import os
import plistlib
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest

from scout import index, memory_service
from scout.config import IndexConfig

from test_index import FakeEmbedder

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".tmp" / "tune-mem-tests"


@pytest.fixture
def scratch() -> Iterator[Path]:
    d = SCRATCH / uuid.uuid4().hex[:12]
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def pending(ix: index.Index) -> list[str]:
    with ix._lock:
        return [r[0] for r in ix._conn.execute("SELECT id FROM pending ORDER BY ts DESC")]


# --- the pending table -----------------------------------------------------------------


def test_put_and_storing_vectors_keep_the_pending_table(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", FakeEmbedder())
    ix.put("a", "note", "Alpha", "first", ts=1)
    ix.put("b", "note", "Beta", "second", ts=2)
    assert pending(ix) == ["b", "a"]
    assert ix.embed_pending(1) == 1 and pending(ix) == ["a"]  # newest first
    ix.put("b", "note", "Beta", "second", ts=2, meta={"read": True})  # same text: no new work
    assert pending(ix) == ["a"]
    ix.put("b", "note", "Beta", "second, edited", ts=2)
    assert pending(ix) == ["b", "a"]
    ix.delete("a")
    assert pending(ix) == ["b"]
    assert ix.embed_pending(10) == 1 and pending(ix) == [] and ix.embed_pending(10) == 0


def test_finding_work_never_scans_the_store(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", FakeEmbedder())
    for i in range(20):
        ix.put(f"d{i}", "note", f"item {i}", f"body {i}", ts=i)
    seen: list[str] = []
    ix._conn.set_trace_callback(seen.append)
    ix.embed_pending(5)
    ix.search("item", kinds=["note"])
    ix._conn.set_trace_callback(None)
    assert not [s for s in seen if "LEFT JOIN vecs" in s]
    plan = " ".join(
        str(r[3])
        for r in ix._conn.execute(
            "EXPLAIN QUERY PLAN SELECT d.id FROM pending p JOIN docs d ON d.id = p.id ORDER BY p.ts DESC LIMIT 5"
        )
    )
    assert "pending_ts" in plan and "TEMP B-TREE" not in plan


def test_the_first_start_after_the_upgrade_finds_the_work_once(scratch: Path) -> None:
    """A store from before table pending: what lacks vectors (or, if long, its later
    windows) is found by one scan at the first start, and not again."""
    db = scratch / "old.db"
    ix = index.Index(db, FakeEmbedder())
    ix.put("done", "note", "Done", "has its vector", ts=1)
    ix.put("long", "mail", "Long", "word " * 400, ts=2)
    ix.put("new", "note", "New", "never embedded", ts=3)
    ix.embed_pending(10)
    with ix._lock:  # as an old store: long has only its first window, new has none
        ix._conn.execute("DELETE FROM vecs WHERE id = 'long' AND part > 0")
        ix._conn.execute("DELETE FROM vecs WHERE id = 'new'")
        ix._conn.execute("DROP TABLE pending")
        ix._conn.execute("DROP TABLE settings")
    ix.close()
    ix = index.Index(db, FakeEmbedder())
    assert set(pending(ix)) == {"long", "new"}
    with ix._lock:
        ix._conn.execute("DELETE FROM pending")
    ix.close()
    assert pending(index.Index(db, FakeEmbedder())) == []  # filled once, not on every start


def test_a_new_model_finds_its_work_once(scratch: Path) -> None:
    db = scratch / "m.db"
    ix = index.Index(db, FakeEmbedder())
    ix.put("a", "note", "Alpha", "first", ts=1)
    ix.embed_pending(10)
    ix.close()

    class Other(FakeEmbedder):
        model = "other@512"

    ix = index.Index(db, Other())
    assert pending(ix) == ["a"]


# --- a search goes ahead of the background ------------------------------------------------


class SlowEmbedder(FakeEmbedder):
    """One call at a time, 10 ms a window, like the model's lock; logs each call."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.log: list[tuple[str, int]] = []

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        with self.lock:
            self.log.append(("query" if query else "docs", len(texts)))
            time.sleep(0.01 * len(texts))
            return super().embed(texts, query)


def test_a_search_waits_for_at_most_one_small_background_call(scratch: Path) -> None:
    emb = SlowEmbedder()
    ix = index.Index(scratch / "m.db", emb, min_similarity=0.3)
    ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=1)
    ix.embed_pending(10)
    for i in range(150):
        ix.put(f"mail{i}", "mail", f"mail {i}", f"note number {i}", ts=100 + i)
    bg = threading.Thread(target=ix.embed_pending, args=(150,))
    bg.start()
    time.sleep(0.05)
    t0 = time.perf_counter()
    hits = ix.search("who is my boss", kinds=["memory"])
    took = time.perf_counter() - t0
    bg.join()
    assert hits[0].id == "m1"
    assert all(n <= index.EMBED_BATCH_WINDOWS for kind, n in emb.log if kind == "docs")
    q = emb.log.index(("query", 1))
    assert q < len(emb.log) - 5  # the background was still going when the query got in
    assert took < 0.5  # one call of 8 windows is 80 ms; the whole backlog 1.5 s


def test_a_search_embeds_only_a_few_windows_of_its_own_kinds(scratch: Path) -> None:
    emb = FakeEmbedder()
    ix = index.Index(scratch / "m.db", emb, min_similarity=0.3)
    for i in range(50):
        ix.put(f"mail{i}", "mail", f"mail {i}", f"note number {i}", ts=100 + i)
    ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=1)
    assert ix.search("who is my boss", kinds=["memory"])[0].id == "m1"
    assert emb.calls == [1, 1]  # the new memory, then the query: no mail
    ix.search("note", kinds=["mail"])  # 50 text hits without vectors
    assert emb.calls[2] == index.MAX_WINDOWS_PER_SEARCH == 8
    long = " ".join(f"word{i}" for i in range(4000))  # ~20 windows
    ix.put("big", "note", "canoe trip", long, ts=500)
    hits = ix.search("canoe", kinds=["note"])
    assert emb.calls[4:] == [1] and hits[0].how == "text"  # too long now: the background does it


def test_the_background_embeds_new_text_within_a_second_or_two(scratch: Path) -> None:
    emb = FakeEmbedder()
    ix = index.Index(scratch / "m.db", emb)

    async def run() -> None:
        task = asyncio.create_task(ix.background(lambda: True, every_s=60))
        await asyncio.sleep(0.8)  # warmed up, nothing to do: waiting its 60 s
        ix.put("a", "note", "Alpha", "first", ts=1)
        for _ in range(40):
            await asyncio.sleep(0.1)
            if not pending(ix):
                break
        task.cancel()

    asyncio.run(run())
    assert pending(ix) == []


# --- recent() and count() ----------------------------------------------------------------


def _plan(ix: index.Index, sql: str, args: Sequence[object]) -> str:
    return " ".join(str(r[3]) for r in ix._conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(args)))


@pytest.mark.parametrize(
    ("kinds", "since", "uses"),
    [
        (None, None, "docs_ts"),
        (["mail"], None, "docs_kind_ts"),
        (["mail"], 5.0, "docs_kind_ts"),
        (None, 5.0, "docs_ts"),
    ],
)
def test_recent_reads_an_index_backwards(
    scratch: Path, kinds: list[str] | None, since: float | None, uses: str
) -> None:
    ix = index.Index(scratch / "m.db", None)
    plan = _plan(ix, *index._recent_sql(kinds, since, 20))
    assert uses in plan and "TEMP B-TREE" not in plan


def test_recent_of_several_kinds_reads_each_kinds_newest(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", None)
    plan = _plan(ix, *index._recent_sql(["mail", "turn"], 5.0, 20))
    assert plan.count("docs_kind_ts") == 2 and "SCAN docs" not in plan


def test_recent_and_count_answer_as_before(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", None)
    kinds = ["mail", "turn", "note"]
    for i in range(60):
        ix.put(f"d{i}", kinds[i % 3], f"t{i}", "b", ts=float((i * 7) % 60))
    assert ix.count() == 60 and ix.count("mail") == 20 and ix.count("nothing") == 0
    every = sorted((h for i in range(60) if (h := ix.get(f"d{i}"))), key=lambda h: -h.ts)
    for ks, since in ((None, None), (["mail"], None), (["mail", "note"], 30.0), (["turn", "turn"], None)):
        want = [h.id for h in every if (not ks or h.kind in ks) and (since is None or h.ts >= since)][:7]
        assert [h.id for h in ix.recent(ks, 7, since)] == want
    ix.put("d0", "note", "t0", "b", ts=0)  # mail -> note
    ix.delete("d1")
    ix.put("x", "turn", "new", "b", ts=1)
    assert ix.prune("mail", 30) > 0
    assert ix.sync("note", []) == (0, 21)
    with ix._lock:
        real = dict(ix._conn.execute("SELECT kind, count(*) FROM docs GROUP BY kind"))
    for k in [*kinds, "nothing"]:
        assert ix.count(k) == real.get(k, 0)
    assert ix.count() == sum(real.values())


# --- the matrix ---------------------------------------------------------------------------


def test_the_matrix_is_read_without_the_lock_and_keeps_changes_made_meanwhile(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emb = FakeEmbedder()
    ix = index.Index(scratch / "m.db", emb, min_similarity=0.3)
    ix.put("keep", "memory", "Our campsite is number 37", "Our campsite is number 37", ts=1)
    ix.put("gone", "memory", "The van is blue", "The van is blue", ts=2)
    ix.put("moved", "memory", "Sam drives the truck", "Sam drives the truck", ts=3)
    ix.embed_pending(10)
    real = index.Index._read_matrix
    free: list[bool] = []

    def reading(self: index.Index, model: str) -> index.Matrix:
        m = real(self, model)

        def other() -> None:  # another thread can take the lock while the rows are read
            got = self._lock.acquire(timeout=1)
            free.append(got)
            if got:
                self._lock.release()

        t = threading.Thread(target=other)
        t.start()
        t.join()
        # Changes made while the rows were being read:
        self.delete("gone")
        self.put("moved", "turn", "Sam drives the truck", "Sam drives the truck", ts=3)
        self.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=4)
        self._embed([self.get("m1")])  # type: ignore[list-item]
        return m

    monkeypatch.setattr(index.Index, "_read_matrix", reading)
    m = ix.load_matrix()
    assert free == [True]
    assert set(m.by_id) == {"keep", "moved", "m1"}
    assert m.codes and m.kind[m.by_id["moved"][0]] == m.codes["turn"]
    assert ix.search("who is my boss", kinds=["memory"])[0].id == "m1"


def test_a_second_caller_waits_for_the_load_under_way(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ix = index.Index(scratch / "m.db", FakeEmbedder())
    ix.put("a", "note", "Alpha", "first", ts=1)
    ix.embed_pending(10)
    real = index.Index._read_matrix
    reads: list[int] = []

    def slow(self: index.Index, model: str) -> index.Matrix:
        reads.append(1)
        time.sleep(0.2)
        return real(self, model)

    monkeypatch.setattr(index.Index, "_read_matrix", slow)
    out: list[index.Matrix] = []
    ts = [threading.Thread(target=lambda: out.append(ix.load_matrix())) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(reads) == 1 and len(out) == 3 and out[0] is out[1] is out[2]


def test_the_service_loads_the_matrix_at_start(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", FakeEmbedder())
    ix.put("a", "note", "Alpha", "first", ts=1)
    ix.embed_pending(10)
    ix._matrix = None
    asyncio.run(memory_service._load_matrix(ix))
    assert ix._matrix is not None and set(ix._matrix.by_id) == {"a"}


# --- settings ----------------------------------------------------------------------------


def test_sqlite_settings(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", None)
    with ix._lock:
        assert ix._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert ix._conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
    ix.optimize()


def test_the_model_unloads_when_idle_by_default() -> None:
    assert IndexConfig().unload_after_s == 900


def test_the_embedder_works_in_batches_of_four(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed

    sizes: list[int] = []

    class Fake:
        def __init__(self, name: str, **kw: object) -> None:
            pass

        def passage_embed(self, texts: list[str], batch_size: int = 256) -> list[np.ndarray]:
            sizes.append(batch_size)
            return [np.ones(4, np.float32) for _ in texts]

        query_embed = passage_embed

    monkeypatch.setattr(fastembed, "TextEmbedding", Fake)
    e = index.LocalEmbedder("BAAI/bge-small-en-v1.5")
    e.embed(["a", "b"])
    e.embed(["q"], query=True)
    assert sizes == [4, 4]


# --- the login item ----------------------------------------------------------------------


def _plist(scratch: Path, *args: str) -> dict[str, object]:
    """scripts/launchd.sh install, with a stand-in launchctl and HOME in the scratch dir."""
    bin_dir = scratch / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "launchctl"
    fake.write_text('#!/bin/sh\n[ "$1" = print ] && exit 1\nexit 0\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(scratch),
        "SCOUT_HOME": str(scratch / "data"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    script = [shutil.which("sh") or "/bin/sh", str(ROOT / "scripts" / "launchd.sh"), "install", *args]
    subprocess.run(script, env=env, check=True, capture_output=True, timeout=30)  # noqa: S603  our own script
    label = "com.local.scout" + (f".{args[0]}" if args else "")
    with (scratch / "Library" / "LaunchAgents" / f"{label}.plist").open("rb") as f:
        out: dict[str, object] = plistlib.load(f)
    return out


def test_the_memory_service_is_not_a_background_process(scratch: Path) -> None:
    assert _plist(scratch, "memory")["ProcessType"] == "Standard"
    assert _plist(scratch)["ProcessType"] == "Interactive"
    assert _plist(scratch, "phone")["ProcessType"] == "Background"


def test_the_old_store_query_is_gone(scratch: Path) -> None:
    """recent() no longer uses the json_each pattern that kept SQLite off the indexes."""
    sql, _ = index._recent_sql(["mail"], None, 5)
    assert "json_each" not in sql and "IS NULL" not in sql
    db = scratch / "m.db"
    index.Index(db, None).close()
    names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert {"pending", "pending_ts", "pending_kind_ts", "docs_ts", "settings"} <= names
