"""Scout's notes as markdown files, one per note, that you can read and edit:

    memory/notes/0003-generator-oil-change.md

    # Generator oil change
    _2026-10-13 11:28_

    Changed the Onan's oil at 412 hours; next at 562.

The number is the note's id ("delete note 3"); notes/.next holds the highest number
given out, so a deleted note's number isn't reused. The index (kind "note", id
note:<n>) is built from the files and follows edits; only the memory service writes here.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
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


def write_atomic(p: Path, text: str) -> None:
    """Replace a file's text in one step (a temp file in the same folder, then a rename)."""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="." + p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        Path(tmp).replace(p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def next_number(d: Path, have: list[Path]) -> int:
    """One past the highest number in use or ever given out (d/.next), which it records."""
    mark = 0
    with contextlib.suppress(OSError, ValueError):
        mark = int((d / ".next").read_text().strip())
    n = max([mark, *(number(p) or 0 for p in have)]) + 1
    write_atomic(d / ".next", f"{n}\n")
    return n


def write(root: Path, title: str, body: str, ts: float) -> Path:
    """A new note file, numbered one past the highest so far (deleted ones included)."""
    with _lock:
        d = notes_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        n = next_number(d, files(root))
        p = d / f"{n:04d}-{slug(title)}.md"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        write_atomic(p, f"# {' '.join(title.split())}\n_{when}_\n\n{body.strip()}\n")
        return p


def parse(p: Path) -> list[dict[str, Any]]:
    """The note in a file as an index item (none if the file isn't a note)."""
    n = number(p)
    if n is None or not p.exists():
        return []
    lines = p.read_text().split("\n")  # not splitlines(): U+2028 and the like stay in the text
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
