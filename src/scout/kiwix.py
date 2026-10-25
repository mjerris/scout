"""kiwix-serve: the program that reads the offline reference's ZIM archives and serves
them over HTTP on 127.0.0.1 (docs/reference.md). It runs as its own process so that
libzim, which is GPL-3.0-or-later, stays out of Scout's.

    Client(port)          -> the catalog, full-text search, title suggestions, raw entries
    Server(files, port)   -> the app's kiwix-serve child: started, restarted when the
                             archives change or it dies, stopped with Scout

The client is plain blocking HTTP (urllib), called from threads like the libzim code it
replaced; any of Scout's processes can use it. Only the app runs the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA

log = logging.getLogger(__name__)

VERSION = "3.8.2"  # scripts/install-kiwix.sh installs this one
HOST = "127.0.0.1"
TIMEOUT_S = 10.0
READY_S = 20.0  # a start that doesn't answer within this has failed
CHECK_S = 10.0  # how often the app looks at the child and the archives
RETRY_S = (10.0, 30.0, 60.0, 300.0)  # waits before restarting one that keeps failing
_ATOM = "{http://www.w3.org/2005/Atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


def program() -> Path:
    """Where scripts/install-kiwix.sh puts kiwix-serve: Scout's own bin folder."""
    return DATA / "bin" / "kiwix-serve"


class Unavailable(Exception):
    """kiwix-serve didn't answer (not running, starting, or gone)."""


@dataclass(frozen=True)
class Entry:
    """A book in kiwix-serve's catalog."""

    id: str  # its name in kiwix-serve's URLs: the file name without .zim
    title: str
    date: str  # 2026-06-01
    articles: int
    fulltext: bool
    language: str = ""  # eng, or a list for a multi-language book: eng,spa,fra


@dataclass(frozen=True)
class Hit:
    """One result of /search: an article of one book."""

    book: str  # the book's id
    path: str
    title: str


@dataclass(frozen=True)
class Raw:
    data: bytes
    mimetype: str
    redirect: str = ""  # the path this entry redirects to


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect comes back as an HTTPError with its Location


class Client:
    """kiwix-serve's HTTP API, as Scout uses it."""

    def __init__(self, port: int, host: str = HOST) -> None:
        self.base = f"http://{host}:{port}"
        self._open = urllib.request.build_opener(_NoRedirect()).open

    def _get(self, path: str, query: dict[str, Any] | None = None) -> tuple[int, dict[str, str], bytes]:
        url = self.base + path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
        try:
            with self._open(url, timeout=TIMEOUT_S) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                return exc.code, dict(exc.headers), exc.read()
        except (OSError, ValueError) as exc:  # refused, reset, timed out
            raise Unavailable(f"kiwix-serve isn't answering on {self.base} ({exc})") from None

    def catalog(self) -> list[Entry]:
        status, _, body = self._get("/catalog/v2/entries", {"count": -1})
        if status != 200:
            raise Unavailable(f"kiwix-serve's catalog answered {status}")
        feed = ET.fromstring(body)  # noqa: S314  (our own kiwix-serve's feed, on 127.0.0.1)
        out = []
        for e in feed.iter(f"{_ATOM}entry"):
            href = next(
                (ln.get("href", "") for ln in e.iter(f"{_ATOM}link") if ln.get("type") == "text/html"), ""
            )
            tags = (e.findtext(f"{_ATOM}tags") or "").split(";")
            out.append(
                Entry(
                    id=urllib.parse.unquote(href.rsplit("/", 1)[-1]),
                    title=(e.findtext(f"{_ATOM}title") or "").strip(),
                    date=(e.findtext(f"{_ATOM}updated") or "")[:10],
                    articles=int(e.findtext(f"{_ATOM}articleCount") or 0),
                    fulltext="_ftindex:yes" in tags,
                    language=(e.findtext(f"{_ATOM}language") or "").strip(),
                )
            )
        return out

    def search(self, books: str | Sequence[str], words: Sequence[str], limit: int) -> tuple[int, list[Hit]]:
        """(how many articles have all the words, the first `limit` of them), by the
        archives' own full-text (Xapian) indexes. Several books are searched in one
        request: Xapian merges them with shared statistics, so the order is one ranking
        across all of them. kiwix-serve refuses books in different languages together
        (HTTP 400), so `books` must share one (Entry.language)."""
        ids = [books] if isinstance(books, str) else list(books)
        status, _, body = self._get(
            "/search",
            {"books.name": ids, "pattern": " ".join(words), "pageLength": limit, "format": "xml"},
        )
        if status != 200:
            log.info("kiwix: search in %d books answered %d", len(ids), status)
            return 0, []
        rss = ET.fromstring(body)  # noqa: S314  (our own kiwix-serve's results)
        # A count of thousands comes with separators: "2,000".
        total = int(re.sub(r"\D", "", rss.findtext(f"channel/{_OPENSEARCH}totalResults") or "") or 0)
        hits = []
        for item in rss.iter("item"):
            book, _, path = (item.findtext("link") or "").removeprefix("/content/").partition("/")
            if book in ids and path:
                hits.append(Hit(book, urllib.parse.unquote(path), (item.findtext("title") or "").strip()))
        return total, hits[:limit]

    def suggest(self, book: str, term: str, count: int) -> list[tuple[str, str]]:
        """Articles whose titles match `term`, as (path, title), by the title index."""
        status, _, body = self._get("/suggest", {"content": book, "term": term, "count": count})
        if status != 200:
            return []
        try:
            got = json.loads(body)
        except ValueError:
            return []
        # kiwix-serve escapes the path and title as HTML inside the JSON ("&amp;").
        return [
            (html.unescape(str(s["path"])), html.unescape(str(s.get("value") or s["path"])))
            for s in got
            if isinstance(s, dict) and s.get("kind") == "path" and s.get("path")
        ][:count]

    def raw(self, book: str, path: str) -> Raw:
        """An entry as stored (HTML, an image...), or where it redirects. KeyError when
        there's no such book or entry."""
        status, headers, body = self._get(f"/raw/{book}/content/{urllib.parse.quote(path, safe='/')}")
        if status in (301, 302, 307, 308):
            where = urllib.parse.urlsplit(headers.get("Location", "")).path
            for prefix in (f"/raw/{book}/content/", f"/content/{book}/"):
                if where.startswith(prefix):
                    return Raw(b"", "", urllib.parse.unquote(where[len(prefix) :]))
            raise KeyError(path)
        if status == 404:
            raise KeyError(path)
        if status != 200:
            raise Unavailable(f"kiwix-serve answered {status} for {book}/{path}")
        return Raw(body, headers.get("Content-Type", "").split(";")[0].strip())


