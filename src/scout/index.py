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
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

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
CREATE TABLE IF NOT EXISTS vecs (
    id TEXT NOT NULL,
    model TEXT NOT NULL,
    sig TEXT NOT NULL,
    vec BLOB NOT NULL,
    PRIMARY KEY (id, model)
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
    item with its kind and time, so a search filters and scores with numpy. Rows are
    added and dropped as items change; dropped rows are reused by compaction."""

    def __init__(self, dims: int) -> None:
        self.dims = dims
        self.ids: list[str] = []
        self.rows: dict[str, int] = {}
        self.vecs = np.zeros((0, dims), np.float32)
        self.kinds: list[str] = []
        self.ts = np.zeros(0, np.float64)
        self.alive = np.zeros(0, bool)

    def __len__(self) -> int:
        return len(self.rows)

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

    def add(self, id: str, vec: np.ndarray, kind: str, ts: float) -> None:
        r = self.rows.get(id)
        if r is None:
            r = len(self.ids)
            self._grow(r + 1)
            self.ids.append(id)
            self.kinds.append(kind)
            self.rows[id] = r
        self.vecs[r], self.ts[r], self.alive[r], self.kinds[r] = vec, ts, True, kind

    def drop(self, id: str) -> None:
        r = self.rows.pop(id, None)
        if r is not None:
            self.alive[r] = False
            if len(self.ids) > 1024 and len(self.rows) < len(self.ids) // 2:
                self._compact()

    def touch(self, id: str, kind: str, ts: float) -> None:
        r = self.rows.get(id)
        if r is not None:
            self.kinds[r], self.ts[r] = kind, ts

    def _compact(self) -> None:
        keep = [r for r in range(len(self.ids)) if self.alive[r]]
        self.ids = [self.ids[r] for r in keep]
        self.kinds = [self.kinds[r] for r in keep]
        self.vecs, self.ts = self.vecs[keep].copy(), self.ts[keep].copy()
        self.alive = np.ones(len(keep), bool)
        self.rows = {id: r for r, id in enumerate(self.ids)}

    def top(
        self, q: np.ndarray, k: int, kinds: Sequence[str] | None = None, since: float | None = None
    ) -> list[tuple[str, float]]:
        """The k most similar items (id, cosine), best first."""
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
        if len(rows) > k:
            part = np.argpartition(-sims, k - 1)[:k]
            rows, sims = rows[part], sims[part]
        order = np.argsort(-sims, kind="stable")
        return [(self.ids[rows[i]], float(sims[i])) for i in order]


_WORD = re.compile(r"[^\W_][\w']*")  # any script: "José", "Müller", "café" (FTS folds the accents)
_STOP = frozenset({"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "about", "from", "by", "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "what", "whats", "who", "where", "when", "how", "which", "that", "this", "it", "its", "my", "me", "i", "you", "your", "we", "our", "they", "their", "there", "have", "has", "had", "not", "no", "so", "just", "can", "could", "would", "will", "any", "some"})  # fmt: skip


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
    _matrix: Matrix | None = field(init=False, repr=False, default=None)  # loaded on first use
    _lock: threading.RLock = field(init=False, repr=False, default_factory=threading.RLock)
    _pool: ThreadPoolExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")

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
        _, _, unranked = self._vectors(text, self.max_new_per_search)
        if not unranked:  # the text hits didn't use up the budget
            self.embed_pending(self.max_new_per_search)
        q = self.embedder.embed([query], query=True)[0]
        with self._lock:
            near = self._loaded_matrix().top(q, max(limit * 4, 60), kinds, since)
        sims = dict(near)
        text_rank = {h.id: i for i, h in enumerate(text)}
        # A text hit just outside the meaning shortlist still gets its own similarity.
        missing = [h for h in text if h.id not in sims and h.id not in {u.id for u in unranked}]
        if missing:
            vecs, kept, _ = self._vectors(missing, 0)
            for h, v in zip(kept, vecs @ q, strict=True):
                sims[h.id] = float(v)
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
        waiting = {h.id for h in unranked}
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
                "WHERE v.model = ?",
                (self.embedder.model,),
            ).fetchall()
            dims = len(rows[0][1]) // 4 if rows else 0
            m = Matrix(dims)
            for id, blob, kind, ts in rows:
                if len(blob) == dims * 4:
                    m.add(id, np.frombuffer(blob, dtype=np.float32), kind, float(ts))
            self._matrix = m
            if rows:
                log.info("index: %d vectors in memory in %.2fs", len(m), time.perf_counter() - t0)
        return self._matrix

    async def asearch(self, query: str, **kw: Any) -> list[Hit]:
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: self.search(query, **kw))

    # --- vectors (lazy) ---------------------------------------------------------------

    def _vectors(
        self, hits: Sequence[Hit], max_new: int | None = None
    ) -> tuple[np.ndarray, list[Hit], list[Hit]]:
        """(vectors, the hits they belong to, hits left without one): cached vectors plus
        up to `max_new` embedded now (all when None), which are cached too."""
        if self.embedder is None:
            raise RuntimeError("no embedder")
        model = self.embedder.model
        have: dict[str, np.ndarray] = {}
        with self._lock:
            for id, blob in self._conn.execute(
                "SELECT v.id, v.vec FROM vecs v JOIN docs d ON d.id = v.id AND d.sig = v.sig "
                "WHERE v.model = ? AND v.id IN (SELECT value FROM json_each(?))",
                (model, json.dumps([h.id for h in hits])),
            ):
                have[id] = np.frombuffer(blob, dtype=np.float32)
        missing = [h for h in hits if h.id not in have]
        skipped = missing[max_new:] if max_new is not None else []
        missing = missing[:max_new] if max_new is not None else missing
        if missing:
            t0 = time.perf_counter()
            new = self.embedder.embed([_doc_text(h) for h in missing])
            self._store_vectors(missing, new)
            for h, v in zip(missing, new, strict=True):
                have[h.id] = v
            log.info("embedded %d items in %.2fs", len(missing), time.perf_counter() - t0)
        kept = [h for h in hits if h.id in have]
        dims = next(iter(have.values())).shape[0] if have else 1
        vecs = np.stack([have[h.id] for h in kept]) if kept else np.zeros((0, dims), np.float32)
        return vecs, kept, skipped

    def _store_vectors(self, hits: Sequence[Hit], vecs: np.ndarray) -> None:
        if self.embedder is None:
            raise RuntimeError("no embedder")
        with self._lock:
            for h, v in zip(hits, vecs, strict=True):
                # Keyed by the sig of the text that was embedded; if the item changed while
                # it was being embedded, drop the stale vector (the next pass redoes it).
                sig = _sig(h.title, h.body, h.who)
                row = self._conn.execute("SELECT sig, kind, ts FROM docs WHERE id = ?", (h.id,)).fetchone()
                if row and row[0] == sig:
                    vec = np.asarray(v, dtype=np.float32)
                    self._conn.execute(
                        "INSERT OR REPLACE INTO vecs(id, model, sig, vec) VALUES (?,?,?,?)",
                        (h.id, self.embedder.model, sig, vec.tobytes()),
                    )
                    if self._matrix is not None:
                        if not self._matrix.dims:
                            self._matrix = Matrix(len(vec))
                        if len(vec) == self._matrix.dims:
                            self._matrix.add(h.id, vec, str(row[1]), float(row[2]))

    def embed_pending(self, limit: int = 64) -> int:
        """Embed up to `limit` items that have no current vector yet, newest first."""
        if self.embedder is None:
            return 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, d.kind, d.title, d.body, d.who, d.ts, d.meta FROM docs d "
                "LEFT JOIN vecs v ON v.id = d.id AND v.model = ? AND v.sig = d.sig "
                "WHERE v.id IS NULL ORDER BY d.ts DESC LIMIT ?",
                (self.embedder.model, limit),
            ).fetchall()
        hits = [_hit(r) for r in rows]
        if hits:
            self._store_vectors(hits, self.embedder.embed([_doc_text(h) for h in hits]))
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
        while True:
            await asyncio.sleep(every_s)
            try:
                if idle():
                    n = await loop.run_in_executor(self._pool, self.embed_pending, batch)
                    if n:
                        log.info("index: embedded %d new items ahead of time", n)
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


EMBED_CHARS = 1000  # what gets embedded: subject, who and the opening (long mail embeds slowly)


def _doc_text(h: Hit) -> str:
    return " ".join(p for p in (h.title, h.who, h.body[:EMBED_CHARS]) if p)


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
