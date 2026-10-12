"""Scout's local store and search index: one SQLite file on this Mac.

Everything Scout can read (memories, its own conversations, notes and lists, and
later mail and calendar) is stored here and searchable locally; only the few best
matches ever go to Claude.

Search runs cheap layers first and the expensive one only on what they find:

    filter (kind, date)  ->  full text (SQLite FTS5, BM25)  ->  meaning (vectors)

Vectors are computed lazily: a search embeds just its candidates (the full-text hits
plus the newest items of the kinds asked for) and caches them, and an idle background
pass embeds new items ahead of time. So the vector cache grows with what's actually
searched, nothing is ever unreachable by full text, and nothing leaves the Mac.

The embedder is nomic-embed-text-v1.5 (ONNX on CPU through fastembed), cut to 256
dimensions; it loads on first use and unloads after a while idle. The model name and
dimension are stored with each vector, so changing either re-embeds on demand.
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


class NomicEmbedder:
    """nomic-embed-text-v1.5 through fastembed (ONNX, CPU), Matryoshka-cut to `dims`.
    Loads on first use; `unload_if_idle` frees its ~1.8 GB after `idle_s`."""

    def __init__(
        self,
        name: str = "nomic-ai/nomic-embed-text-v1.5",
        dims: int = 256,
        idle_s: float = 900,
        threads: int = 2,
    ) -> None:
        self.name = name
        self.threads = threads
        self.dims = dims
        self.idle_s = idle_s
        self.model = f"{name}@{dims}"
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
                log.info("embedder loaded in %.1fs", time.perf_counter() - t0)
            self._used = time.monotonic()
            prefix = "search_query: " if query else "search_document: "
            raw = np.array(list(self._m.embed([prefix + t for t in texts], batch_size=16)), dtype=np.float32)
        # Matryoshka: layer-norm, cut, then unit length (nomic's recipe).
        raw = (raw - raw.mean(axis=1, keepdims=True)) / (raw.std(axis=1, keepdims=True) + 1e-6)
        cut = raw[:, : self.dims]
        out: np.ndarray = cut / (np.linalg.norm(cut, axis=1, keepdims=True) + 1e-9)
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


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9']*")
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
    min_similarity: float = 0.5  # a match by meaning alone (no shared words) needs this cosine
    max_new_per_search: int = 32  # items a search may embed itself; the background does the rest
    _conn: sqlite3.Connection = field(init=False, repr=False)
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
        """Add or update one item. Returns False when it was already stored unchanged."""
        sig = _sig(title, body, who)
        with self._lock:
            row = self._conn.execute("SELECT sig FROM docs WHERE id = ?", (id,)).fetchone()
            if row and row[0] == sig:
                return False
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
                    ts if ts is not None else time.time(),
                    json.dumps(meta or {}),
                    sig,
                ),
            )
        return True

    def delete(self, id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE id = ?", (id,))
            self._conn.execute("DELETE FROM vecs WHERE id = ?", (id,))

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
        pool: int = 200,
    ) -> list[Hit]:
        """Best matches for `query`: full text, re-ranked by meaning when an embedder is
        available (the candidates are the text hits plus the `pool` newest items, so a
        question that shares no words with the answer can still find it). semantic=None:
        by meaning only if the model is already loaded (never wait ~25 s for it)."""
        text = self.text_search(query, kinds, since, limit=60)
        if semantic is None:
            semantic = bool(getattr(self.embedder, "loaded", True))
        if not semantic or self.embedder is None:
            return text[:limit]
        cands = {h.id: h for h in text}
        for h in self.recent(kinds, pool, since):
            cands.setdefault(h.id, h)
        if not cands:
            return []
        # Rank by meaning what has a vector, embedding at most a few new ones here (the
        # background pass embeds the rest); text matches without one keep their text rank.
        vecs, hits, unranked = self._vectors(list(cands.values()), self.max_new_per_search)
        q = self.embedder.embed([query], query=True)[0]
        sims = vecs @ q
        # Meaning decides; sharing the query's words adds a little (more for the best text
        # matches). One bar for both, so a shared stray word ("work" in "working") can't
        # carry an unrelated item, and recent-but-unrelated items never match.
        text_rank = {h.id: i for i, h in enumerate(text)}
        ranked = []
        for i, h in enumerate(hits):
            t = text_rank.get(h.id)
            h.similarity = float(sims[i])
            h.score = h.similarity + (TEXT_BONUS / (1 + t) if t is not None else 0.0)
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
                row = self._conn.execute("SELECT sig FROM docs WHERE id = ?", (h.id,)).fetchone()
                if row:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO vecs(id, model, sig, vec) VALUES (?,?,?,?)",
                        (h.id, self.embedder.model, row[0], np.asarray(v, dtype=np.float32).tobytes()),
                    )

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
    try:
        m = json.loads(meta) if meta else {}
    except ValueError:
        m = {}
    return Hit(id, kind, title, body, who, float(ts), m)


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
        limit: int = 8, semantic: bool | None = True, pool: int = 200,
    ) -> list[Hit]: ...  # fmt: skip
    async def asearch(self, query: str, **kw: Any) -> list[Hit]: ...


_index: Store | None = None  # tests put a local Index here


def default() -> Store:
    """The store Scout uses: the scout-memory service."""
    if _index is not None:
        return _index
    from . import memory_client

    return memory_client.Store(memory_client.client())
