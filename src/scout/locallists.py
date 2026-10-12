"""Scout's own lists and tasks, kept in its local store: the fallback when the Mac's
Reminders can't be used (not a Mac, access not allowed, or `[lists] backend = "scout"`).

Same shapes as reminders_mac, so every caller (local answers, the shared tools) works
the same either way. Items are indexed for local search while open."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from . import appdb, index
from .calendar_mac import iso, parse_time, text_arg
from .mac import ToolError

DEFAULT = "Reminders"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (name TEXT PRIMARY KEY COLLATE NOCASE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list TEXT NOT NULL COLLATE NOCASE,
    title TEXT NOT NULL,
    due TEXT,
    notes TEXT NOT NULL DEFAULT '',
    completed INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    done_at REAL
);
"""


def _ix() -> appdb.AppDB:
    db = appdb.default()
    db.script(_SCHEMA)
    return db


def _names(ix: appdb.AppDB) -> list[str]:
    names = [str(r[0]) for r in ix.sql("SELECT name FROM lists ORDER BY created")]
    return names or [DEFAULT]


def _resolve(ix: appdb.AppDB, name: str, create: bool) -> str:
    """A list by name, case-insensitively ("shopping" finds "Shopping"); a missing one is
    created on add and an error on read."""
    for n in _names(ix):
        if n.lower() == name.lower():
            return n
    if not create:
        raise ToolError(f"There's no {name} list. Lists: {', '.join(_names(ix))}.")
    ix.sql("INSERT OR IGNORE INTO lists(name, created) VALUES (?, ?)", (name, time.time()))
    return name


async def list_names() -> list[str]:
    return _names(_ix())


async def lists() -> str:
    return "\n".join(f"{n} (Scout)" + (", default" if i == 0 else "") for i, n in enumerate(_names(_ix())))


def _item(row: tuple[Any, ...]) -> dict[str, Any]:
    id, lst, title, due, notes, completed = row
    return {
        "id": f"scout:{id}",
        "list": lst,
        "title": title,
        "due": due,
        "notes": notes,
        "completed": bool(completed),
    }


async def reminder_data(
    list_name: Any = None, include_completed: Any = False, due_before: Any = None
) -> dict[str, Any]:
    ix = _ix()
    name = text_arg(list_name, "list", 200)
    lst = _resolve(ix, name, create=False) if name else None
    rows = ix.sql(
        "SELECT id, list, title, due, notes, completed FROM list_items "
        "WHERE (? IS NULL OR list = ?) AND (? OR completed = 0) ORDER BY due IS NULL, due, created",
        (lst, lst, bool(include_completed)),
    )
    items = [_item(r) for r in rows]
    if due_before:
        cut = parse_time(due_before, "due_before")
        items = [r for r in items if r["due"] and _due(r["due"]) < cut]
    return {"reminders": items[:100], "total": len(items)}


def _due(due: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(due)
    return t if t.tzinfo else t.astimezone()


async def add(a: dict[str, Any]) -> str:
    ix = _ix()
    title = text_arg(a.get("title"), "title", 200)
    if not title:
        raise ToolError("title is required")
    lst = _resolve(ix, text_arg(a.get("list"), "list", 200) or _names(ix)[0], create=True)
    due = text_arg(a.get("due"), "due", 40) or None
    if due:
        t = parse_time(due, "due")
        due = due if "T" not in due and " " not in due else iso(t)
    notes = text_arg(a.get("notes"), "notes", 2000)
    now = time.time()
    ix.sql(
        "INSERT INTO list_items(list, title, due, notes, created) VALUES (?,?,?,?,?)",
        (lst, title, due, notes, now),
    )
    rid = ix.last_rowid
    index.default().put(
        f"list:{rid}",
        "list",
        title=title,
        body=f"{title}\n{notes}".strip(),
        who=lst,
        ts=now,
        meta={"due": due},
    )
    return f"Added {title} to {lst}" + (f", due {due}." if due else ".")


async def complete(a: dict[str, Any]) -> str:
    ix = _ix()
    rid = text_arg(a.get("id"), "id", 200).removeprefix("scout:")
    title = text_arg(a.get("title"), "title", 200)
    if not rid.isdigit() or not title:
        raise ToolError("id and title are required (both from reminders_list)")
    rows = ix.sql("SELECT list, title FROM list_items WHERE id = ? AND completed = 0", (int(rid),))
    if not rows or rows[0][1].lower() != title.lower():
        raise ToolError("that item isn't open on any list (list them again)")
    ix.sql("UPDATE list_items SET completed = 1, done_at = ? WHERE id = ?", (time.time(), int(rid)))
    index.default().delete(f"list:{rid}")
    return f"Marked {rows[0][1]} done in {rows[0][0]}."
