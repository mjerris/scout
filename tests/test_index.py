"""The local store and search: full text, lazy vectors cached by content, meaning
re-ranking with a floor, sync, and the background pass. A tiny fake embedder (word
hashing) stands in for the model; the real one is measured in scripts, not here."""

import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from scout import index

SYNONYMS = {"boss": "manager", "site": "campsite"}


class FakeEmbedder:
    model = "fake@512"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        self.calls.append(len(texts))
        out = np.zeros((len(texts), 512), np.float32)
        for i, t in enumerate(texts):
            for w in index.fts_query(t).replace('"', "").split(" OR "):
                if w:
                    w = SYNONYMS.get(w, w)
                    out[i, int(hashlib.blake2b(w.encode(), digest_size=8).hexdigest(), 16) % 512] += 1.0
        out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
        return out


def make(tmp_path: Path, embedder: FakeEmbedder | None = None) -> index.Index:
    ix = index.Index(tmp_path / "scout.db", embedder, min_similarity=0.3)
    ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=100)
    ix.put("m2", "memory", "Our campsite is number 37", "Our campsite is number 37", ts=200)
    ix.put("t1", "turn", "is starlink down", "is starlink down\nScout: Starlink is online", ts=300)
    return ix


def test_full_text_search_by_kind_and_date(tmp_path: Path) -> None:
    ix = make(tmp_path)
    assert [h.id for h in ix.search("manager", semantic=False)] == ["m1"]
    assert [h.id for h in ix.search("starlink", kinds=["memory"], semantic=False)] == []
    assert [h.id for h in ix.search("campsite", since=150, semantic=False)] == ["m2"]
    assert ix.search("what's the", semantic=False) == []  # only filler words


def test_meaning_finds_what_shares_no_words(tmp_path: Path) -> None:
    ix = make(tmp_path, FakeEmbedder())
    assert ix.search("who is my boss")[0].id == "m1"
    assert ix.search("what site are we in")[0].id == "m2"
    assert ix.search("how tall is the eiffel tower") == []  # nothing related: no match at all


def test_vectors_are_cached_and_redone_only_when_the_item_changes(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    ix = make(tmp_path, emb)
    ix.search("boss")
    assert emb.calls == [3, 1]  # the three items once, then the query
    ix.search("campsite")
    assert emb.calls == [3, 1, 1]  # cached: only the query
    assert ix.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=100) is False  # unchanged
    ix.put("m1", "memory", "Pat is my old manager", "Pat is my old manager", ts=100)
    ix.search("boss")
    assert emb.calls == [3, 1, 1, 1, 1]  # just the changed item, then the query


def test_sync_replaces_a_kind_and_drops_what_is_gone(tmp_path: Path) -> None:
    ix = make(tmp_path)
    changed, removed = ix.sync(
        "memory",
        [{"id": "m2", "title": "Our campsite is number 37", "body": "Our campsite is number 37", "ts": 200}],
    )
    assert (changed, removed) == (0, 1)
    assert ix.count("memory") == 1 and ix.get("m1") is None and ix.count("turn") == 1


