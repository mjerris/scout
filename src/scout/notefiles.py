"""Scout's notes as markdown files, one per note, that you can read and edit:

    memory/notes/0003-generator-oil-change.md

    # Generator oil change
    _2026-10-13 11:28_

    Changed the Onan's oil at 412 hours; next at 562.

The number is the note's id ("delete note 3"). The index (kind "note", id note:<n>) is
built from the files and follows edits; only the memory service writes here.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^(\d+)-[^/]*\.md$")
_DATE = re.compile(r"^_(\d{4}-\d\d-\d\d \d\d:\d\d)_\s*$")
_lock = threading.Lock()


def notes_dir(root: Path) -> Path:
    return root / "notes"


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:50].rstrip("-") or "note"


def files(root: Path) -> list[Path]:
    d = notes_dir(root)
    return sorted(p for p in d.glob("*.md") if _NAME.match(p.name)) if d.exists() else []


def number(p: Path) -> int | None:
    m = _NAME.match(p.name)
    return int(m.group(1)) if m else None


def find(root: Path, n: int) -> Path | None:
    return next((p for p in files(root) if number(p) == n), None)


def write(root: Path, title: str, body: str, ts: float) -> Path:
    """A new note file, numbered one past the highest so far."""
    with _lock:
        d = notes_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        n = max((number(p) or 0 for p in files(root)), default=0) + 1
        p = d / f"{n:04d}-{slug(title)}.md"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        p.write_text(f"# {' '.join(title.split())}\n_{when}_\n\n{body.strip()}\n")
        return p


def parse(p: Path) -> list[dict[str, Any]]:
    """The note in a file as an index item (none if the file isn't a note)."""
    n = number(p)
    if n is None or not p.exists():
        return []
    lines = p.read_text().splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines.pop(0)[2:].strip()
    ts = p.stat().st_mtime
    if lines and (m := _DATE.match(lines[0].strip())):
        lines.pop(0)
        with contextlib.suppress(ValueError):
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M"))
    body = "\n".join(lines).strip()
    return [{"id": f"note:{n}", "title": title or body.split("\n", 1)[0][:80], "body": body, "ts": ts}]


def id_prefix(p: Path) -> str | None:
    n = number(p)
    return f"note:{n}" if n is not None else None
