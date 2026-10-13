"""Scout's local store and search index: one SQLite file on this Mac.

Everything Scout can read (memories, its own conversations, notes and lists, and
later mail and calendar) is stored here and searchable locally; only the few best
matches ever go to Claude.

A search ranks by meaning over every item that has a vector (the kinds and dates
asked for), and full text (SQLite FTS5, BM25) adds a small bonus for shared words and
finds what has no vector yet. Meaning decides because, measured on a synthetic year of
20,000 conversations with 50 questions, comparing against every item found the answer
in the top 5 for 78-82% of them; full text alone, or meaning over just the text hits
and the newest items, found 34%.

The vectors live in SQLite (table vecs) and, for searching, in one in-memory matrix
(float32; 384 dimensions is ~1.5 KB an item, 150 MB per 100,000). An idle background
pass embeds new items, and a search embeds the few text hits still without one, so
nothing is ever unreachable by full text, and nothing leaves the Mac.

The embedder is bge-small-en-v1.5 (ONNX on CPU through fastembed, ~0.2 GB in memory,
loads in ~4 s); it unloads after a while idle. The model name and dimension are stored
with each vector, so changing either re-embeds in the background.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from . import journal, notefiles, taskfiles
from .config import DATA

log = logging.getLogger(__name__)

MODELS = DATA / "models" / "fastembed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    sig TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS docs_kind_ts ON docs(kind, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, body, who, content='docs', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, body, who) VALUES (new.rowid, new.title, new.body, new.who);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, who) VALUES ('delete', old.rowid, old.title, old.body, old.who);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, who) VALUES ('delete', old.rowid, old.title, old.body, old.who);
    INSERT INTO docs_fts(rowid, title, body, who) VALUES (new.rowid, new.title, new.body, new.who);
END;
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    sig TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vecs (
    id TEXT NOT NULL,
    model TEXT NOT NULL,
    sig TEXT NOT NULL,
    vec BLOB NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, model, part)
);
"""


@dataclass
class Hit:
    id: str
    kind: str
    title: str
    body: str
    who: str
    ts: float
    meta: dict[str, Any]
    score: float = 0.0
    how: str = ""  # "text", "meaning" or "text+meaning"
    similarity: float | None = None  # cosine to the query, when ranked by meaning

    def snippet(self, n: int = 200) -> str:
        s = " ".join(self.body.split())
        return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray: ...


DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class LocalEmbedder:
    """A fastembed model (ONNX, CPU). nomic models get their task prefixes and a
    Matryoshka cut to `dims`; others use fastembed's query and passage embedding at full
    size. Every vector is unit length. Loads on first use; `unload_if_idle` frees it
    after `idle_s`."""

    def __init__(
        self,
        name: str = DEFAULT_MODEL,
        dims: int = 0,  # 0: the model's own size
        idle_s: float = 900,
        threads: int = 2,
    ) -> None:
        self.name = name
        self.threads = threads
        self.dims = dims
        self.idle_s = idle_s
        self.model = f"{name}@{dims}" if dims else name
        self._nomic = name.startswith("nomic-ai/")
        self._m: Any = None
        self._used = 0.0
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        with self._lock:
            if self._m is None:
                from fastembed import TextEmbedding

                t0 = time.perf_counter()
                MODELS.mkdir(parents=True, exist_ok=True)
                # Few threads: embedding never takes the cores listening and speaking need.
                # The copy on disk first, without asking Hugging Face; download only if absent.
                try:
                    self._m = TextEmbedding(
                        self.name, cache_dir=str(MODELS), threads=self.threads, local_files_only=True
                    )
                except Exception as exc:  # any failure to load locally: download
                    log.info("embedder %s not on disk (%s); downloading", self.name, type(exc).__name__)
                    self._m = TextEmbedding(self.name, cache_dir=str(MODELS), threads=self.threads)
                log.info("embedder %s loaded in %.1fs", self.name, time.perf_counter() - t0)
            self._used = time.monotonic()
            if self._nomic:
                prefix = "search_query: " if query else "search_document: "
                raw = np.array(
                    list(self._m.embed([prefix + t for t in texts], batch_size=16)), dtype=np.float32
                )
            else:
                f = self._m.query_embed if query else self._m.passage_embed
                raw = np.array(list(f(list(texts), batch_size=16)), dtype=np.float32)
        if self._nomic:
            # Matryoshka: layer-norm, then cut (nomic's recipe).
            raw = (raw - raw.mean(axis=1, keepdims=True)) / (raw.std(axis=1, keepdims=True) + 1e-6)
        if self.dims:
            raw = raw[:, : self.dims]
        out: np.ndarray = raw / (np.linalg.norm(raw, axis=1, keepdims=True) + 1e-9)
        return out

    @property
    def loaded(self) -> bool:
        return self._m is not None

    def unload_if_idle(self) -> bool:
        with self._lock:
            if self.idle_s > 0 and self._m is not None and time.monotonic() - self._used > self.idle_s:
                self._m = None
                log.info("embedder unloaded after %.0f s idle", self.idle_s)
                return True
        return False