def test_background_pass_embeds_new_items_ahead_of_time(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    ix = make(tmp_path, emb)
    assert ix.embed_pending(2) == 2 and ix.embed_pending(10) == 1 and ix.embed_pending(10) == 0
    ix.search("boss")
    assert emb.calls == [2, 1, 1]  # the search needed nothing new but its query


def test_hits_format_for_claude(tmp_path: Path) -> None:
    ix = make(tmp_path)
    out = index.format_hits(ix.search("starlink", semantic=False))
    assert out.startswith("[turn] ") and out.endswith(
        ": is starlink down: is starlink down Scout: Starlink is online"
    )
    assert index.format_hits([]) == "No matches in Scout's local index."


@pytest.mark.parametrize(("text", "q"), [("What's Pat's number?", '"pat" OR "number"'), ("the and of", "")])
def test_fts_query(text: str, q: str) -> None:
    assert index.fts_query(text) == q


def test_a_search_embeds_only_a_few_new_items_itself(tmp_path: Path) -> None:
    """The background pass embeds the backlog; a search adds at most a few, and text
    matches still waiting for a vector are kept by their text rank."""
    emb = FakeEmbedder()
    ix = make(tmp_path, emb)
    ix.max_new_per_search = 1
    hits = ix.search("starlink manager campsite")
    assert emb.calls == [1, 1]  # one item embedded, then the query
    assert {h.id for h in hits} == {"m1", "m2", "t1"}
    assert sum(h.how == "text" for h in hits) == 2  # the two still waiting rank by text


def test_meaning_reaches_every_item_not_just_the_newest(tmp_path: Path) -> None:
    """Regression: meaning only ranked the text hits and the 200 newest items, so an
    older item that shares no words with the question was never found."""
    emb = FakeEmbedder()
    ix = index.Index(tmp_path / "scout.db", emb, min_similarity=0.3)
    ix.put("old", "memory", "Pat is my manager", "Pat is my manager", ts=1)
    for i in range(300):
        ix.put(f"n{i}", "turn", f"timer {i}", f"set a timer for {i} minutes", ts=1000 + i)
    while ix.embed_pending(128):
        pass
    assert ix.search("who is my boss")[0].id == "old"


def test_the_matrix_follows_puts_and_deletes(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    ix = make(tmp_path, emb)
    assert ix.search("who is my boss")[0].id == "m1"  # loads the matrix
    ix.put("m1", "memory", "Our van is blue", "Our van is blue", ts=100)  # new text: old vector gone
    assert all(h.id != "m1" for h in ix.search("who is my boss"))
    ix.put("m3", "memory", "Sam is my manager", "Sam is my manager", ts=400)
    assert ix.search("who is my boss")[0].id == "m3"
    ix.delete("m3")
    assert all(h.id != "m3" for h in ix.search("who is my boss"))
    ix.put(
        "m2", "turn", "Our campsite is number 37", "Our campsite is number 37", ts=200
    )  # same text, new kind
    assert ix.search("what site are we in", kinds=["memory"]) == []
    assert ix.search("what site are we in", kinds=["turn"])[0].id == "m2"


def test_kind_and_date_filters_apply_to_meaning(tmp_path: Path) -> None:
    ix = make(tmp_path, FakeEmbedder())
    assert ix.search("who is my boss", kinds=["turn"]) == []
    assert ix.search("who is my boss", since=150) == []
    assert ix.search("who is my boss", since=50)[0].id == "m1"


def test_matrix_top_filters_and_orders() -> None:
    m = index.Matrix(2)
    m.set("a", [np.array([1.0, 0.0], np.float32)], "mail", 10)
    m.set("b", [np.array([0.6, 0.8], np.float32)], "turn", 20)
    m.set("c", [np.array([0.0, 1.0], np.float32)], "mail", 30)
    q = np.array([1.0, 0.0], np.float32)
    assert [i for i, _ in m.top(q, 3)] == ["a", "b", "c"]
    assert [i for i, _ in m.top(q, 1)] == ["a"]
    assert [i for i, _ in m.top(q, 3, kinds=["mail"])] == ["a", "c"]
    assert [i for i, _ in m.top(q, 3, since=15)] == ["b", "c"]
    m.drop("a")
    assert [i for i, _ in m.top(q, 3)] == ["b", "c"] and len(m) == 2


def test_matrix_compacts_after_many_drops() -> None:
    m = index.Matrix(2)
    for i in range(2000):
        m.set(f"x{i}", [np.array([1.0, float(i)], np.float32)], "turn", i)
    for i in range(1500):
        m.drop(f"x{i}")
    assert len(m) == 500 and len(m.ids) < 2000  # rows reclaimed
    assert {i for i, _ in m.top(np.array([0.0, 1.0], np.float32), 3)} == {"x1999", "x1998", "x1997"}


def test_the_background_pass_works_through_a_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(index, "BACKLOG_PAUSE_S", 0.01)
    emb = FakeEmbedder()
    ix = index.Index(tmp_path / "scout.db", emb, min_similarity=0.3)
    for i in range(50):
        ix.put(f"n{i}", "turn", f"note {i}", f"note number {i}", ts=i)

    async def run() -> None:
        task = asyncio.create_task(ix.background(lambda: True, every_s=30, batch=8))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if sum(emb.calls[1:]) >= 50:  # calls[0] is the warm-up
                break
        task.cancel()

    asyncio.run(run())
    assert emb.calls[0] == 1 and sum(emb.calls[1:]) == 50
    assert all(n == 8 for n in emb.calls[1:-1])  # in batches, one after another, not one per 30 s


def test_the_model_loads_from_disk_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed

    made: list[dict[str, object]] = []

    class Fake:
        def __init__(self, name: str, **kw: object) -> None:
            made.append(kw)
            if kw.get("local_files_only") and len(made) == 1 and not have[0]:
                raise ValueError("not cached")

        def passage_embed(self, texts: list[str], batch_size: int = 16) -> list[np.ndarray]:
            return [np.ones(4, np.float32) for _ in texts]

    have = [True]
    monkeypatch.setattr(fastembed, "TextEmbedding", Fake)
    e = index.LocalEmbedder("BAAI/bge-small-en-v1.5")
    e.embed(["hello"])
    assert [m.get("local_files_only") for m in made] == [True]  # cached: no network at all
    made.clear()
    have[0] = False
    e2 = index.LocalEmbedder("BAAI/bge-small-en-v1.5")
    out = e2.embed(["hello"])
    assert [m.get("local_files_only") for m in made] == [True, None]  # missing: then download
    assert out.shape == (1, 4) and abs(float(np.linalg.norm(out[0])) - 1) < 1e-5


def test_a_long_item_is_found_by_any_window(tmp_path: Path) -> None:
    """Regression: only an item's first 1,000 characters were embedded, so a long mail
    whose answer is near the end couldn't be found by meaning."""
    emb = FakeEmbedder()
    ix = index.Index(tmp_path / "scout.db", emb, min_similarity=0.3)
    filler = "the quarterly figures look fine. " * 120  # ~4,000 characters
    ix.put("long", "mail", "Weekly update", filler + "Also, Pat is my manager now.", ts=1)
    ix.put("other", "mail", "Groceries", "milk eggs bread", ts=2)
    while ix.embed_pending(16):
        pass
    assert ix.search("who is my boss")[0].id == "long"
    with ix._lock:
        parts = ix._conn.execute("SELECT count(*) FROM vecs WHERE id = 'long'").fetchone()[0]
    assert parts == len(index._doc_texts(ix.get("long"))) > 4  # type: ignore[arg-type]


def test_windows_cover_the_text_with_overlap() -> None:
    h = index.Hit("x", "mail", "Title", "".join(f"{i:04d}" for i in range(1000)), "Pat", 0, {})  # 4,000 chars
    texts = index._doc_texts(h)
    assert texts[0].startswith("Title Pat 0000") and len(texts) == 5
    assert all(t.startswith("Title ") for t in texts[1:])
    assert texts[-1].endswith("0999")  # the end is covered
    short = index.Hit("y", "turn", "Hi", "short", "", 0, {})
    assert index._doc_texts(short) == ["Hi short"]
    huge = index.Hit("z", "mail", "T", "x" * 100_000, "", 0, {})
    assert len(index._doc_texts(huge)) == index.MAX_WINDOWS


def test_old_single_vectors_become_part_zero_and_long_items_are_redone(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE vecs (id TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL, vec BLOB NOT NULL, "
        "PRIMARY KEY (id, model));"
    )
    conn.execute("INSERT INTO vecs VALUES ('a', 'fake@512', 's', ?)", (np.zeros(512, np.float32).tobytes(),))
    conn.commit()
    conn.close()
    ix = index.Index(db, FakeEmbedder())
    with ix._lock:
        assert ix._conn.execute("SELECT id, part FROM vecs").fetchall() == [("a", 0)]
    ix.put("long", "mail", "T", "word " * 400, ts=1)  # 2,000 characters: needs more than one window
    assert ix.embed_pending(10) == 1
    with ix._lock:
        assert ix._conn.execute("SELECT count(*) FROM vecs WHERE id = 'long'").fetchone()[0] >= 2
    assert ix.embed_pending(10) == 0  # every window done: nothing left


def test_matrix_counts_an_item_once_by_its_best_window() -> None:
    m = index.Matrix(2)
    m.set("a", [np.array([0.0, 1.0], np.float32), np.array([1.0, 0.0], np.float32)], "mail", 1)
    m.set("b", [np.array([0.8, 0.6], np.float32)], "mail", 2)
    q = np.array([1.0, 0.0], np.float32)
    top = m.top(q, 2)
    assert [i for i, _ in top] == ["a", "b"] and top[0][1] == 1.0 and abs(top[1][1] - 0.8) < 1e-6
    assert m.best(["a", "b", "zz"], q) == {"a": 1.0, "b": pytest.approx(0.8)}
    m.set("a", [np.array([0.0, 1.0], np.float32)], "mail", 1)  # replaced: one window now
    assert [i for i, _ in m.top(q, 1)] == ["b"] and len(m) == 2
