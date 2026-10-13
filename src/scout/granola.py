"""Granola meeting notes in Scout's local store, so "what did we decide in the budget
meeting" is searched on this Mac like mail.

Granola's public API (docs.granola.ai; a personal key, Business plan or above, in
`[granola] api_key`): GET /v1/notes lists notes (newest changes found with
updated_after), GET /v1/notes/{id}?include=transcript gives one with its summary and
transcript, and a transcript too large to come inline (413) is read in pages from
/v1/notes/{id}/transcript. Only notes Granola has finished (summary and transcript)
are listed.

Each note is stored as kind "meeting": one item for the summary (id granola:<note>)
and the transcript in chunks of about CHUNK_CHARS (granola:<note>#<n>), each line
"Speaker: text". Granola is the record; this is a searchable copy, refreshed when a
note changes and dropped when it's deleted. The memory service runs a pass every
`every_min` minutes.

A listed note Granola won't give yet (404: not processed) is kept in
state/granola_pending.json with when it was first seen and asked for by id each pass,
so the next pass's `updated_after` can move on past it; after PENDING_DAYS it's left.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import index
from .config import DATA

log = logging.getLogger(__name__)

API = "https://public-api.granola.ai"
KIND = "meeting"
CHUNK_CHARS = 1000  # what embeds well (index.EMBED_CHARS)
PAGE = 30  # notes per list page (the API's maximum)
GAP_S = 0.25  # between request starts: Granola allows 5 a second sustained
WORKERS = 3  # full notes fetched at once (each takes about a second to answer)
TIMEOUT_S = 30.0
MAX_WAIT_S = 60.0  # the longest a 429's Retry-After is honored
PENDING = DATA / "state" / "granola_pending.json"
PENDING_DAYS = 7.0  # a note still unfinished after this long is left alone
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Fetch = Callable[[str, dict[str, Any]], dict[str, Any]]  # (path, query) -> JSON


class GranolaError(Exception):
    pass


def retry_after(value: str | None, now: float | None = None) -> float:
    """Seconds to wait from a Retry-After header (seconds or an HTTP date), 0 to MAX_WAIT_S."""
    if not value:
        return 5.0
    try:
        wait = float(value)
    except ValueError:
        try:
            wait = email.utils.parsedate_to_datetime(value).timestamp() - (
                time.time() if now is None else now
            )
        except (TypeError, ValueError, IndexError):
            wait = 5.0
    return max(0.0, min(wait, MAX_WAIT_S))


def fetcher(api_key: str, base: str = API, gap_s: float = GAP_S) -> Fetch:
    """GETs to Granola's API with the key, spaced out (from any number of threads, one
    start at a time), retrying a 429 after its wait, which holds back every thread's
    next request too."""
    last = [0.0]
    lock = threading.Lock()

    def slot() -> None:
        with lock:  # waited out under the lock, so starts are never closer than gap_s
            wait = last[0] + gap_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()

    def get(path: str, query: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        req = urllib.request.Request(  # noqa: S310 (a fixed https API)
            f"{base}{path}" + (f"?{q}" if q else ""),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "Scout",
            },
        )
        for attempt in range(4):
            slot()
            try:
                with _opener.open(req, timeout=TIMEOUT_S) as r:
                    out: dict[str, Any] = json.loads(r.read() or b"{}")
                    return out
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    resume = time.monotonic() + retry_after(exc.headers.get("Retry-After"))
                    with lock:  # nobody's next request before the wait is over
                        last[0] = max(last[0], resume - gap_s)
                    continue
                raise GranolaError(f"{path}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                raise GranolaError(f"{path}: {exc}") from None
        raise GranolaError(f"{path}: still rate limited")

    return get


def changed_notes(get: Fetch, updated_after: str | None) -> Iterator[dict[str, Any]]:
    """Every listed note changed after `updated_after` (all of them when None)."""
    cursor = None
    while True:
        page = get("/v1/notes", {"page_size": PAGE, "updated_after": updated_after, "cursor": cursor})
        yield from page.get("notes") or []
        cursor = page.get("cursor")
        if not page.get("hasMore") or not cursor:
            return


def full_note(get: Fetch, note_id: str) -> dict[str, Any]:
    """The note with its transcript (read in pages when it's too large to come inline)."""
    path = f"/v1/notes/{urllib.parse.quote(note_id)}"
    try:
        return get(path, {"include": "transcript"})
    except GranolaError as exc:
        if "HTTP 413" not in str(exc):
            raise
    note = get(path, {})
    lines: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = get(f"{path}/transcript", {"page_size": 100, "cursor": cursor})
        lines += page.get("transcript") or []
        cursor = page.get("cursor")
        if not page.get("hasMore") or not cursor:
            break
    note["transcript"] = lines
    return note


def speaker(line: dict[str, Any]) -> str:
    s = line.get("speaker") or {}
    if s.get("name"):
        return str(s["name"])
    if s.get("attribution") == "me" or (not s.get("attribution") and s.get("source") == "microphone"):
        return "Me"
    return str(s.get("diarization_label") or "Them")


def _ts(iso: str | None) -> float:
    if not iso:
        return time.time()
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def documents(note: dict[str, Any]) -> list[dict[str, Any]]:
    """A note as index items: its summary, then its transcript in chunks."""
    nid = str(note["id"])
    title = str(note.get("title") or (note.get("calendar_event") or {}).get("event_title") or "Meeting")
    people = [a.get("name") or a.get("email") for a in note.get("attendees") or [] if isinstance(a, dict)]
    who = ", ".join(p for p in people if p)[:300]
    ev = note.get("calendar_event") or {}
    start = _ts(ev.get("scheduled_start_time") or note.get("created_at"))
    meta = {
        "source": "granola",
        "note": nid,
        "url": note.get("web_url"),
        "updated_at": note.get("updated_at"),
    }
    summary = str(note.get("summary_markdown") or note.get("summary_text") or "")
    docs = [{"id": f"granola:{nid}", "title": title, "body": summary, "who": who, "ts": start, "meta": meta}]
    chunk: list[str] = []
    size, n = 0, 0
    for line in note.get("transcript") or []:
        text = " ".join(str(line.get("text") or "").split())
        if not text:
            continue
        said = f"{speaker(line)}: {text}"
        if chunk and size + len(said) > CHUNK_CHARS:
            n += 1
            docs.append(_chunk(nid, n, title, who, start, meta, chunk))
            chunk, size = [], 0
        chunk.append(said)
        size += len(said) + 1
    if chunk:
        docs.append(_chunk(nid, n + 1, title, who, start, meta, chunk))
    return docs


def _chunk(
    nid: str, n: int, title: str, who: str, ts: float, meta: dict[str, Any], lines: list[str]
) -> dict[str, Any]:
    return {
        "id": f"granola:{nid}#{n}",
        "title": f"{title} (transcript, part {n})",
        "body": "\n".join(lines),
        "who": who,
        "ts": ts,
        "meta": {**meta, "part": n},
    }


def store_note(ix: index.Store, note: dict[str, Any]) -> int:
    """Put a note's items, and drop chunks a shorter new transcript no longer has. Each
    item notes when it was stored (stored_at), so a reader can wait for the whole note."""
    now = time.time()
    docs = documents(note)
    changed = sum(ix.put(kind=KIND, **{**d, "meta": {**d["meta"], "stored_at": now}}) for d in docs)
    n = len(docs) - 1
    while ix.get(f"granola:{note['id']}#{n + 1}") is not None:
        n += 1
        ix.delete(f"granola:{note['id']}#{n}")
        changed += 1
    return changed


def forget_note(ix: index.Store, nid: str) -> int:
    """Drop a deleted note's summary and transcript chunks. Returns how many items."""
    gone = 0
    if ix.get(f"granola:{nid}") is not None:
        ix.delete(f"granola:{nid}")
        gone += 1
    n = 1
    while ix.get(f"granola:{nid}#{n}") is not None:
        ix.delete(f"granola:{nid}#{n}")
        gone += 1
        n += 1
    return gone


def _load_pending(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_pending(path: Path, pending: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pending))
    tmp.replace(path)


def sync_pass(
    ix: index.Store,
    get: Fetch,
    since: str | None,
    pending_path: Path | None = None,
    now: float | None = None,
    workers: int = WORKERS,
) -> tuple[int, int, str | None]:
    """Bring notes changed after `since` (all, when None) into the store, and ask again for
    the ones Granola hadn't finished. Returns (notes, items changed, the newest updated_at
    listed: the next pass's `since`). Full notes are fetched `workers` at a time (the
    fetcher keeps the request rate); they're stored here, in listing order."""
    path = pending_path or PENDING
    now = time.time() if now is None else now
    pending = _load_pending(path)
    notes = changed = 0
    newest = since
    asked: set[str] = set()
    # listing order: (note id, its fetch; None for a deleted note)
    queue: deque[tuple[str, Future[dict[str, Any]] | None]] = deque()

    def settle() -> None:
        nonlocal notes, changed
        nid, fut = queue.popleft()
        if fut is None:
            pending.pop(nid, None)
            changed += forget_note(ix, nid)
            return
        try:
            note = fut.result()
        except GranolaError as exc:
            if "HTTP 404" not in str(exc):
                raise
            first = pending.setdefault(nid, now)  # not processed yet: ask again next pass
            if now - first > PENDING_DAYS * 86400:
                log.warning(
                    "granola: note %s still unavailable after %.0f days; leaving it", nid, PENDING_DAYS
                )
                pending.pop(nid)
            return
        pending.pop(nid, None)
        changed += store_note(ix, note)
        notes += 1

    pool = ThreadPoolExecutor(max(1, workers), thread_name_prefix="granola")

    def fetch(nid: str) -> None:
        asked.add(nid)
        queue.append((nid, pool.submit(full_note, get, nid)))
        while len(queue) > workers:
            settle()

    try:
        for summary in changed_notes(get, since):
            u = summary.get("updated_at")
            if isinstance(u, str) and (newest is None or u > newest):
                newest = u
            nid = str(summary["id"])
            if summary.get("deleted_at"):
                asked.add(nid)
                queue.append((nid, None))
                continue
            fetch(nid)
        for nid in [n for n in pending if n not in asked]:
            fetch(nid)
        while queue:
            settle()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        _save_pending(path, pending)
    return notes, changed, newest