class Matrix:
    """Every current vector of one model, in memory, for ranking by meaning: one row per
    vector (an item has one per window of its text) with the item's kind and time, so a
    search filters and scores with numpy, and an item counts by its best window. Rows
    are added and dropped as items change; dropped rows are reclaimed by compaction."""

    def __init__(self, dims: int) -> None:
        self.dims = dims
        self.ids: list[str] = []  # the item of each row
        self.by_id: dict[str, list[int]] = {}
        self.vecs = np.zeros((0, dims), np.float32)
        self.kinds: list[str] = []
        self.ts = np.zeros(0, np.float64)
        self.alive = np.zeros(0, bool)

    def __len__(self) -> int:
        return len(self.by_id)

    def _grow(self, n: int) -> None:
        if n <= len(self.alive):
            return
        cap = max(n, 2 * len(self.alive), 256)
        for name, fill in (
            ("vecs", np.zeros((cap, self.dims), np.float32)),
            ("ts", np.zeros(cap)),
            ("alive", np.zeros(cap, bool)),
        ):
            old = getattr(self, name)
            fill[: len(old)] = old
            setattr(self, name, fill)

    def set(self, id: str, vecs: Sequence[np.ndarray], kind: str, ts: float) -> None:
        """An item's vectors (all its windows), replacing any it had."""
        self.drop(id)
        rows = []
        for v in vecs:
            r = len(self.ids)
            self._grow(r + 1)
            self.ids.append(id)
            self.kinds.append(kind)
            self.vecs[r], self.ts[r], self.alive[r] = v, ts, True
            rows.append(r)
        if rows:
            self.by_id[id] = rows

    def drop(self, id: str) -> None:
        for r in self.by_id.pop(id, []):
            self.alive[r] = False
        if len(self.ids) > 1024 and int(self.alive[: len(self.ids)].sum()) < len(self.ids) // 2:
            self._compact()

    def touch(self, id: str, kind: str, ts: float) -> None:
        for r in self.by_id.get(id, []):
            self.kinds[r], self.ts[r] = kind, ts

    def _compact(self) -> None:
        keep = [r for r in range(len(self.ids)) if self.alive[r]]
        self.ids = [self.ids[r] for r in keep]
        self.kinds = [self.kinds[r] for r in keep]
        self.vecs, self.ts = self.vecs[keep].copy(), self.ts[keep].copy()
        self.alive = np.ones(len(keep), bool)
        self.by_id = {}
        for r, id in enumerate(self.ids):
            self.by_id.setdefault(id, []).append(r)

    def top(
        self, q: np.ndarray, k: int, kinds: Sequence[str] | None = None, since: float | None = None
    ) -> list[tuple[str, float]]:
        """The k items most similar to q (id, cosine of their best window), best first."""
        n = len(self.ids)
        if not n or k <= 0:
            return []
        mask = self.alive[:n].copy()
        if kinds:
            want = set(kinds)
            mask &= np.fromiter((kd in want for kd in self.kinds), bool, n)
        if since is not None:
            mask &= self.ts[:n] >= since
        rows = np.flatnonzero(mask)
        if not len(rows):
            return []
        sims = self.vecs[rows] @ q
        # Enough rows to find k distinct items (an item's windows can crowd the top).
        shortlist = min(len(rows), k * 8)
        while True:
            if shortlist < len(rows):
                part = np.argpartition(-sims, shortlist - 1)[:shortlist]
            else:
                part = np.arange(len(rows))
            out: dict[str, float] = {}
            for i in part[np.argsort(-sims[part], kind="stable")]:
                id = self.ids[rows[i]]
                if id not in out:
                    out[id] = float(sims[i])
                    if len(out) == k:
                        return list(out.items())
            if shortlist >= len(rows):
                return list(out.items())
            shortlist = min(len(rows), shortlist * 4)

    def best(self, ids: Sequence[str], q: np.ndarray) -> dict[str, float]:
        """The similarity of each of these items (that has vectors): its best window."""
        out = {}
        for id in ids:
            rows = [r for r in self.by_id.get(id, []) if self.alive[r]]
            if rows:
                out[id] = float((self.vecs[rows] @ q).max())
        return out


