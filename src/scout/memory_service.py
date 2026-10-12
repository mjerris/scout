"""scout-memory: Scout's memory and search as a separate local service.

It owns the local store (state/memory.db: documents, vectors, the full-text index),
the embedding model, the background embedding, and the user's remembered facts.
Scout, its tools and other local programs use it over HTTP on 127.0.0.1 with a bearer
token (state/memory.token, readable by this user only):

    GET  /v1/health
    POST /v1/docs/put {id, kind, title, body, who, ts, meta}  -> {changed}
    POST /v1/docs/delete {id} | /v1/docs/get {id} | /v1/docs/count {kind}
    POST /v1/docs/sync {kind, items}                     -> {changed, removed}
    POST /v1/docs/recent {kinds, limit, since}           -> {hits}
    POST /v1/docs/prune {kind, before}                   -> {removed}
    POST /v1/search {query, kinds, since, limit, semantic} -> {hits}
    POST /v1/memories/add {text} | /forget {about} | /matching {about}
    POST /v1/memories/relevant {request, limit} | /newest {limit} | /list

It runs under its own login item (com.local.scout.memory) at low CPU priority, so
embedding never competes with listening, and it keeps running while the voice app
restarts. memory_client.py is the other side.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from . import index
from .config import DATA, load
from .memory import Memory

log = logging.getLogger("scout.memory_service")

DB = DATA / "state" / "memory.db"
TOKEN = DATA / "state" / "memory.token"
OLD_DB = DATA / "state" / "scout.db"  # where the store lived inside the app before


def token(path: Path = TOKEN) -> str:
    """The service's bearer token, created on first use (mode 0600)."""
    try:
        t = path.read_text().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


def migrate(ix: index.Index, old: Path = OLD_DB) -> int:
    """Once: bring documents and vectors over from the app's old store."""
    if ix.count() or not old.exists():
        return 0
    src = sqlite3.connect(old)
    try:
        if not src.execute("SELECT name FROM sqlite_master WHERE name = 'docs'").fetchone():
            return 0
        rows = src.execute("SELECT id, kind, title, body, who, ts, meta FROM docs").fetchall()
        for id, kind, title, body, who, ts, meta in rows:
            ix.put(id, kind, title, body, who, ts, index._hit((id, kind, title, body, who, ts, meta)).meta)
        src.execute("DROP TABLE IF EXISTS docs_fts")
        src.execute("DROP TABLE IF EXISTS docs")
        src.execute("DROP TABLE IF EXISTS vecs")
        src.commit()
        log.info("moved %d documents from the app's store", len(rows))
        return len(rows)
    finally:
        src.close()


def _hit(h: index.Hit) -> dict[str, Any]:
    return dataclasses.asdict(h)


def _kinds(v: Any) -> list[str] | None:
    return [str(k) for k in v] if isinstance(v, list) and v else None


