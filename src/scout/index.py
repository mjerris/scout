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
(float32; 384 dimensions is ~1.5 KB a vector). An item has one vector per window of
its text (up to 20), so a short item takes ~1.5 KB and a long one up to ~30 KB; 100,000
vectors are ~150 MB. The matrix loads in the background when the service starts. An
idle background pass embeds new items (table pending lists what still needs a vector,
newest first), a few windows per call to the model so a search's own embedding gets in
between; a search embeds only a few windows itself (its text hits, then the newest new
items of the kinds it asks for), so nothing is ever unreachable by full text, and
nothing leaves the Mac.

The embedder is bge-small-en-v1.5 (ONNX on CPU through fastembed, ~0.7 GB resident
while loaded, loads in ~4 s); it unloads after 15 minutes idle ([index] unload_after_s). The model name and dimension are stored
with each vector, so changing either re-embeds in the background.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from . import journal, links, notefiles, taskfiles
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
CREATE INDEX IF NOT EXISTS docs_ts ON docs(ts);
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
    sig TEXT NOT NULL,
    ids TEXT,  -- the items it held when last read (JSON list); NULL: not known yet
    stat TEXT  -- its size and modified time then: unchanged, it isn't read again
);
CREATE TABLE IF NOT EXISTS vecs (
    id TEXT NOT NULL,
    model TEXT NOT NULL,
    sig TEXT NOT NULL,
    vec BLOB NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (id, model, part)
);
-- Items whose text changed since their vectors were made (or that have none): put()
-- adds them, storing their vectors removes them, so finding work never scans the store.
CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, kind TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS pending_ts ON pending(ts);
CREATE INDEX IF NOT EXISTS pending_kind_ts ON pending(kind, ts);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- How records point at each other (links.py, docs/links.md): a task to what it was found
-- in, an emailed meeting summary to its Granola note, a journal turn to what it used.
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS links_dst ON links(dst);
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
LOAD_RETRY_S = 600.0  # after the model fails to load (offline, missing), wait this long to try again


class EmbedderUnavailable(RuntimeError):
    """The model failed to load a little while ago; not tried again yet."""


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
        batch: int = 4,
    ) -> None:
        # 2 threads and batches of 1-4 measured fastest on this Mac's CPU (38 docs/s in
        # ~250 MB at batch 1); batch 16 was slower (28-32 docs/s) and took ~470 MB.
        self.name = name
        self.threads = threads
        self.batch = batch
        self.dims = dims
        self.idle_s = idle_s
        self.model = f"{name}@{dims}" if dims else name
        self._nomic = name.startswith("nomic-ai/")
        self._m: Any = None
        self._used = 0.0
        self._failed_at: float | None = None  # when loading last failed
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        with self._lock:
            if self._m is None:
                if self._failed_at is not None and time.monotonic() - self._failed_at < LOAD_RETRY_S:
                    raise EmbedderUnavailable(f"embedder {self.name} failed to load; retrying later")
                try:
                    self._load()
                except Exception:
                    self._failed_at = time.monotonic()
                    raise
                self._failed_at = None
            self._used = time.monotonic()
            if self._nomic:
                prefix = "search_query: " if query else "search_document: "
                raw = np.array(
                    list(self._m.embed([prefix + t for t in texts], batch_size=self.batch)), dtype=np.float32
                )
            else:
                f = self._m.query_embed if query else self._m.passage_embed
                raw = np.array(list(f(list(texts), batch_size=self.batch)), dtype=np.float32)
        if self._nomic:
            # Matryoshka: layer-norm, then cut (nomic's recipe).
            raw = (raw - raw.mean(axis=1, keepdims=True)) / (raw.std(axis=1, keepdims=True) + 1e-6)
        if self.dims:
            raw = raw[:, : self.dims]
        out: np.ndarray = raw / (np.linalg.norm(raw, axis=1, keepdims=True) + 1e-9)
        return out

    def _load(self) -> None:
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
        self.kind = np.zeros(0, np.int32)  # each row's kind, as a code
        self.codes: dict[str, int] = {}
        self.ts = np.zeros(0, np.float64)
        self.alive = np.zeros(0, bool)
        self.dead = 0  # dropped rows not yet reclaimed

    def __len__(self) -> int:
        return len(self.by_id)

    def _code(self, kind: str) -> int:
        return self.codes.setdefault(kind, len(self.codes))

    def _grow(self, n: int) -> None:
        if n <= len(self.alive):
            return
        cap = max(n, 2 * len(self.alive), 256)
        for name, fill in (
            ("vecs", np.zeros((cap, self.dims), np.float32)),
            ("kind", np.zeros(cap, np.int32)),
            ("ts", np.zeros(cap)),
            ("alive", np.zeros(cap, bool)),
        ):
            old = getattr(self, name)
            fill[: len(old)] = old
            setattr(self, name, fill)

    def set(self, id: str, vecs: Sequence[np.ndarray], kind: str, ts: float) -> None:
        """An item's vectors (all its windows), replacing any it had."""
        self.drop(id)
        if not len(vecs):
            return
        r = len(self.ids)
        self._grow(r + len(vecs))
        rows = list(range(r, r + len(vecs)))
        self.ids.extend([id] * len(vecs))
        self.vecs[r : r + len(vecs)] = np.asarray(vecs, np.float32)
        self.kind[r : r + len(vecs)] = self._code(kind)
        self.ts[r : r + len(vecs)] = ts
        self.alive[r : r + len(vecs)] = True
        self.by_id[id] = rows

    def drop(self, id: str) -> None:
        rows = self.by_id.pop(id, None)
        if not rows:
            return
        self.alive[rows] = False
        self.dead += len(rows)
        if len(self.ids) > 1024 and self.dead > len(self.ids) // 2:
            self._compact()

    def touch(self, id: str, kind: str, ts: float) -> None:
        rows = self.by_id.get(id)
        if rows:
            self.kind[rows], self.ts[rows] = self._code(kind), ts

    def _compact(self) -> None:
        keep = np.flatnonzero(self.alive[: len(self.ids)])
        self.ids = [self.ids[r] for r in keep]
        self.vecs, self.kind, self.ts = self.vecs[keep], self.kind[keep], self.ts[keep]
        self.alive = np.ones(len(keep), bool)
        self.dead = 0
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
            mask &= np.isin(self.kind[:n], [self.codes[kd] for kd in kinds if kd in self.codes])
        if since is not None:
            mask &= self.ts[:n] >= since
        kept = int(np.count_nonzero(mask))
        if not kept:
            return []
        # Score every row in place and rule out the filtered ones; gather rows (a copy)
        # only when the filter keeps few of them.
        rows: np.ndarray | None = None
        if kept * 8 < n:
            rows = np.flatnonzero(mask)
            sims = self.vecs[rows] @ q
        else:
            sims = self.vecs[:n] @ q
            sims[~mask] = -np.inf
        # Enough rows to find k distinct items (an item's windows can crowd the top).
        shortlist = min(kept, k * 8)
        while True:
            if shortlist < len(sims):
                part = np.argpartition(-sims, shortlist - 1)[:shortlist]
            else:
                part = np.arange(len(sims))
            out: dict[str, float] = {}
            for i in part[np.argsort(-sims[part], kind="stable")]:
                id = self.ids[rows[i] if rows is not None else i]
                if id not in out:
                    out[id] = float(sims[i])
                    if len(out) == k:
                        return list(out.items())
            if shortlist >= kept:
                return list(out.items())
            shortlist = min(kept, shortlist * 4)

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


