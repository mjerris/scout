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
    m.add("a", np.array([1.0, 0.0], np.float32), "mail", 10)
    m.add("b", np.array([0.6, 0.8], np.float32), "turn", 20)
    m.add("c", np.array([0.0, 1.0], np.float32), "mail", 30)
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
        m.add(f"x{i}", np.array([1.0, float(i)], np.float32), "turn", i)
    for i in range(1500):
        m.drop(f"x{i}")
    assert len(m) == 500 and len(m.ids) < 2000  # rows reclaimed
    assert {i for i, _ in m.top(np.array([0.0, 1.0], np.float32), 3)} == {"x1999", "x1998", "x1997"}