class Service:
    def __init__(self, ix: index.Index, memory: Memory, secret: str) -> None:
        self.ix = ix
        self.memory = memory
        self.secret = secret
        memory.on_change = self._index_memories
        self._index_memories()

    def _index_memories(self) -> None:
        self.ix.sync("memory", self.memory.as_documents())

    def _delete(self, id: str) -> str:
        self.ix.delete(id)
        return id

    def relevant(self, request: str, limit: int) -> list[str]:
        """Remembered facts for a request: shared words first, then close in meaning."""
        out = self.memory.relevant(request, limit)
        if len(out) < limit and request.strip():
            for h in self.ix.search(request, kinds=["memory"], limit=limit, semantic=None):
                if h.title not in out:
                    out.append(h.title)
        return out[:limit]

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth], client_max_size=8 * 1024 * 1024)
        r = app.router
        r.add_get("/v1/health", self._health)
        routes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "/v1/docs/put": lambda a: {"changed": self.ix.put(
                str(a["id"]), str(a["kind"]), str(a.get("title") or ""), str(a.get("body") or ""),
                str(a.get("who") or ""), a.get("ts"), a.get("meta") or {})},
            "/v1/docs/delete": lambda a: {"deleted": self._delete(str(a["id"]))},
            "/v1/docs/get": lambda a: {"hit": _hit(h) if (h := self.ix.get(str(a["id"]))) else None},
            "/v1/docs/count": lambda a: {"count": self.ix.count(a.get("kind"))},
            "/v1/docs/sync": lambda a: dict(zip(("changed", "removed"), self.ix.sync(str(a["kind"]), a.get("items") or []), strict=True)),
            "/v1/docs/recent": lambda a: {"hits": [_hit(h) for h in self.ix.recent(_kinds(a.get("kinds")), int(a.get("limit") or 20), a.get("since"))]},
            "/v1/docs/prune": lambda a: {"removed": self.ix.prune(str(a["kind"]), float(a["before"]))},
            "/v1/search": lambda a: {"hits": [_hit(h) for h in self.ix.search(
                str(a.get("query") or ""), kinds=_kinds(a.get("kinds")), since=a.get("since"),
                limit=int(a.get("limit") or 8), semantic=a.get("semantic", None))]},
            "/v1/memories/add": lambda a: {"fact": self.memory.add(str(a.get("text") or ""))},
            "/v1/memories/forget": lambda a: {"facts": self.memory.forget(str(a.get("about") or ""))},
            "/v1/memories/matching": lambda a: {"facts": self.memory.matching(str(a.get("about") or ""))},
            "/v1/memories/relevant": lambda a: {"facts": self.relevant(str(a.get("request") or ""), int(a.get("limit") or 5))},
            "/v1/memories/newest": lambda a: {"facts": self.memory.newest(int(a.get("limit") or 10))},
            "/v1/memories/list": lambda a: {"facts": list(self.memory.facts)},
        }  # fmt: skip
        for path, fn in routes.items():
            r.add_post(path, self._handler(fn))
        return app

    @web.middleware
    async def _auth(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {self.secret}"):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        emb = self.ix.embedder
        return web.json_response({
            "ok": True, "docs": self.ix.count(), "memories": len(self.memory.facts),
            "model": getattr(emb, "model", None), "model_loaded": bool(getattr(emb, "loaded", False)),
        })  # fmt: skip

    def _handler(
        self, fn: Callable[[dict[str, Any]], Any]
    ) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def handle(request: web.Request) -> web.Response:
            from .mac import ToolError

            try:
                args = await request.json()
                if not isinstance(args, dict):
                    raise ValueError("a JSON object is expected")
            except ValueError as exc:
                return web.json_response({"error": f"bad request: {exc}"}, status=400)
            try:
                out = await asyncio.get_running_loop().run_in_executor(None, fn, args)
            except ToolError as exc:  # the caller's mistake, said plainly ("nothing to remember")
                return web.json_response({"error": str(exc), "tool_error": True}, status=422)
            except (KeyError, TypeError, ValueError) as exc:
                return web.json_response({"error": f"bad request: {exc!r}"}, status=400)
            return web.json_response(out if out is not None else {})

        return handle


async def serve(host: str, port: int, db: Path = DB) -> None:
    cfg = load().index
    emb = index.NomicEmbedder(cfg.model, cfg.dims, cfg.unload_after_s) if cfg.semantic else None
    ix = index.Index(db, emb, cfg.min_similarity)
    migrate(ix)
    svc = Service(ix, Memory(DATA / "state" / "memory.json"), token())
    runner = web.AppRunner(svc.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("scout-memory listening on %s:%d (%d documents)", host, port, ix.count())
    if cfg.semantic and cfg.background_embed:
        await ix.background(lambda: True)
    else:
        await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    with contextlib.suppress(OSError):
        os.nice(10)  # embedding yields the CPU to listening and speaking
    cfg = load().memory_service
    t0 = time.monotonic()
    log.info("starting scout-memory")
    try:
        asyncio.run(serve(cfg.host, cfg.port))
    finally:
        log.info("scout-memory stopped after %.0f s", time.monotonic() - t0)


if __name__ == "__main__":
    main()