def _file_sig(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=8).hexdigest()  # as journal.file_sig


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
    _matrix: Matrix | None = field(init=False, repr=False, default=None)  # see load_matrix
    _lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _pool: ThreadPoolExecutor = field(init=False, repr=False)
    _sync_lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _old_vecs: bool = field(init=False, repr=False, default=True)  # another model's vectors may remain
    _counts: dict[str, int] | None = field(init=False, repr=False, default=None)  # items per kind
    # While the matrix loads (without the lock): the items changed meanwhile, redone after.
    _loading: threading.Event | None = field(init=False, repr=False, default=None)
    _dirty: set[str] | None = field(init=False, repr=False, default=None)
    # A search's embedding goes first: the background waits while one is waiting or running.
    _turn: threading.Condition = field(init=False, repr=False, default_factory=threading.Condition)
    _urgent: int = field(init=False, repr=False, default=0)
    _fresh: bool = field(init=False, repr=False, default=False)  # new text since the background looked
    # Where the phone reaches Scout's web UI (links.web_base in the service): record pages.
    web_base: Callable[[], str] = lambda: ""

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Safe in WAL: a power cut can lose the last commits, never the store.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA journal_size_limit=67108864")  # the WAL file shrinks back to 64 MB
        self._conn.execute("PRAGMA analysis_limit=1000")  # PRAGMA optimize stays quick on a big store
        self._add_vector_parts()
        self._conn.executescript(_SCHEMA)
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(files)")]
        for col in ("ids", "stat"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} TEXT")
        self._fill_pending()
        self._link_tasks_once()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")

    def _fill_pending(self) -> None:
        """Once per model (the first start with table pending, or a new model): every item
        without its current vectors (or, if long, only the one from before an item had one
        per window) goes into pending. From then on put() and _store_vectors keep it."""
        if self.embedder is None:
            return
        model = self.embedder.model
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'pending_model'").fetchone()
        if row and row[0] == model:
            return
        t0 = time.perf_counter()
        with self._tx():
            n = self._conn.execute(
                "INSERT OR IGNORE INTO pending(id, kind, ts) SELECT d.id, d.kind, d.ts FROM docs d "
                "LEFT JOIN vecs v0 ON v0.id = d.id AND v0.model = ? AND v0.part = 0 AND v0.sig = d.sig "
                "LEFT JOIN vecs v1 ON v1.id = d.id AND v1.model = ? AND v1.part = 1 AND v1.sig = d.sig "
                "WHERE v0.id IS NULL OR (length(d.body) > ? AND v1.id IS NULL)",
                (model, model, EMBED_CHARS),
            ).rowcount
            self._conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('pending_model', ?)", (model,)
            )
        log.info("index: %d items to embed for %s (found in %.2fs)", n, model, time.perf_counter() - t0)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[None]:
        """One transaction (with the lock held), or part of the one already open."""
        with self._lock:
            if self._conn.in_transaction:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                self._counts = None  # counted again when next asked
                raise
            self._conn.execute("COMMIT")

    def _changed(self, id: str) -> None:
        """Note an item whose vectors, kind or time changed while the matrix loads."""
        if self._dirty is not None:
            self._dirty.add(id)

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
        with self._tx():
            row = self._conn.execute("SELECT kind, ts, meta, sig FROM docs WHERE id = ?", (id,)).fetchone()
            if row and (row[0], row[3]) == (kind, sig) and ts in (None, row[1]) and _meta(row[2]) == meta:
                return False
            if when is None:
                when = row[1] if row else time.time()
            if not row or row[3] != sig:
                self._conn.execute(
                    "INSERT INTO pending(id, kind, ts) VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, ts=excluded.ts",
                    (id, kind, when),
                )
                self._fresh = True
            elif (row[0], row[1]) != (kind, when):
                self._conn.execute("UPDATE pending SET kind = ?, ts = ? WHERE id = ?", (kind, when, id))
            if self._counts is not None and (not row or row[0] != kind):
                self._counts[kind] = self._counts.get(kind, 0) + 1
                if row:
                    self._counts[row[0]] -= 1
            self._changed(id)
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
        with self._tx():
            row = self._conn.execute("DELETE FROM docs WHERE id = ? RETURNING kind", (id,)).fetchone()
            self._conn.execute("DELETE FROM vecs WHERE id = ?", (id,))
            self._conn.execute("DELETE FROM pending WHERE id = ?", (id,))
            # Its own links go with it; links to it stay (a task still names the mail it came
            # from after the mail ages out: resolve() leaves out what's gone).
            self._conn.execute("DELETE FROM links WHERE src = ? OR (dst = ? AND rel = 'same_as')", (id, id))
            if row and self._counts is not None:
                self._counts[row[0]] -= 1
            self._changed(id)
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
        """Items of a kind (or all): counted once, then kept up to date by put and delete."""
        with self._lock:
            if self._counts is None:
                self._counts = dict(self._conn.execute("SELECT kind, count(*) FROM docs GROUP BY kind"))
            return sum(self._counts.values()) if kind is None else self._counts.get(kind, 0)

    def recent(
        self, kinds: Sequence[str] | None = None, limit: int = 20, since: float | None = None
    ) -> list[Hit]:
        """The newest items (of these kinds, since `since`), newest first."""
        sql, args = _recent_sql(kinds, since, limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
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
        refs: Sequence[str] = (),
    ) -> str:
        """Append one exchange to the day's journal file and index it; returns the file.
        `refs`: the records the answer was about (its "about" links)."""
        if self.journal_root is None:
            raise RuntimeError("no journal")
        e = journal.Entry(
            ts, who, q, a, [(str(n), dict(args or {})) for n, args in tools], private, [str(r) for r in refs]
        )
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
        if fields.get("source") and not fields.get("link"):
            url = self.source_url(str(fields["source"]))
            fields = {**fields, "link": url} if url else fields
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
        """Make the index hold exactly the items of one file (a journal day, a note or a
        task), or none if it's gone. Returns how many items changed. An item another
        file also holds (a renamed file, two with one number) stays, as that file has it."""
        what = self._file_kind(p)
        if what is None:
            return 0
        kind, prefix, exact = what
        # One sync at a time, from one read of the file: two at once could each delete
        # what the other just put, or record a sig of bytes they didn't parse.
        with self._sync_lock:
            try:
                stat = _file_stat(p)  # before the read: an edit during it shows next time
                data: bytes | None = p.read_bytes()
            except FileNotFoundError:
                stat, data = "", None
            items = self._parse(kind, p, data) if data is not None else []
            changed = sum(self.put(kind=kind, **it) for it in items)
            if kind == "task":
                for it in items:
                    self._link_task(it["id"], it.get("meta") or {}, str(it.get("body") or ""))
            elif kind == "turn":  # a journal entry's refs line: what the answer was about
                for it in items:
                    refs = [str(r) for r in (it.get("meta") or {}).get("refs") or [] if str(r) != it["id"]]
                    self._set_links(it["id"], ("about",), [(r, "about") for r in dict.fromkeys(refs)])
            keep = {it["id"] for it in items}
            with self._lock:
                row = self._conn.execute("SELECT ids FROM files WHERE path = ?", (str(p),)).fetchone()
                if row and row[0] is not None:
                    held = set(json.loads(row[0]))
                elif row or exact:  # read before ids were kept: what its name covers
                    sql = (
                        "SELECT id FROM docs WHERE kind = ? AND id = ?"
                        if exact
                        else "SELECT id FROM docs WHERE kind = ? AND substr(id, 1, length(?)) = ?"
                    )
                    held = {
                        r[0]
                        for r in self._conn.execute(sql, (kind, prefix) if exact else (kind, prefix, prefix))
                    }
                else:
                    held = set()  # a file never read: nothing here is from it
                old = held - keep
                others = self._holders(old, p)
            gone = [id for id in old if id not in others]
            for id in gone:
                self.delete(id)
            with self._lock:
                if data is not None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO files(path, sig, ids, stat) VALUES (?, ?, ?, ?)",
                        (str(p), _file_sig(data), json.dumps(sorted(keep)), stat),
                    )
                else:
                    self._conn.execute("DELETE FROM files WHERE path = ?", (str(p),))
            # What another file holds too is now that file's version.
            for q in sorted(set(others.values())):
                changed += self.sync_file(Path(q))
        return changed + len(gone)

    def _holders(self, ids: set[str], p: Path) -> dict[str, str]:
        """Which other known file holds each of these items (with the lock held)."""
        if not ids:
            return {}
        rows = self._conn.execute(
            "SELECT j.value, f.path FROM files f, json_each(f.ids) j WHERE f.path != ? "
            "AND j.value IN (SELECT value FROM json_each(?))",
            (str(p), json.dumps(sorted(ids))),
        )
        return {str(id): str(path) for id, path in rows}

    def _parse(self, kind: str, p: Path, data: bytes) -> list[dict[str, Any]]:
        """A file's items from these bytes (bad UTF-8 replaced, not refused)."""
        text = data.decode("utf-8", "replace")
        if kind == "turn":
            return journal.parse(p, text)
        parse = notefiles.parse if kind == "note" else taskfiles.parse
        try:
            if text.encode() == data:
                return parse(p)
        except UnicodeDecodeError:  # changed under us to bad bytes: parse the text read
            pass
        # Bad bytes: parse a cleaned copy (same name and time) beside the store.
        tmp = self.path.parent / ".sync-tmp" / p.name
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text(text)
            st = p.stat() if p.exists() else None
            if st is not None:
                os.utime(tmp, (st.st_atime, st.st_mtime))
            return parse(tmp)
        finally:
            tmp.unlink(missing_ok=True)

    def sync_files(self) -> int:
        """Re-index journal, note and task files that changed, appeared or went away
        since last time (an edit, a deleted entry, a renamed file, a file restored from a
        backup). A file that can't be read is skipped and tried again next time. Returns
        items changed."""
        if self.journal_root is None:
            return 0
        with self._lock:
            known = {
                path: (sig, ids is not None, stat)
                for path, sig, ids, stat in self._conn.execute("SELECT path, sig, ids, stat FROM files")
            }
        changed = 0
        root = self.journal_root
        files = journal.day_files(root) + notefiles.files(root) + taskfiles.files(root)
        # What went away first, so an item that moved to another file is put back by it.
        for path in sorted(set(known) - {str(p) for p in files}):
            changed += self._try_sync(Path(path))
        for p in files:
            sig, has_ids, stat = known.get(str(p), ("", False, None))
            try:
                now = _file_stat(p)
                if has_ids and stat == now:
                    continue  # same size and time: not read at all
                same = has_ids and sig == journal.file_sig(p)
            except OSError as exc:
                log.warning("index: can't read %s: %s", p, exc)
                continue
            if same:  # touched but not changed: remember the new time, skip the parse
                with self._lock:
                    self._conn.execute("UPDATE files SET stat = ? WHERE path = ?", (now, str(p)))
            else:
                changed += self._try_sync(p)
        return changed

    def _try_sync(self, p: Path) -> int:
        try:
            return self.sync_file(p)
        except Exception:
            log.exception("index: couldn't index %s; trying again next time", p)
            return 0

    # --- links between records (links.py, docs/links.md) -------------------------------

    def link(self, src: str, dst: str, rel: str) -> None:
        """Note that record src points at dst (rel: one of links.RELS). Idempotent."""
        if rel not in links.RELS:
            raise ValueError(f"rel must be one of {', '.join(links.RELS)}")
        if not src or not dst or src == dst:
            raise ValueError("a link needs two different records")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO links(src, dst, rel, ts) VALUES (?, ?, ?, ?)",
                (src, dst, rel, time.time()),
            )

    def links(self, id: str) -> list[dict[str, Any]]:
        """Every link of a record, either way: [{id (the other record), rel, dir: "out"
        (this one points at it) or "in"}], oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT dst, rel, 'out', ts FROM links WHERE src = ? "
                "UNION ALL SELECT src, rel, 'in', ts FROM links WHERE dst = ? ORDER BY 4",
                (id, id),
            ).fetchall()
        return [{"id": r[0], "rel": r[1], "dir": r[2]} for r in rows]

    def _set_links(self, src: str, rels: Sequence[str], to: Sequence[tuple[str, str]]) -> None:
        """Make src's links of these rels exactly `to` ((dst, rel) pairs); the ones kept
        keep their time."""
        with self._tx():
            have = set(
                self._conn.execute(
                    f"SELECT dst, rel FROM links WHERE src = ? AND rel IN ({','.join('?' * len(rels))})",  # noqa: S608 (placeholders only)
                    (src, *rels),
                ).fetchall()
            )
            want = set(to)
            for dst, rel in have - want:
                self._conn.execute("DELETE FROM links WHERE src = ? AND dst = ? AND rel = ?", (src, dst, rel))
            now = time.time()
            for dst, rel in want - have:
                self._conn.execute(
                    "INSERT OR IGNORE INTO links(src, dst, rel, ts) VALUES (?, ?, ?, ?)", (src, dst, rel, now)
                )

    def source_record(self, key: str) -> str | None:
        """The record a task's source key names (links.source_record; a Slack read's key
        is found by its conversation and newest message)."""
        rec = links.source_record(key)
        m = re.fullmatch(r"slack:([^:@\s]+)@(\d+(?:\.\d+)?)", rec or "")
        if m is None:
            return rec
        ch, ts = m.group(1), float(m.group(2))
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM docs WHERE kind = 'slack' AND id >= ? AND id < ? AND instr(id, '#') = 0 "
                "AND (abs(CAST(json_extract(meta, '$.last_ts') AS REAL) - ?) < 0.000002 "
                "OR abs(CAST(json_extract(meta, '$.ts') AS REAL) - ?) < 0.000002) ORDER BY ts DESC LIMIT 1",
                (f"slack:{ch}:", f"slack:{ch};", ts, ts),
            ).fetchone()
        return str(row[0]) if row else None

    def _link_task(self, id: str, meta: dict[str, Any], body: str) -> None:
        """A task's found_in (its source) and merged_from (sources merged into it) links,
        from its file: the file is the truth, so they're re-derived whenever it's read."""
        recs: list[str] = []
        for key in links.task_sources(meta, body):
            rec = self.source_record(key)
            if rec and rec != id and rec not in recs:
                recs.append(rec)
        self._set_links(
            id, ("found_in", "merged_from"),
            [(r, "found_in" if i == 0 else "merged_from") for i, r in enumerate(recs)],
        )  # fmt: skip

    def _link_tasks_once(self) -> None:
        """Once: links for the tasks indexed before there were links (later, each task's
        are made when its file is read)."""
        with self._lock:
            if self._conn.execute("SELECT 1 FROM settings WHERE key = 'links_tasks'").fetchone():
                return
            rows = self._conn.execute("SELECT id, meta, body FROM docs WHERE kind = 'task'").fetchall()
        for id, meta, body in rows:
            self._link_task(id, _meta(meta), body)
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('links_tasks', '1')")
        if rows:
            log.info("index: links made for %d tasks", len(rows))

    def link_meetings(self, since: float | None = None) -> int:
        """same_as links from meeting summaries that came by email to the Granola note of
        the same meeting (links.same_meeting: its start just before the email, a similar
        title, and no equally good other). Returns how many emails are linked now."""
        since = time.time() - 30 * 86400 if since is None else since
        with self._lock:
            emails = self._conn.execute(
                "SELECT id, ts, title FROM docs WHERE kind = 'meeting' AND ts >= ? "
                "AND substr(id, 1, 12) = 'mailmeeting:'",
                (since,),
            ).fetchall()
        linked = 0
        for id, ts, title in emails:
            with self._lock:
                cands = self._conn.execute(
                    "SELECT id, ts, title FROM docs WHERE kind = 'meeting' AND ts BETWEEN ? AND ? "
                    "AND substr(id, 1, 8) = 'granola:' AND instr(id, '#') = 0",
                    (ts - links.SAME_MEETING_BEFORE_S, ts + links.SAME_MEETING_AFTER_S),
                ).fetchall()
            found = [(str(c[0]), float(c[1]), str(c[2])) for c in cands]
            g = links.same_meeting(float(ts), str(title), found)
            self._set_links(id, ("same_as",), [(g, "same_as")] if g else [])
            linked += g is not None
        return linked

    def missing(self, kind: str, key: str, limit: int = 20) -> list[Hit]:
        """The newest items of a kind whose meta has no `key` (partial items aside)."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("key must be a plain field name")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLS} FROM docs WHERE kind = ? AND json_extract(meta, '$.{key}') IS NULL "  # noqa: S608 (key checked above)
                "AND coalesce(json_extract(meta, '$.partial'), 0) = 0 ORDER BY ts DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [_hit(r) for r in rows]

    def open_url(self, id: str) -> str | None:
        """Where a record opens: a Granola note's page, a Slack message's permalink, a
        mail's message:// link (from its Message-ID), an emailed meeting summary's mail;
        Scout's own records (tasks, notes, journal turns, facts) their page on Scout's
        web UI. None when there's nowhere to go (no such record; mail whose Message-ID
        isn't known yet)."""
        h = self.get(id)
        return self._url(h) if h is not None else None

    def _url(self, h: Hit) -> str | None:
        m = h.meta
        if h.kind == "meeting" and m.get("source") == "email":
            mail = self.get(str(m.get("note") or f"mail:{m.get('mail_id')}"))
            return self._url(mail) if mail is not None and mail.kind == "mail" else None
        if h.kind == "meeting":
            return str(m["url"]) if m.get("url") else None
        if h.kind == "slack":
            return str(m["permalink"]) if m.get("permalink") else None
        if h.kind == "mail":
            return links.message_url(str(m.get("message_id") or ""))
        if h.kind in links.OWN_KINDS:
            return links.page_url(self.web_base(), h.id)
        return None

    def source_url(self, key: str) -> str | None:
        """The link out of a task source key's record (not a Scout page), or None."""
        rec = self.source_record(key)
        h = self.get(rec) if rec else None
        return self._url(h) if h is not None and h.kind not in links.OWN_KINDS else None

    def resolve(self, id: str) -> dict[str, Any] | None:
        """A record with where it opens and what it's linked to: {id, kind, title, url,
        page, private, related: [{id, rel, dir, kind, title, url}]}. A task's url is its
        source's (the meeting it was found in) when that has one, else its own page;
        `page` is a Scout record's page on the web UI (None for records from outside)."""
        h = self.get(id)
        if h is None:
            return None
        related = []
        for ln in self.links(id):
            r = self.get(ln["id"])
            if r is None:
                continue
            related.append({**ln, "kind": r.kind, "title": r.title, "url": self._url(r)})
        own = h.kind in links.OWN_KINDS
        page = links.page_url(self.web_base(), h.id) if own else None
        url = self._url(h)
        if h.kind == "task":
            src = next((r["url"] for r in related if r["rel"] == "found_in" and r["url"]), None)
            url = src or str(h.meta.get("link") or "") or page
        private = (
            h.kind in links.PRIVATE_KINDS
            or bool(h.meta.get("private"))
            or (h.kind == "task" and str(h.meta.get("from") or "") in ("mail", "meeting", "slack"))
        )
        return {"id": h.id, "kind": h.kind, "title": h.title, "url": url, "page": page,
                "private": private, "related": related}  # fmt: skip

    def upkeep(self) -> None:
        """The links that follow from what's stored: emailed meeting summaries to their
        Granola notes, and tasks' `- link:` lines. Never fails the caller."""
        try:
            self.link_meetings()
            n = self.link_tasks()
            if n:
                log.info("index: %d tasks got their link", n)
        except Exception:
            log.exception("index: making links failed; trying again next time")

    def link_tasks(self) -> int:
        """Tasks with a source but no `- link:` line get one when their source has a link
        out (a mail's waits for its Message-ID). Returns how many were written."""
        if self.journal_root is None:
            return 0
        n = 0
        for h in self.missing("task", "link", limit=5000):
            src = str(h.meta.get("source") or "")
            url = self.source_url(src) if src else None
            if url and self.task_update(int(h.meta.get("n") or 0), {"link": url}):
                n += 1
        return n

    # --- searching ------------------------------------------------------------------

    def text_search(
        self, query: str, kinds: Sequence[str] | None = None, since: float | None = None, limit: int = 50
    ) -> list[Hit]:
        """Full-text matches, best first (BM25: titles count most, then people, then bodies)."""
        q = fts_query(query)
        if not q:
            return []
        where, args = _filters(kinds, since, "d.")
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta, bm25(docs_fts, 3.0, 1.0, 2.0) AS r "  # noqa: S608 (only placeholders and fixed column names are joined in)
                f"FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid WHERE docs_fts MATCH ?{where} "
                "ORDER BY r LIMIT ?",
                (q, *args, limit),
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
        # Items without a vector yet: embed a few windows now (text hits first, then the
        # newest new items of the kinds asked for), so what was just added is found by
        # meaning; the background does the rest. Ahead of the background: it waits
        # between its small calls to the model while a search embeds.
        try:
            unranked, used = self._ensure_vectors(text, self.max_new_per_search, MAX_WINDOWS_PER_SEARCH)
            if used < MAX_WINDOWS_PER_SEARCH:
                seen = {h.id for h in text}
                new = [h for h in self._pending_of(kinds, since, self.max_new_per_search) if h.id not in seen]
                now = _within(new, self.max_new_per_search, MAX_WINDOWS_PER_SEARCH - used, at_least_one=False)
                if now:
                    self._embed(now, urgent=True)
            q = self._call_embedder([query], query=True, urgent=True)[0]
        except Exception as exc:  # the model can't load (offline, missing): full text still works
            log.warning(
                "index: searching by meaning failed (%s: %s); full text only", type(exc).__name__, exc
            )
            return text[:limit]
        waiting = {h.id for h in unranked}
        self.load_matrix()
        with self._lock:
            m = self._matrix
            assert m is not None  # noqa: S101 (loaded above; never unset)
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

    def load_matrix(self) -> Matrix:
        """The in-memory matrix of the embedder's current vectors, read from the store the
        first time (the service does it in the background at start). The read streams rows
        into one preallocated array through its own connection, without the lock, so the
        store stays usable meanwhile; items changed during it are redone after. A second
        caller waits for the read already under way."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        while True:
            with self._lock:
                if self._matrix is not None:
                    return self._matrix
                loading = self._loading
                if loading is None:
                    loading = self._loading = threading.Event()
                    self._dirty = set()
                    break
            loading.wait()  # another thread is reading it; if that failed, read it here
        try:
            t0 = time.perf_counter()
            m = self._read_matrix(self.embedder.model)
            with self._lock:
                for id in self._dirty or ():
                    m = self._reload_item(m, id)
                self._matrix = m
            if len(m.ids):
                log.info(
                    "index: %d items (%d vectors) in memory in %.2fs",
                    len(m),
                    len(m.ids),
                    time.perf_counter() - t0,
                )
            return m
        finally:
            with self._lock:
                self._loading, self._dirty = None, None
            loading.set()

    def _loaded_matrix(self) -> Matrix:
        """load_matrix, by its earlier name."""
        return self.load_matrix()

    def _read_matrix(self, model: str) -> Matrix:
        conn = sqlite3.connect(self.path)
        try:
            # Rows in storage order (an item's are stored together); the count (from the
            # primary key's index) sizes the array, the join drops vectors of old text.
            n = int(conn.execute("SELECT count(*) FROM vecs WHERE model = ?", (model,)).fetchone()[0])
            m = Matrix(0)
            ids: list[str] = []
            kind = np.zeros(n, np.int32)
            ts = np.zeros(n, np.float64)
            vecs = np.zeros((0, 0), np.float32)
            i = 0
            for id, blob, k, t in conn.execute(
                "SELECT v.id, v.vec, d.kind, d.ts FROM vecs v JOIN docs d ON d.id = v.id AND d.sig = v.sig "
                "WHERE v.model = ?",
                (model,),
            ):
                if not m.dims:
                    m.dims = len(blob) // 4
                    vecs = np.empty((n, m.dims), np.float32)
                if len(blob) != m.dims * 4 or i >= n:
                    continue
                vecs[i] = np.frombuffer(blob, np.float32)
                kind[i] = m._code(k)
                ts[i] = t
                ids.append(id)
                i += 1
        finally:
            conn.close()
        m.ids, m.vecs, m.kind, m.ts = ids, vecs[:i], kind[:i], ts[:i]
        m.alive = np.ones(i, bool)
        for r, id in enumerate(ids):
            m.by_id.setdefault(id, []).append(r)
        return m

    def _reload_item(self, m: Matrix, id: str) -> Matrix:
        """Bring one item's rows in the matrix up to date with the store (lock held)."""
        if self.embedder is None:
            return m
        rows = self._conn.execute(
            "SELECT v.vec, d.kind, d.ts FROM vecs v JOIN docs d ON d.id = v.id AND d.sig = v.sig "
            "WHERE v.id = ? AND v.model = ? ORDER BY v.part",
            (id, self.embedder.model),
        ).fetchall()
        if not rows:
            m.drop(id)
            return m
        if not m.dims and not len(m.ids):
            m = Matrix(len(rows[0][0]) // 4)
        vs = [np.frombuffer(r[0], np.float32) for r in rows if len(r[0]) == m.dims * 4]
        m.set(id, vs, str(rows[0][1]), float(rows[0][2]))
        return m

    async def asearch(self, query: str, **kw: Any) -> list[Hit]:
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: self.search(query, **kw))

    # --- vectors (lazy) ---------------------------------------------------------------

    def _ensure_vectors(self, hits: Sequence[Hit], max_new: int, max_windows: int) -> tuple[list[Hit], int]:
        """Embed up to `max_new` of these items that have no current vectors, within
        `max_windows` windows; returns the ones left without (the background pass does
        them) and the windows embedded."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        if not hits:
            return [], 0
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
        # One too long for the budget waits for the background (and ranks by text).
        now = _within(missing, max_new, max_windows, at_least_one=False)
        if now:
            self._embed(now, urgent=True)
        done = {h.id for h in now}
        return [h for h in missing if h.id not in done], sum(len(_doc_texts(h)) for h in now)

    def _call_embedder(self, texts: Sequence[str], query: bool = False, urgent: bool = False) -> np.ndarray:
        """One call to the model. An urgent one (a search) is next in line: the background
        waits for it before its next call (at most URGENT_WAIT_S, so it can't starve)."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        if not urgent:
            with self._turn:
                self._turn.wait_for(lambda: self._urgent == 0, URGENT_WAIT_S)
            return self.embedder.embed(texts, query=query)
        with self._turn:
            self._urgent += 1
        try:
            return self.embedder.embed(texts, query=query)
        finally:
            with self._turn:
                self._urgent -= 1
                self._turn.notify_all()

    def _embed(self, hits: Sequence[Hit], urgent: bool = False) -> None:
        """Embed every window of these items and keep the vectors, in calls of at most
        EMBED_BATCH_WINDOWS windows (so a search waits for one small call at most)."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        t0 = time.perf_counter()
        windows = 0
        at = 0
        while at < len(hits):
            batch = _within(hits[at:], len(hits), EMBED_BATCH_WINDOWS)
            texts = [_doc_texts(h) for h in batch]
            all_texts = [t for ts in texts for t in ts]
            flat = np.concatenate([
                self._call_embedder(all_texts[j : j + EMBED_BATCH_WINDOWS], urgent=urgent)
                for j in range(0, len(all_texts), EMBED_BATCH_WINDOWS)
            ])  # fmt: skip
            parts, i = [], 0
            for ts in texts:
                parts.append(flat[i : i + len(ts)])
                i += len(ts)
            self._store_vectors(batch, parts)
            windows += len(flat)
            at += len(batch)
        if windows > len(hits) or time.perf_counter() - t0 > 1:
            log.debug("embedded %d items (%d windows) in %.2fs", len(hits), windows, time.perf_counter() - t0)

    def _store_vectors(self, hits: Sequence[Hit], parts: Sequence[np.ndarray]) -> None:
        if self.embedder is None:
            raise RuntimeError("no embedder")
        model = self.embedder.model
        with self._tx():
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
                self._conn.execute("DELETE FROM pending WHERE id = ?", (h.id,))
                self._changed(h.id)
                if self._matrix is not None and vs:
                    if not self._matrix.dims:
                        self._matrix = Matrix(len(vs[0]))
                    if len(vs[0]) == self._matrix.dims:
                        self._matrix.set(h.id, vs, str(row[1]), float(row[2]))

    def embed_pending(self, limit: int = 64, max_windows: int | None = None) -> int:
        """Embed up to `limit` items (within `max_windows` windows) whose vectors are
        missing or out of date (a long item stored before it had one per window counts
        too), newest first, from table pending. With none left, removes a batch of
        another model's vectors."""
        if self.embedder is None:
            return 0
        with self._lock:
            self._fresh = False
            rows = self._conn.execute(
                "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta FROM pending p "
                "JOIN docs d ON d.id = p.id ORDER BY p.ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        hits = [_hit(r) for r in rows]
        if max_windows is not None:
            hits = _within(hits, limit, max_windows)
        if hits:
            self._embed(hits)
        elif self._old_vecs:
            self._drop_old_vectors()
        return len(hits)

    def _pending_of(self, kinds: Sequence[str] | None, since: float | None, limit: int) -> list[Hit]:
        """The newest items (of these kinds) still waiting for vectors (indexed: cheap)."""
        where, args = _filters(kinds, since, "p.")
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta FROM pending p "  # noqa: S608 (only placeholders and fixed column names are joined in)
                f"JOIN docs d ON d.id = p.id WHERE 1{where} ORDER BY p.ts DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [_hit(r) for r in rows]

    def optimize(self) -> None:
        """SQLite's own upkeep (fresh statistics for the query planner), while idle."""
        with self._lock:
            self._conn.execute("PRAGMA optimize")

    def _drop_old_vectors(self, batch: int = 5000) -> None:
        """One batch of vectors from another model (or size): the current one's are done."""
        if self.embedder is None:
            return
        with self._lock:
            n = self._conn.execute(
                "DELETE FROM vecs WHERE rowid IN (SELECT rowid FROM vecs WHERE model != ? LIMIT ?)",
                (self.embedder.model, batch),
            ).rowcount
            if n < batch:
                self._old_vecs = False
        if n:
            log.info("index: removed %d vectors of an old model", n)

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
        last_optimize = time.monotonic()
        done = 0  # embedded in this run of batches
        while True:
            # A backlog (a first sync, a new model) is worked through batch after batch;
            # otherwise look again every every_s, or within a second of new text.
            wait = BACKLOG_PAUSE_S if backlog else every_s
            t0 = time.monotonic()
            while True:
                left = wait - (time.monotonic() - t0)
                if left <= 0 or (not backlog and self._fresh and idle()):
                    break
                await asyncio.sleep(min(left, 1.0))
            backlog = False
            try:
                if self.journal_root is not None and time.monotonic() - last_files >= every_s:
                    last_files = time.monotonic()
                    n = await loop.run_in_executor(self._pool, self.sync_files)
                    if n:
                        log.info("index: %d journal entries re-read from edited files", n)
                    await loop.run_in_executor(self._pool, self.upkeep)
                if idle():
                    n = await loop.run_in_executor(self._pool, self.embed_pending, batch)
                    done += n
                    backlog = n == batch or self._old_vecs
                    if done and not backlog:
                        log.info("index: embedded %d new items ahead of time", done)
                        done = 0
                    if n:
                        continue
                    if time.monotonic() - last_optimize >= OPTIMIZE_EVERY_S:
                        last_optimize = time.monotonic()
                        await loop.run_in_executor(self._pool, self.optimize)
                unload = getattr(self.embedder, "unload_if_idle", None)
                if unload is not None:
                    await loop.run_in_executor(self._pool, unload)
            except Exception:
                log.exception("index background pass failed")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
        self._pool.shutdown(wait=False)


# Filters are written into each statement only when given, so SQLite can use the
# indexes (docs_kind_ts, docs_ts); "? IS NULL OR ..." would keep it from them.
_COLS = "id, kind, title, body, who, ts, meta"


def _filters(kinds: Sequence[str] | None, since: float | None, col: str = "") -> tuple[str, list[Any]]:
    """' AND kind IN (?,..) AND ts >= ?' (each only when given) and its arguments."""
    sql, args = "", list[Any]()
    if kinds:
        sql += f" AND {col}kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if since is not None:
        sql += f" AND {col}ts >= ?"
        args.append(since)
    return sql, args


def _recent_sql(kinds: Sequence[str] | None, since: float | None, limit: int) -> tuple[str, list[Any]]:
    """The newest items: one index range read backwards per kind (several kinds: the
    newest `limit` of each, merged), never a sort of the whole store."""
    if not kinds or len(set(kinds)) == 1:
        where, args = _filters(kinds[:1] if kinds else None, since)
        sql = f"SELECT {_COLS} FROM docs WHERE 1{where} ORDER BY ts DESC LIMIT ?"  # noqa: S608 (only placeholders and fixed column names are joined in)
        return sql, [*args, limit]
    parts, args = [], []
    for k in dict.fromkeys(kinds):
        where, a = _filters([k], since)
        parts.append(f"SELECT * FROM (SELECT {_COLS} FROM docs WHERE 1{where} ORDER BY ts DESC LIMIT ?)")  # noqa: S608 (only placeholders and fixed column names are joined in)
        args += [*a, limit]
    return " UNION ALL ".join(parts) + " ORDER BY ts DESC LIMIT ?", [*args, limit]


def _file_stat(p: Path) -> str:
    """A file's size and modified time (ns): what tells an unchanged file without reading it."""
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


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
MAX_WINDOWS_PER_SEARCH = 8  # windows a search may embed itself (~0.25 s); the background does the rest
EMBED_BATCH_WINDOWS = 8  # windows in one call to the model: a search waits for one call at most
URGENT_WAIT_S = 5.0  # the background waits at most this long for searches' embedding
OPTIMIZE_EVERY_S = 3600.0  # PRAGMA optimize while idle, this often
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


def _within(hits: Sequence[Hit], max_items: int, max_windows: int, at_least_one: bool = True) -> list[Hit]:
    """The first of these items, up to max_items, whose windows fit in max_windows (with
    at_least_one, the first item however long)."""
    out: list[Hit] = []
    n = 0
    for h in hits[:max_items]:
        w = len(_doc_texts(h))
        if (out or not at_least_one) and n + w > max_windows:
            break
        out.append(h)
        n += w
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
        refs: Sequence[str] = (),
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
    def link(self, src: str, dst: str, rel: str) -> None: ...
    def links(self, id: str) -> list[dict[str, Any]]: ...
    def open_url(self, id: str) -> str | None: ...
    def resolve(self, id: str) -> dict[str, Any] | None: ...
    def missing(self, kind: str, key: str, limit: int = 20) -> list[Hit]: ...


_index: Store | None = None  # tests put a local Index here


def default() -> Store:
    """The store Scout uses: the scout-memory service."""
    if _index is not None:
        return _index
    from . import memory_client

    return memory_client.Store(memory_client.client())