_WORD = re.compile(r"[^\W_][\w']*")  # any script: "José", "Müller", "café" (FTS folds the accents)
_STOP = frozenset({"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "about", "from", "by", "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "what", "whats", "who", "where", "when", "how", "which", "that", "this", "it", "its", "my", "me", "i", "you", "your", "we", "our", "they", "their", "there", "have", "has", "had", "not", "no", "so", "just", "can", "could", "would", "will", "any", "some"})  # fmt: skip


BACKLOG_PAUSE_S = 0.5  # between embedding batches while there's a backlog
TEXT_BONUS = 0.1  # added to the cosine of the best full-text match (less further down)


def fts_query(text: str) -> str:
    """User words as an FTS5 OR-query of quoted terms ("budget" OR "pat"), or ''."""
    words = [
        re.sub(r"'s$", "", w.lower()).replace("'", "") for w in _WORD.findall(text.replace("\u2019", "'"))
    ]
    terms = [w for w in dict.fromkeys(words) if w and w not in _STOP][:16]
    return " OR ".join(f'"{t}"' for t in terms)


def _sig(*parts: str) -> str:
    return hashlib.blake2b("\x1f".join(parts).encode(), digest_size=8).hexdigest()


@dataclass
class Index:
    path: Path
    embedder: Embedder | None = None
    min_similarity: float = 0.6  # a match by meaning alone (no shared words) needs this cosine
    max_new_per_search: int = 32  # text hits a search may embed itself; the background does the rest
    _conn: sqlite3.Connection = field(init=False, repr=False)
    journal_root: Path | None = None  # where the markdown journal lives (journal.py)
    keep_private: Callable[[], bool] = lambda: False  # keep answers from mail in the journal
    _matrix: Matrix | None = field(init=False, repr=False, default=None)  # loaded on first use
    _lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _pool: ThreadPoolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._add_vector_parts()
        self._conn.executescript(_SCHEMA)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")

    def _add_vector_parts(self) -> None:
        """Once: vecs from before an item could have several vectors (one per window of
        its text) become part 0."""
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(vecs)")]
        if cols and "part" not in cols:
            self._conn.executescript(
                "BEGIN; ALTER TABLE vecs RENAME TO vecs_old; "
                "CREATE TABLE vecs (id TEXT NOT NULL, model TEXT NOT NULL, sig TEXT NOT NULL, vec BLOB NOT NULL, "
                "part INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (id, model, part)); "
                "INSERT INTO vecs(id, model, sig, vec, part) SELECT id, model, sig, vec, 0 FROM vecs_old; "
                "DROP TABLE vecs_old; COMMIT;"
            )

    # --- writing --------------------------------------------------------------------

    def put(
        self,
        id: str,
        kind: str,
        title: str = "",
        body: str = "",
        who: str = "",
        ts: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        """Add or update one item. Returns False when it was already stored unchanged.
        `sig` covers only the embedded text, so a change to meta, ts or kind updates the
        row but keeps its vector."""
        sig = _sig(title, body, who)
        meta = meta or {}
        when = ts
        with self._lock:
            row = self._conn.execute("SELECT kind, ts, meta, sig FROM docs WHERE id = ?", (id,)).fetchone()
            if row and (row[0], row[3]) == (kind, sig) and ts in (None, row[1]) and _meta(row[2]) == meta:
                return False
            if when is None:
                when = row[1] if row else time.time()
            if self._matrix is not None:
                if row and row[3] == sig:
                    self._matrix.touch(id, kind, when)  # same text: the vector still holds
                else:
                    self._matrix.drop(id)
            self._conn.execute(
                "INSERT INTO docs(id, kind, title, body, who, ts, meta, sig) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, title=excluded.title, body=excluded.body, "
                "who=excluded.who, ts=excluded.ts, meta=excluded.meta, sig=excluded.sig",
                (
                    id,
                    kind,
                    title,
                    body,
                    who,
                    when,
                    json.dumps(meta),
                    sig,
                ),
            )
        return True

    def delete(self, id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE id = ?", (id,))
            self._conn.execute("DELETE FROM vecs WHERE id = ?", (id,))
            if self._matrix is not None:
                self._matrix.drop(id)

    def sync(self, kind: str, items: Iterable[dict[str, Any]]) -> tuple[int, int]:
        """Make `kind` hold exactly `items` (each a put() dict). Returns (changed, removed)."""
        keep, changed = set(), 0
        for it in items:
            keep.add(it["id"])
            changed += self.put(kind=kind, **{k: v for k, v in it.items() if k != "kind"})
        with self._lock:
            gone = [
                r[0]
                for r in self._conn.execute("SELECT id FROM docs WHERE kind = ?", (kind,))
                if r[0] not in keep
            ]
        for id in gone:
            self.delete(id)
        return changed, len(gone)

    def get(self, id: str) -> Hit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, kind, title, body, who, ts, meta FROM docs WHERE id = ?", (id,)
            ).fetchone()
        return _hit(row) if row else None

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FROM docs WHERE ? IS NULL OR kind = ?", (kind, kind)
            ).fetchone()
            return int(row[0])

    def recent(
        self, kinds: Sequence[str] | None = None, limit: int = 20, since: float | None = None
    ) -> list[Hit]:
        with self._lock:
            rows = self._conn.execute(
                _RECENT_SQL,
                (*_filter_args(kinds, since), limit),
            ).fetchall()
        return [_hit(r) for r in rows]

    def prune(self, kind: str, before: float) -> int:
        """Remove `kind` items older than `before`; returns how many."""
        with self._lock:
            old = [
                r[0]
                for r in self._conn.execute("SELECT id FROM docs WHERE kind = ? AND ts < ?", (kind, before))
            ]
        for id in old:
            self.delete(id)
        return len(old)

    # --- the markdown journal (journal.py): the files are the record, this is the index ---

    def journal_add(
        self,
        ts: float,
        who: str,
        q: str,
        a: str,
        tools: Sequence[tuple[str, dict[str, Any]]] = (),
        private: bool = False,
    ) -> str:
        """Append one exchange to the day's journal file and index it; returns the file."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        e = journal.Entry(ts, who, q, a, [(str(n), dict(args or {})) for n, args in tools], private)
        p = journal.append(self.journal_root, e, self.keep_private())
        self.sync_file(p)
        return str(p)

    def note_add(self, title: str, body: str, ts: float | None = None) -> int:
        """A new note file (notefiles.py), indexed; returns its number."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        p = notefiles.write(self.journal_root, title, body, time.time() if ts is None else ts)
        self.sync_file(p)
        return notefiles.number(p) or 0

    def note_delete(self, n: int) -> bool:
        """Delete note n's file and its index entry; False if there's no such note."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        p = notefiles.find(self.journal_root, n)
        if p is None:
            return False
        p.unlink()
        self.sync_file(p)
        return True

    def task_add(self, title: str, fields: dict[str, Any], context: str = "") -> int:
        """A new task file (taskfiles.py), indexed; returns its number."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        p = taskfiles.write(self.journal_root, title, fields, context)
        self.sync_file(p)
        return taskfiles.number(p) or 0

    def task_update(self, n: int, fields: dict[str, Any], add_context: str = "") -> bool:
        """Change task n's fields (None removes one) and add context; False if no such task."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        p = taskfiles.find(self.journal_root, n)
        if p is None:
            return False
        taskfiles.update(p, dict(fields), add_context)
        self.sync_file(p)
        return True

    def _file_kind(self, p: Path) -> tuple[str, str, bool] | None:
        """(kind, id prefix, whether ids match it exactly) for a file Scout keeps."""
        if self.journal_root is not None and p.parent == notefiles.notes_dir(self.journal_root):
            key = notefiles.id_prefix(p)
            return ("note", key, True) if key else None
        if self.journal_root is not None and p.parent == taskfiles.tasks_dir(self.journal_root):
            key = taskfiles.id_prefix(p)
            return ("task", key, True) if key else None
        key = journal.id_prefix(p)
        return ("turn", key, False) if key else None

    def sync_file(self, p: Path) -> int:
        """Make the index hold exactly the items of one file (a journal day or a note),
        or none if it's gone. Returns how many items changed."""
        what = self._file_kind(p)
        if what is None:
            return 0
        kind, prefix, exact = what
        parse = {"note": notefiles.parse, "task": taskfiles.parse}.get(kind, journal.parse)
        items = parse(p) if p.exists() else []
        changed = sum(self.put(kind=kind, **it) for it in items)
        keep = {it["id"] for it in items}
        with self._lock:
            sql = (
                "SELECT id FROM docs WHERE kind = ? AND id = ?"
                if exact
                else "SELECT id FROM docs WHERE kind = ? AND substr(id, 1, length(?)) = ?"
            )
            args = (kind, prefix) if exact else (kind, prefix, prefix)
            old = [r[0] for r in self._conn.execute(sql, args) if r[0] not in keep]
        for id in old:
            self.delete(id)
        with self._lock:
            if p.exists():
                self._conn.execute(
                    "INSERT OR REPLACE INTO files(path, sig) VALUES (?, ?)", (str(p), journal.file_sig(p))
                )
            else:
                self._conn.execute("DELETE FROM files WHERE path = ?", (str(p),))
        return changed + len(old)

    def sync_files(self) -> int:
        """Re-index journal files that changed, appeared or went away since last time
        (an edit, a deleted entry, a file restored from a backup). Returns items changed."""
        if self.journal_root is None:
            return 0
        with self._lock:
            known = dict(self._conn.execute("SELECT path, sig FROM files").fetchall())
        changed = 0
        root = self.journal_root
        files = journal.day_files(root) + notefiles.files(root) + taskfiles.files(root)
        for p in files:
            if known.get(str(p)) != journal.file_sig(p):
                changed += self.sync_file(p)
        for path in set(known) - {str(p) for p in files}:
            changed += self.sync_file(Path(path))
        return changed

    # --- searching ------------------------------------------------------------------

    def text_search(
        self, query: str, kinds: Sequence[str] | None = None, since: float | None = None, limit: int = 50
    ) -> list[Hit]:
        """Full-text matches, best first (BM25: titles count most, then people, then bodies)."""
        q = fts_query(query)
        if not q:
            return []
        with self._lock:
            rows = self._conn.execute(
                _TEXT_SQL,
                (q, *_filter_args(kinds, since), limit),
            ).fetchall()
        out = []
        for row in rows:
            h = _hit(row[:7])
            h.score, h.how = -float(row[7]), "text"
            out.append(h)
        return out

    def search(
        self,
        query: str,
        kinds: Sequence[str] | None = None,
        since: float | None = None,
        limit: int = 8,
        semantic: bool | None = True,
    ) -> list[Hit]:
        """Best matches for `query`. With an embedder: ranked by meaning over every item
        with a vector (of `kinds`, since `since`), plus a small bonus for full-text
        matches; text hits still without a vector keep their text rank. semantic=None:
        by meaning only if the model is already loaded (never wait for it to load)."""
        text = self.text_search(query, kinds, since, limit=60)
        if semantic is None:
            semantic = bool(getattr(self.embedder, "loaded", True))
        if not semantic or self.embedder is None:
            return text[:limit]
        # Items without a vector yet: embed a few now, text hits first, then the newest
        # (the background does the rest), so what was just added is found by meaning.
        unranked = self._ensure_vectors(text, self.max_new_per_search)
        if not unranked:  # the text hits didn't use up the budget
            self.embed_pending(self.max_new_per_search)
        q = self.embedder.embed([query], query=True)[0]
        waiting = {h.id for h in unranked}
        with self._lock:
            m = self._loaded_matrix()
            sims = dict(m.top(q, max(limit * 4, 60), kinds, since))
            # A text hit just outside the meaning shortlist still gets its own similarity.
            sims.update(m.best([h.id for h in text if h.id not in sims and h.id not in waiting], q))
        text_rank = {h.id: i for i, h in enumerate(text)}
        hits = {h.id: h for h in text}
        hits.update({h.id: h for h in self._get_many([id for id in sims if id not in hits])})
        # Meaning decides; sharing the query's words adds a little (more for the best text
        # matches). One bar for both, so a shared stray word ("work" in "working") can't
        # carry an unrelated item.
        ranked = []
        for id, sim in sims.items():
            if id not in hits:
                continue  # deleted since the matrix was read
            h = hits[id]
            t = text_rank.get(id)
            h.similarity = sim
            h.score = sim + (TEXT_BONUS / (1 + t) if t is not None else 0.0)
            if h.score < self.min_similarity:
                continue
            h.how = "text+meaning" if t is not None else "meaning"
            ranked.append(h)
        for t, h in enumerate(text):
            if h.id in waiting:
                h.score, h.how = self.min_similarity + TEXT_BONUS / (1 + t), "text"
                ranked.append(h)
        ranked.sort(key=lambda h: -h.score)
        return ranked[:limit]

    def _get_many(self, ids: Sequence[str]) -> list[Hit]:
        if not ids:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, title, body, who, ts, meta FROM docs WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(ids)),),
            ).fetchall()
        return [_hit(r) for r in rows]

    def _loaded_matrix(self) -> Matrix:
        """The in-memory matrix of the embedder's current vectors (with the lock held)."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        if self._matrix is None:
            t0 = time.perf_counter()
            rows = self._conn.execute(
                "SELECT v.id, v.vec, d.kind, d.ts FROM vecs v JOIN docs d ON d.id = v.id AND d.sig = v.sig "
                "WHERE v.model = ? ORDER BY v.id, v.part",
                (self.embedder.model,),
            ).fetchall()
            dims = len(rows[0][1]) // 4 if rows else 0
            m = Matrix(dims)
            item: list[np.ndarray] = []
            for i, (id, blob, kind, ts) in enumerate(rows):
                if len(blob) == dims * 4:
                    item.append(np.frombuffer(blob, dtype=np.float32))
                if i + 1 == len(rows) or rows[i + 1][0] != id:
                    m.set(id, item, kind, float(ts))
                    item = []
            self._matrix = m
            if rows:
                log.info("index: %d vectors in memory in %.2fs", len(m), time.perf_counter() - t0)
        return self._matrix

    async def asearch(self, query: str, **kw: Any) -> list[Hit]:
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: self.search(query, **kw))

    # --- vectors (lazy) ---------------------------------------------------------------

    def _ensure_vectors(self, hits: Sequence[Hit], max_new: int) -> list[Hit]:
        """Embed up to `max_new` of these items that have no current vectors; returns the
        ones left without (the background pass does them)."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        if not hits:
            return []
        with self._lock:
            have = {
                r[0]
                for r in self._conn.execute(
                    "SELECT v.id FROM vecs v JOIN docs d ON d.id = v.id AND d.sig = v.sig "
                    "WHERE v.model = ? AND v.part = 0 AND v.id IN (SELECT value FROM json_each(?))",
                    (self.embedder.model, json.dumps([h.id for h in hits])),
                )
            }
        missing = [h for h in hits if h.id not in have]
        if missing[:max_new]:
            self._embed(missing[:max_new])
        return missing[max_new:]

    def _embed(self, hits: Sequence[Hit]) -> None:
        """Embed every window of these items and keep the vectors."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        t0 = time.perf_counter()
        texts = [_doc_texts(h) for h in hits]
        flat = self.embedder.embed([t for ts in texts for t in ts])
        parts, at = [], 0
        for ts in texts:
            parts.append(flat[at : at + len(ts)])
            at += len(ts)
        self._store_vectors(hits, parts)
        if len(flat) > len(hits) or time.perf_counter() - t0 > 1:
            log.debug(
                "embedded %d items (%d windows) in %.2fs", len(hits), len(flat), time.perf_counter() - t0
            )

    def _store_vectors(self, hits: Sequence[Hit], parts: Sequence[np.ndarray]) -> None:
        if self.embedder is None:
            raise RuntimeError("no embedder")
        model = self.embedder.model
        with self._lock:
            for h, vecs in zip(hits, parts, strict=True):
                # Keyed by the sig of the text that was embedded; if the item changed while
                # it was being embedded, drop the stale vectors (the next pass redoes them).
                sig = _sig(h.title, h.body, h.who)
                row = self._conn.execute("SELECT sig, kind, ts FROM docs WHERE id = ?", (h.id,)).fetchone()
                if not row or row[0] != sig:
                    continue
                vs = [np.asarray(v, dtype=np.float32) for v in vecs]
                self._conn.execute("DELETE FROM vecs WHERE id = ? AND model = ?", (h.id, model))
                self._conn.executemany(
                    "INSERT INTO vecs(id, model, sig, vec, part) VALUES (?,?,?,?,?)",
                    [(h.id, model, sig, v.tobytes(), n) for n, v in enumerate(vs)],
                )
                if self._matrix is not None and vs:
                    if not self._matrix.dims:
                        self._matrix = Matrix(len(vs[0]))
                    if len(vs[0]) == self._matrix.dims:
                        self._matrix.set(h.id, vs, str(row[1]), float(row[2]))

    def embed_pending(self, limit: int = 64) -> int:
        """Embed up to `limit` items whose vectors are missing or out of date (a long item
        stored before it had one per window counts too), newest first."""
        if self.embedder is None:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta FROM docs d "
                "LEFT JOIN vecs v0 ON v0.id = d.id AND v0.model = ? AND v0.part = 0 AND v0.sig = d.sig "
                "LEFT JOIN vecs v1 ON v1.id = d.id AND v1.model = ? AND v1.part = 1 AND v1.sig = d.sig "
                "WHERE v0.id IS NULL OR (length(d.body) > ? AND v1.id IS NULL) ORDER BY d.ts DESC LIMIT ?",
                (self.embedder.model, self.embedder.model, EMBED_CHARS, limit),
            ).fetchall()
        hits = [_hit(r) for r in rows]
        if hits:
            self._embed(hits)
        return len(hits)

    async def background(self, idle: Any, every_s: float = 60.0, batch: int = 64) -> None:
        """Embed new items ahead of time while Scout is idle; unload the model when unused.
        `idle()` says whether Scout is free (not listening to or answering anyone)."""
        loop = asyncio.get_running_loop()
        if self.embedder is not None:  # warm up, so searches can rank by meaning right away
            try:
                await loop.run_in_executor(
                    self._pool, lambda: self.embedder.embed(["warm up"]) if self.embedder else None
                )
            except Exception:
                log.exception("loading the embedding model failed")
        backlog = True  # look right away after starting
        last_files = 0.0
        done = 0  # embedded in this run of batches
        while True:
            # A backlog (a first sync, a new model) is worked through batch after batch;
            # otherwise look again every every_s.
            await asyncio.sleep(BACKLOG_PAUSE_S if backlog else every_s)
            backlog = False
            try:
                if self.journal_root is not None and time.monotonic() - last_files >= every_s:
                    last_files = time.monotonic()
                    n = await loop.run_in_executor(self._pool, self.sync_files)
                    if n:
                        log.info("index: %d journal entries re-read from edited files", n)
                if idle():
                    n = await loop.run_in_executor(self._pool, self.embed_pending, batch)
                    done += n
                    backlog = n == batch
                    if done and not backlog:
                        log.info("index: embedded %d new items ahead of time", done)
                        done = 0
                    if n:
                        continue
                unload = getattr(self.embedder, "unload_if_idle", None)
                if unload is not None:
                    await loop.run_in_executor(self._pool, unload)
            except Exception:
                log.exception("index background pass failed")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        self._pool.shutdown(wait=False)


# Fixed filter clauses, parameterized by _filter_args: kinds (a JSON list, or NULL for
# all) and a start time (or NULL).
_RECENT_SQL = (
    "SELECT id, kind, title, body, who, ts, meta FROM docs "
    "WHERE (? IS NULL OR kind IN (SELECT value FROM json_each(?))) AND (? IS NULL OR ts >= ?) "
    "ORDER BY ts DESC LIMIT ?"
)
_TEXT_SQL = (
    "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta, bm25(docs_fts, 3.0, 1.0, 2.0) AS r "
    "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid WHERE docs_fts MATCH ? "
    "AND (? IS NULL OR d.kind IN (SELECT value FROM json_each(?))) AND (? IS NULL OR d.ts >= ?) "
    "ORDER BY r LIMIT ?"
)


def _filter_args(kinds: Sequence[str] | None, since: float | None) -> tuple[Any, ...]:
    k = json.dumps(list(kinds)) if kinds else None
    return (k, k, since, since)


def _hit(row: Sequence[Any]) -> Hit:
    id, kind, title, body, who, ts, meta = row
    return Hit(id, kind, title, body, who, float(ts), _meta(meta))


def _meta(text: str | None) -> dict[str, Any]:
    try:
        m = json.loads(text) if text else {}
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


EMBED_CHARS = 1000  # one window of an item's text (what embeds well)
WINDOW_OVERLAP = 100  # windows overlap a little, so a sentence cut at an edge is in one whole
MAX_WINDOWS = 20  # an item's first 20 windows (~18,000 characters) are embedded


def _doc_texts(h: Hit) -> list[str]:
    """What gets embedded for an item: its title, who and opening; then each further
    window of the body, with the title for context."""
    out = [" ".join(p for p in (h.title, h.who, h.body[:EMBED_CHARS]) if p)]
    start = EMBED_CHARS - WINDOW_OVERLAP
    while start + WINDOW_OVERLAP < len(h.body) and len(out) < MAX_WINDOWS:
        out.append(" ".join(p for p in (h.title, h.body[start : start + EMBED_CHARS]) if p))
        start += EMBED_CHARS - WINDOW_OVERLAP
    return out


def format_hits(hits: Sequence[Hit], snippets: bool = True) -> str:
    """Compact lines for Claude: [kind] date: title (who): snippet."""
    if not hits:
        return "No matches in Scout's local index."
    lines = []
    for h in hits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.ts))
        line = f"[{h.kind}] {when}: {h.title or '(untitled)'}" + (f" ({h.who})" if h.who else "")
        if snippets and h.body and h.body != h.title:
            line += f": {h.snippet()}"
        lines.append(line)
    return "\n".join(lines)


class Store(Protocol):
    """What callers use: index.Index in the memory service, memory_client.Store elsewhere."""

    def put(
        self, id: str, kind: str, title: str = "", body: str = "", who: str = "",
        ts: float | None = None, meta: dict[str, Any] | None = None,
    ) -> bool: ...  # fmt: skip
    def delete(self, id: str) -> None: ...
    def sync(self, kind: str, items: Iterable[dict[str, Any]]) -> tuple[int, int]: ...
    def get(self, id: str) -> Hit | None: ...
    def count(self, kind: str | None = None) -> int: ...
    def recent(
        self, kinds: Sequence[str] | None = None, limit: int = 20, since: float | None = None
    ) -> list[Hit]: ...
    def prune(self, kind: str, before: float) -> int: ...
    def journal_add(
        self, ts: float, who: str, q: str, a: str,
        tools: Sequence[tuple[str, dict[str, Any]]] = (), private: bool = False,
    ) -> str: ...  # fmt: skip
    def note_add(self, title: str, body: str, ts: float | None = None) -> int: ...
    def note_delete(self, n: int) -> bool: ...
    def task_add(self, title: str, fields: dict[str, Any], context: str = "") -> int: ...
    def task_update(self, n: int, fields: dict[str, Any], add_context: str = "") -> bool: ...
    def search(
        self, query: str, kinds: Sequence[str] | None = None, since: float | None = None,
        limit: int = 8, semantic: bool | None = True,
    ) -> list[Hit]: ...  # fmt: skip
    async def asearch(self, query: str, **kw: Any) -> list[Hit]: ...


_index: Store | None = None  # tests put a local Index here


def default() -> Store:
    """The store Scout uses: the scout-memory service."""
    if _index is not None:
        return _index
    from . import memory_client

    return memory_client.Store(memory_client.client())
