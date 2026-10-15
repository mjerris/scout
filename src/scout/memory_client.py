"""The client side of scout-memory (memory_service.py): the local store, search and
remembered facts, over HTTP on 127.0.0.1.

`Store` has the same methods as index.Index, and `RemoteMemory` the ones of
memory.Memory that Scout uses, so callers don't care which side of the wire they're
on. Calls are short local HTTP requests; a stopped service is a ToolError that says so.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import index
from .mac import ToolError

DOWN = "Scout's memory service isn't running (it restarts on its own; scripts/launchd.sh status memory)."

TIMEOUT = 5.0  # a down or stuck service fails fast; the voice app waits on these
SLOW_TIMEOUT = 120.0  # syncing many items, or a search by meaning (loading the model takes ~25 s)

# Never through a proxy: HTTP_PROXY doesn't exempt 127.0.0.1, and the token and the
# payloads (facts, mail) must not leave the Mac.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Client:
    def __init__(self, url: str, token_path: Path) -> None:
        self.url = url.rstrip("/")
        self.token_path = token_path
        self._token = ""

    def call(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> dict[str, Any]:
        if not self._token:
            try:
                self._token = self.token_path.read_text().strip()
            except FileNotFoundError:
                raise ToolError(DOWN) from None
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(  # noqa: S310 (self.url is our own http://127.0.0.1 address)
            self.url + path,
            data=data,
            method="GET" if payload is None else "POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with _opener.open(req, timeout=timeout or TIMEOUT) as resp:
                out: dict[str, Any] = json.loads(resp.read() or b"{}")
                return out
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except ValueError:
                err = {}
            if exc.code == 401:
                self._token = ""  # the service made a new token: read it again next time
            if err.get("tool_error"):
                raise ToolError(str(err.get("error"))) from None
            raise ToolError(f"memory service: {err.get('error') or exc}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            raise ToolError(DOWN) from None


def _hit(d: dict[str, Any]) -> index.Hit:
    return index.Hit(**d)


class Store:
    """index.Index's methods, served by scout-memory."""

    def __init__(self, client: Client) -> None:
        self.c = client

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
        args = {
            "id": id,
            "kind": kind,
            "title": title,
            "body": body,
            "who": who,
            "ts": ts,
            "meta": meta or {},
        }
        return bool(self.c.call("/v1/docs/put", args)["changed"])

    def delete(self, id: str) -> None:
        self.c.call("/v1/docs/delete", {"id": id})

    def sync(self, kind: str, items: Iterable[dict[str, Any]]) -> tuple[int, int]:
        out = self.c.call("/v1/docs/sync", {"kind": kind, "items": list(items)}, timeout=SLOW_TIMEOUT)
        return int(out["changed"]), int(out["removed"])

    def get(self, id: str) -> index.Hit | None:
        h = self.c.call("/v1/docs/get", {"id": id})["hit"]
        return _hit(h) if h else None

    def count(self, kind: str | None = None) -> int:
        return int(self.c.call("/v1/docs/count", {"kind": kind})["count"])

    def recent(
        self, kinds: Sequence[str] | None = None, limit: int = 20, since: float | None = None
    ) -> list[index.Hit]:
        out = self.c.call("/v1/docs/recent", {"kinds": list(kinds or []), "limit": limit, "since": since})
        return [_hit(h) for h in out["hits"]]

    def prune(self, kind: str, before: float) -> int:
        return int(self.c.call("/v1/docs/prune", {"kind": kind, "before": before})["removed"])

    def search(
        self,
        query: str,
        kinds: Sequence[str] | None = None,
        since: float | None = None,
        limit: int = 8,
        semantic: bool | None = True,
    ) -> list[index.Hit]:
        args = {
            "query": query,
            "kinds": list(kinds or []),
            "since": since,
            "limit": limit,
            "semantic": semantic,
        }
        # By meaning it may load the model first (seconds); a background batch delays it
        # by one small call to the model at most.
        timeout = None if semantic is False else SLOW_TIMEOUT
        return [_hit(h) for h in self.c.call("/v1/search", args, timeout=timeout)["hits"]]

    async def asearch(self, query: str, **kw: Any) -> list[index.Hit]:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: self.search(query, **kw))

    def note_add(self, title: str, body: str, ts: float | None = None) -> int:
        return int(self.c.call("/v1/notes/add", {"title": title, "body": body, "ts": ts})["id"])

    def note_delete(self, n: int) -> bool:
        return bool(self.c.call("/v1/notes/delete", {"id": n})["deleted"])

    def task_add(self, title: str, fields: dict[str, Any], context: str = "") -> int:
        return int(self.c.call("/v1/tasks/add", {"title": title, "fields": fields, "context": context})["id"])

    def task_update(self, n: int, fields: dict[str, Any], add_context: str = "") -> bool:
        args = {"id": n, "fields": fields, "context": add_context}
        return bool(self.c.call("/v1/tasks/update", args)["updated"])

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
        args = {"ts": ts, "who": who, "q": q, "a": a, "tools": [list(t) for t in tools], "private": private}
        if refs:
            args["refs"] = [str(r) for r in refs]
        return str(self.c.call("/v1/journal/add", args)["file"])

    def resolve(self, id: str) -> dict[str, Any] | None:
        """index.Index.resolve: the record, where it opens and what it's linked to; None
        when there's no such record."""
        try:
            out = self.c.call("/v1/resolve?" + urllib.parse.urlencode({"id": id}), None)
        except ToolError as exc:
            if "404" in str(exc):
                return None
            raise
        return out or None

    def link(self, src: str, dst: str, rel: str) -> None:
        self.c.call("/v1/links/add", {"src": src, "dst": dst, "rel": rel})

    def links(self, id: str) -> list[dict[str, Any]]:
        return list(self.c.call("/v1/links/list", {"id": id})["links"])

    def open_url(self, id: str) -> str | None:
        url = self.c.call("/v1/links/url", {"id": id}).get("url")
        return str(url) if url else None

    def missing(self, kind: str, key: str, limit: int = 20) -> list[index.Hit]:
        out = self.c.call("/v1/docs/missing", {"kind": kind, "key": key, "limit": limit})
        return [_hit(h) for h in out["hits"]]

    def health(self) -> dict[str, Any]:
        return self.c.call("/v1/health", None, timeout=5)


