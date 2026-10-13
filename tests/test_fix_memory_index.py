"""Review fixes in the store: put() notices meta/ts/kind changes without re-embedding,
vectors are kept only for the text that was embedded, accented words are searchable,
remembered facts change one at a time, and an interrupted move from the old store
finishes on the next start."""

import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest

from scout import index, memory, memory_service
from scout.memory import Memory

from test_index import FakeEmbedder

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp" / "fix-memory-tests"


@pytest.fixture
def scratch() -> Iterator[Path]:
    d = SCRATCH / uuid.uuid4().hex[:12]
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


# --- #34: put() change detection ------------------------------------------------------


def test_a_meta_only_change_is_stored(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", None)
    ix.put("mail:1", "mail", "Hi", "", "pat", ts=10, meta={"read": False, "partial": True})
    assert ix.put("mail:1", "mail", "Hi", "", "pat", ts=10, meta={"read": True}) is True
    hit = ix.get("mail:1")
    assert hit is not None and hit.meta == {"read": True}  # partial cleared, read set
    assert ix.put("mail:1", "mail", "Hi", "", "pat", ts=10, meta={"read": True}) is False


def test_ts_and_kind_changes_are_stored(scratch: Path) -> None:
    ix = index.Index(scratch / "m.db", None)
    ix.put("x", "note", "T", "B", ts=10)
    assert ix.put("x", "note", "T", "B", ts=20) is True
    assert ix.put("x", "list", "T", "B", ts=20) is True
    hit = ix.get("x")
    assert hit is not None and (hit.kind, hit.ts) == ("list", 20.0)
    assert ix.put("x", "list", "T", "B") is False  # no new time given: unchanged


def test_a_meta_only_change_keeps_the_vector(scratch: Path) -> None:
    emb = FakeEmbedder()
    ix = index.Index(scratch / "m.db", emb, min_similarity=0.3)
    ix.put("m1", "mail", "Pat is my manager", "Pat is my manager", ts=100, meta={"read": False})
    ix.search("boss")
    assert emb.calls == [1, 1]  # the item, then the query
    assert ix.put("m1", "mail", "Pat is my manager", "Pat is my manager", ts=100, meta={"read": True})
    assert ix.embed_pending() == 0
    ix.search("boss")
    assert emb.calls == [1, 1, 1]  # only the query: the vector still fits the text


# --- #37: a vector belongs to the text that was embedded --------------------------------


class RewritingEmbedder(FakeEmbedder):
    """Rewrites the item (a partial mail filled in) while its old text is being embedded."""

    def __init__(self) -> None:
        super().__init__()
        self.ix: index.Index | None = None

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        if self.ix is not None and not query:
            ix, self.ix = self.ix, None
            ix.put("mail:1", "mail", "Oil change", "The full body: oil changed in Cadillac", ts=5)
        return super().embed(texts, query)


def test_a_vector_is_not_kept_for_text_that_changed_while_embedding(scratch: Path) -> None:
    emb = RewritingEmbedder()
    ix = index.Index(scratch / "m.db", emb)
    ix.put("mail:1", "mail", "Oil change", "", ts=5)  # subject only
    emb.ix = ix
    assert ix.embed_pending() == 1  # embedded the subject-only text; the body arrived meanwhile
    assert ix.embed_pending() == 1  # so the full mail still needs its own vector
    assert ix.embed_pending() == 0


# --- #35: accented words ----------------------------------------------------------------


def test_fts_query_keeps_accented_words() -> None:
    assert index.fts_query("email from José Müller café") == '"email" OR "josé" OR "müller" OR "café"'


@pytest.mark.parametrize("query", ["José Müller", "jose muller", "café"])
def test_accented_names_are_found_by_text(scratch: Path, query: str) -> None:
    ix = index.Index(scratch / "m.db", None)
    ix.put("n1", "note", "Lunch with José Müller", "at the café", ts=1)
    ix.put("n2", "note", "Something else", "", ts=2)
    assert [h.id for h in ix.search(query, semantic=False)] == ["n1"]


# --- #36: remembered facts change one at a time ----------------------------------------


def test_a_forget_during_an_add_stays_forgotten(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mem = Memory(scratch / "memory.json")
    mem.add("my dentist is Dr. Lee")
    adding = threading.Event()
    real = memory.keywords

    def slow(text: str) -> set[str]:
        # The add thread, comparing against the stored fact: let a forget run meanwhile.
        if threading.current_thread().name == "add" and text == "my dentist is Dr. Lee":
            adding.set()
            time.sleep(0.3)
        return real(text)

    monkeypatch.setattr(memory, "keywords", slow)
    a = threading.Thread(target=mem.add, args=("Sam likes tea",), name="add")
    a.start()
    assert adding.wait(5)
    forgotten = mem.forget("dentist")
    a.join(5)
    assert forgotten == ["my dentist is Dr. Lee"]
    assert [f["text"] for f in mem.facts] == ["Sam likes tea"]
    assert [f["text"] for f in Memory(scratch / "memory.json").facts] == ["Sam likes tea"]
    assert not list(scratch.glob("*.tmp"))


# --- #40: an interrupted move from the old store resumes ---------------------------------


def test_an_interrupted_move_finishes_next_start(scratch: Path) -> None:
    old = scratch / "scout.db"
    legacy = index.Index(old, None)
    for i in range(3):
        legacy.put(f"turn:{i}", "turn", f"question {i}", f"answer {i}", ts=i)
    legacy.close()
    new = index.Index(scratch / "memory.db", None)
    new.put("turn:0", "turn", "question 0 (newer)", "answer 0", ts=0)  # copied before the kill
    assert memory_service.migrate(new, old) == 2
    assert new.count() == 3
    first = new.get("turn:0")
    assert first is not None and first.title == "question 0 (newer)"  # not overwritten
    tables = {r[0] for r in sqlite3.connect(old).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "docs" not in tables
    assert memory_service.migrate(new, old) == 0