# --- the child process (the app only) ------------------------------------------------------


class Server:
    """The app's kiwix-serve, serving `files()` (the archives the reference uses) on
    127.0.0.1:`port`: started when there are archives and the program is installed,
    restarted when the set of archives changes or it exits, stopped with Scout. It is
    started with -a <Scout's pid> too, so it exits by itself if Scout is killed."""

    def __init__(
        self,
        files: Callable[[], list[Path]],
        port: int,
        exe: Path | None = None,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        self.files, self.port, self.exe = files, port, exe or program()
        self.on_change = on_change  # called after every start or stop (the books changed)
        self.client = Client(port)
        self._proc: asyncio.subprocess.Process | None = None
        self._serving: list[Path] = []
        self._failures = 0
        self._retry_at = 0.0
        self.state = "not started"
        self.version = ""

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None and self._proc.returncode is None else None

    def status(self) -> dict[str, Any]:
        """For voice_status: whether it runs, where, and how many archives it serves."""
        return {
            "kiwix_serve": self.state,
            "version": self.version or None,
            "address": f"{HOST}:{self.port}",
            "pid": self.pid,
            "archives": len(self._serving) if self.pid else 0,
        }

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("kiwix: checking kiwix-serve failed")
            await asyncio.sleep(CHECK_S)

    async def check(self) -> None:
        """Start, restart or stop it to match the archives there are now."""
        files = await asyncio.to_thread(self.files)
        if not files:
            if self.pid:
                log.info("kiwix: no archives any more; stopping kiwix-serve")
            await self.stop()
            self.state = "no archives"
            return
        if not self.exe.is_file() or not os.access(self.exe, os.X_OK):
            await self.stop()
            if self.state != "not installed":
                log.warning("kiwix: %s isn't installed (scripts/install-kiwix.sh)", self.exe)
            self.state = "not installed"
            return
        if self.pid and files == self._serving:
            return
        if self.pid:
            log.info("kiwix: the archives changed (%d now); restarting kiwix-serve", len(files))
        elif self._proc is not None:  # it was running and exited by itself
            self._failures += 1
            self._retry_at = time.monotonic() + RETRY_S[min(self._failures, len(RETRY_S)) - 1]
            log.warning(
                "kiwix: kiwix-serve exited (code %s); see logs/kiwix-serve.log", self._proc.returncode
            )
            self._proc = None
            self._serving = []
            self.state = "failed"
            self.on_change()
        if time.monotonic() < self._retry_at:
            return
        await self.stop()
        await self._start(files)

    async def _start(self, files: list[Path]) -> None:
        (DATA / "logs").mkdir(parents=True, exist_ok=True)
        args = [
            "--address", HOST, "--port", str(self.port),
            "--attachToProcess", str(os.getpid()),  # it exits if Scout is killed
            "--skipInvalid",  # a damaged archive is left out, not the whole server
        ]  # fmt: skip
        with (DATA / "logs" / "kiwix-serve.log").open("ab") as out:
            self._proc = await asyncio.create_subprocess_exec(
                str(self.exe), *args, *map(str, files),
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out,
            )  # fmt: skip
        self.state = "starting"
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline and self.pid:
            try:
                served = await asyncio.to_thread(self.client.catalog)
            except Unavailable:
                await asyncio.sleep(0.2)
                continue
            self._serving, self._failures, self._retry_at = files, 0, 0.0
            self.state = "running"
            if not self.version:
                self.version = await self._version()
            log.info(
                "kiwix: kiwix-serve %s up on %s:%d (pid %s) with %d of %d archives",
                self.version or "?", HOST, self.port, self.pid, len(served), len(files),
            )  # fmt: skip
            self.on_change()
            return
        code = self._proc.returncode if self._proc is not None else None
        await self.stop()
        self._failures += 1
        self._retry_at = time.monotonic() + RETRY_S[min(self._failures, len(RETRY_S)) - 1]
        self.state = "failed"
        log.warning(
            "kiwix: kiwix-serve didn't start on %s:%d (%s); see logs/kiwix-serve.log",
            HOST, self.port, f"exit code {code}" if code is not None else "no answer",
        )  # fmt: skip

    async def _version(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.exe), "--version",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
        except (OSError, TimeoutError):
            return ""
        first = out.decode("utf-8", "replace").strip().splitlines()[:1]
        return first[0].removeprefix("kiwix-tools").strip() if first else ""

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        serving, self._serving = self._serving, []
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        self.state = "stopped"
        if serving:
            self.on_change()
