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
note changes. The memory service runs a pass every `every_min` minutes.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from . import index

log = logging.getLogger(__name__)

API = "https://public-api.granola.ai"
KIND = "meeting"
CHUNK_CHARS = 1000  # what embeds well (index.EMBED_CHARS)
PAGE = 30  # notes per list page (the API's maximum)
GAP_S = 0.25  # between requests: Granola allows 5 a second sustained
TIMEOUT_S = 30.0
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Fetch = Callable[[str, dict[str, Any]], dict[str, Any]]  # (path, query) -> JSON


class GranolaError(Exception):
    pass


def fetcher(api_key: str, base: str = API, gap_s: float = GAP_S) -> Fetch:
    """GETs to Granola's API with the key, spaced out, retrying a 429 after its wait."""
    last = [0.0]

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
            wait = last[0] + gap_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            try:
                with _opener.open(req, timeout=TIMEOUT_S) as r:
                    out: dict[str, Any] = json.loads(r.read() or b"{}")
                    return out
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(float(exc.headers.get("Retry-After") or 5))
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
    """Put a note's items, and drop chunks a shorter new transcript no longer has."""
    docs = documents(note)
    changed = sum(ix.put(kind=KIND, **d) for d in docs)
    n = len(docs) - 1
    while ix.get(f"granola:{note['id']}#{n + 1}") is not None:
        n += 1
        ix.delete(f"granola:{note['id']}#{n}")
        changed += 1
    return changed


def sync_pass(ix: index.Store, get: Fetch, since: str | None) -> tuple[int, int, str | None]:
    """Bring notes changed after `since` (all, when None) into the store. Returns (notes,
    items changed, the newest updated_at seen: the next pass's `since`)."""
    notes = changed = 0
    newest = since
    waiting: list[str] = []  # updated_at of notes Granola hasn't finished yet
    for summary in changed_notes(get, since):
        u = summary.get("updated_at")
        if isinstance(u, str) and (newest is None or u > newest):
            newest = u
        if summary.get("deleted_at"):
            continue
        try:
            note = full_note(get, str(summary["id"]))
        except GranolaError as exc:
            if "HTTP 404" in str(exc):  # not processed yet: ask again next pass
                if isinstance(u, str):
                    waiting.append(u)
                continue
            raise
        changed += store_note(ix, note)
        notes += 1
    if waiting:  # resume just before the oldest unfinished note, so it isn't skipped
        before = (_ts(min(waiting)) - 1) if newest is None or min(waiting) <= newest else None
        if before is not None:
            newest = dt.datetime.fromtimestamp(before, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return notes, changed, newest
