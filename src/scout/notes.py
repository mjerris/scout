"""Notes: markdown files in Scout's data folder (memory/notes/, notefiles.py), searchable
by words and meaning, with a copy in an Apple Notes folder when `[notes] apple = true`
and Notes automation is allowed (one macOS prompt), so they're on the phone too. Without
that access, or off a Mac, notes simply stay in Scout."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any

from . import appdb, index, mac
from .config import load
from .mac import ToolError

log = logging.getLogger(__name__)

# Which Apple Notes note is the copy of which of Scout's (the files are the notes).
_SCHEMA = "CREATE TABLE IF NOT EXISTS note_apple (note INTEGER PRIMARY KEY, apple_id TEXT NOT NULL);"
MAX_CHARS = 5000

_APPLE_ADD = """
on run argv
    set folderName to item 1 of argv
    tell application "Notes"
        if not (exists folder folderName) then make new folder with properties {name:folderName}
        set n to make new note at folder folderName with properties {name:item 2 of argv, body:item 3 of argv}
        return id of n
    end tell
end run
"""
_APPLE_DELETE = """
on run argv
    tell application "Notes" to delete note id (item 1 of argv)
end run
"""
_apple_off_until = 0.0  # after Notes refuses, stop trying for a while


def _ix() -> appdb.AppDB:
    db = appdb.default()
    db.script(_SCHEMA)
    return db


def _title(text: str) -> str:
    first = text.strip().split("\n", 1)[0]
    words = first.split()
    return " ".join(words[:8]) + ("…" if len(words) > 8 else "")


async def _to_apple(title: str, body: str) -> str | None:
    global _apple_off_until
    cfg = load().notes
    if not cfg.apple or time.monotonic() < _apple_off_until:
        return None
    rich = (
        "<div><b>"
        + html.escape(title)
        + "</b></div>"
        + "".join(f"<div>{html.escape(line) or '<br>'}</div>" for line in body.split("\n"))
    )
    try:
        # 120 s: the first time, macOS asks whether Scout may control Notes.
        return await mac._run(
            "/usr/bin/osascript", "-e", _APPLE_ADD, "--", cfg.apple_folder, title, rich, timeout=120
        )
    except (ToolError, OSError) as exc:
        log.info("Apple Notes copy skipped (%s); notes stay in Scout", exc)
        _apple_off_until = time.monotonic() + 3600
        return None


async def add(text: Any, title: Any = None) -> dict[str, Any]:
    body = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not body:
        raise ToolError("nothing to note")
    if len(body) > MAX_CHARS:
        raise ToolError(f"that note is too long (max {MAX_CHARS} characters)")
    if any(ord(c) < 32 and c != "\n" for c in body):
        raise ToolError("that note has control characters")
    name = " ".join(str(title or "").split())[:120] or _title(body)
    nid = await asyncio.to_thread(index.default().note_add, name, body)
    apple = await _to_apple(name, body)
    if apple:
        _ix().sql("INSERT OR REPLACE INTO note_apple(note, apple_id) VALUES (?, ?)", (nid, apple))
    return {"id": nid, "title": name, "apple": bool(apple)}


async def add_text(a: dict[str, Any]) -> str:
    n = await add(a.get("text"), a.get("title"))
    return f"Saved note {n['id']}: {n['title']}" + (" (also in Apple Notes)." if n["apple"] else ".")


async def find(query: Any, limit: int = 5) -> list[dict[str, Any]]:
    q = str(query or "").strip()
    store = index.default()
    hits = (
        await store.asearch(q, kinds=["note"], limit=limit, semantic=None)
        if q
        else await asyncio.to_thread(store.recent, ["note"], limit)
    )
    return [
        {"id": int(h.id.split(":", 1)[1]), "title": h.title, "body": h.body, "created": h.ts} for h in hits
    ]


async def find_text(a: dict[str, Any]) -> str:
    found = await find(a.get("query"), max(1, min(int(a.get("limit") or 5), 20)))
    if not found:
        return "No notes found."
    return "\n\n".join(
        f"Note {n['id']} ({time.strftime('%Y-%m-%d', time.localtime(n['created']))}): {n['title']}\n{n['body']}"
        for n in found
    )


async def delete(a: dict[str, Any]) -> str:
    try:
        nid = int(a.get("id") or 0)
    except (TypeError, ValueError):
        raise ToolError("id must be a note number from notes_find") from None
    store = index.default()
    h = await asyncio.to_thread(store.get, f"note:{nid}")
    if h is None or not await asyncio.to_thread(store.note_delete, nid):
        raise ToolError(f"there's no note {nid}")
    ix = _ix()
    rows = ix.sql("SELECT apple_id FROM note_apple WHERE note = ?", (nid,))
    if rows:
        ix.sql("DELETE FROM note_apple WHERE note = ?", (nid,))
        try:
            await mac._run("/usr/bin/osascript", "-e", _APPLE_DELETE, "--", rows[0][0], timeout=30)
        except (ToolError, OSError) as exc:
            log.info("Apple Notes copy of note %d not deleted: %s", nid, exc)
    return f"Deleted note {nid}: {h.title}."


def speak(found: list[dict[str, Any]]) -> str:
    """A note read aloud (the best match), or that there's none."""
    if not found:
        return "I don't have a note about that."
    n = found[0]
    more = f" I have {len(found) - 1} more that might match." if len(found) > 1 else ""
    body = n["body"] if len(n["body"]) <= 400 else n["body"][:400].rsplit(" ", 1)[0] + ", and more."
    return f"Your note: {body}{more}"
