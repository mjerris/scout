"""Scout's tasks as markdown files, one per task (docs/tasks-design.md):

    memory/tasks/0012-send-the-revised-travel-budget-to-pat.md

    # Send the revised travel budget to Pat
    - status: open
    - owner: me
    - due: 2026-10-20
    - source: granola:not_1d3tmYTlCICgjy

    Pat asked for the revised numbers before Monday's review.

The "- key: value" lines right after the title are the task's fields; the rest is its
context. The number is the task's id; the index (kind "task", id task:<n>) is built from
the files and follows edits. Only the memory service writes here.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
from pathlib import Path
from typing import Any

FIELDS = (
    "status", "owner", "due", "importance", "urgency", "effort", "pin",
    "source", "found", "done", "reminder",
)  # fmt: skip
STATUSES = ("open", "done", "dropped")
_NAME = re.compile(r"^(\d+)-[^/]*\.md$")
_FIELD = re.compile(r"^- ([a-z_]+):\s*(.*?)\s*$")
_lock = threading.Lock()


def tasks_dir(root: Path) -> Path:
    return root / "tasks"


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60].rstrip("-") or "task"


def files(root: Path) -> list[Path]:
    d = tasks_dir(root)
    return sorted(p for p in d.glob("*.md") if _NAME.match(p.name)) if d.exists() else []


def number(p: Path) -> int | None:
    m = _NAME.match(p.name)
    return int(m.group(1)) if m else None


def find(root: Path, n: int) -> Path | None:
    return next((p for p in files(root) if number(p) == n), None)


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def render(title: str, fields: dict[str, Any], context: str) -> str:
    lines = [f"# {_clean(title)}"]
    for k in FIELDS:
        v = _clean(fields.get(k))
        if v:
            lines.append(f"- {k}: {v}")
    extra = [k for k in fields if k not in FIELDS and _clean(fields.get(k))]
    lines += [f"- {k}: {_clean(fields[k])}" for k in sorted(extra)]
    body = context.strip()
    return "\n".join(lines) + "\n" + (f"\n{body}\n" if body else "")


def read(p: Path) -> tuple[str, dict[str, str], str]:
    """(title, fields, context) of a task file."""
    lines = p.read_text().splitlines()
    title = lines.pop(0)[2:].strip() if lines and lines[0].startswith("# ") else ""
    fields: dict[str, str] = {}
    while lines and (m := _FIELD.match(lines[0])):
        fields[m.group(1)] = m.group(2)
        lines.pop(0)
    return title, fields, "\n".join(lines).strip()


def write(root: Path, title: str, fields: dict[str, Any], context: str) -> Path:
    """A new task file, numbered one past the highest so far."""
    with _lock:
        d = tasks_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        n = max((number(p) or 0 for p in files(root)), default=0) + 1
        p = d / f"{n:04d}-{slug(title)}.md"
        f = {"status": "open", "owner": "me", "found": time.strftime("%Y-%m-%d %H:%M"), **fields}
        p.write_text(render(title, f, context))
        return p


def update(p: Path, fields: dict[str, Any], add_context: str = "") -> None:
    """Change some fields (a None or "" value removes one) and add to the context."""
    with _lock:
        title, have, context = read(p)
        title = _clean(fields.pop("title", None)) or title
        for k, v in fields.items():
            if v is None or _clean(v) == "":
                have.pop(k, None)
            else:
                have[k] = _clean(v)
        if add_context.strip() and add_context.strip() not in context:
            context = f"{context}\n\n{add_context.strip()}" if context else add_context.strip()
        p.write_text(render(title, have, context))


def parse(p: Path) -> list[dict[str, Any]]:
    """The task in a file as an index item (meta holds its fields)."""
    n = number(p)
    if n is None or not p.exists():
        return []
    title, fields, context = read(p)
    ts = p.stat().st_mtime
    if fields.get("found"):
        with contextlib.suppress(ValueError):
            ts = time.mktime(time.strptime(fields["found"], "%Y-%m-%d %H:%M"))
    who = fields.get("owner", "me")
    return [{"id": f"task:{n}", "title": title, "body": context, "who": "" if who == "me" else who,
             "ts": ts, "meta": {**fields, "n": n}}]  # fmt: skip


def id_prefix(p: Path) -> str | None:
    n = number(p)
    return f"task:{n}" if n is not None else None