class RemoteMemory:
    """memory.Memory's methods (the ones Scout uses), served by scout-memory."""

    def __init__(self, client: Client) -> None:
        self.c = client

    def add(self, text: str) -> str:
        return str(self.c.call("/v1/memories/add", {"text": text})["fact"])

    def forget(self, about: str) -> list[str]:
        return list(self.c.call("/v1/memories/forget", {"about": about})["facts"])

    def matching(self, about: str) -> list[str]:
        return list(self.c.call("/v1/memories/matching", {"about": about})["facts"])

    def relevant(self, request: str, limit: int = 5) -> list[str]:
        """Facts sharing words with the request, then (with the model warm) close in meaning."""
        return list(self.c.call("/v1/memories/relevant", {"request": request, "limit": limit})["facts"])

    def newest(self, limit: int = 10) -> list[str]:
        return list(self.c.call("/v1/memories/newest", {"limit": limit})["facts"])

    @property
    def facts(self) -> list[dict[str, Any]]:
        return list(self.c.call("/v1/memories/list", {})["facts"])


_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        from .config import load
        from .memory_service import TOKEN

        cfg = load().memory_service
        _client = Client(f"http://{cfg.host}:{cfg.port}", TOKEN)
    return _client


_memory: Any = None  # tests put a local memory.Memory here


def memory() -> Any:
    """The remembered facts Scout uses: RemoteMemory, or a test's local Memory."""
    return _memory if _memory is not None else RemoteMemory(client())
