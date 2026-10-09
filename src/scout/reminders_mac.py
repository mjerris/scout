"""The Mac's Reminders (iCloud, Exchange and other synced lists), through the same
native/vcal EventKit helper as the calendar. Reminders have their own macOS
permission, asked for on first use.

Reading is a pre-approved tool. Adding and completing a reminder are confirmed by
voice every time (see brain.REMINDER_WRITE_TOOLS)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .calendar_mac import ensure_access, iso, parse_time, text_arg, vcal
from .mac import ToolError

_LIMIT = 100


def due_time(r: dict[str, Any]) -> dt.datetime | None:
    """When a reminder is due (a date-only due counts from that day's start), or None."""
    due = r.get("due")
    if not due:
        return None
    t = dt.datetime.fromisoformat(due)
    return t if t.tzinfo else t.astimezone()


def is_overdue(r: dict[str, Any], now: dt.datetime) -> bool:
    t = due_time(r)
    if t is None or r.get("completed"):
        return False
    if "T" not in r["due"]:  # due on a day: overdue once that day is over
        return t.date() < now.date()
    return t < now


def _when(r: dict[str, Any]) -> str:
    t = due_time(r)
    if t is None:
        return ""
    return t.strftime("%a %b %-d") if "T" not in r["due"] else t.strftime("%a %b %-d, %-I:%M %p")


def format_reminders(data: dict[str, Any], now: dt.datetime | None = None) -> str:
    items = data.get("reminders", [])
    if not items:
        return "No reminders."
    now = now or dt.datetime.now().astimezone()
    lines = []
    for r in items:
        line = f"{r.get('title') or '(no title)'} [{r.get('list', '')}]"
        notes = []
        if r.get("completed"):
            notes.append("done")
        if when := _when(r):
            notes.append(f"due {when}" + (", overdue" if is_overdue(r, now) else ""))
        if notes:
            line += f" ({'; '.join(notes)})"
        line += f" (id {r['id']}" + (f", due {r['due']})" if r.get("due") else ")")
        if r.get("notes"):
            line += f"\n  notes: {' '.join(str(r['notes']).split())[:200]}"
        lines.append(line)
    total = data.get("total", len(items))
    if total > len(items):
        lines.append(f"(showing {len(items)} of {total}; name a list or a due date)")
    return "\n".join(lines)


async def list_names(helper: Path | None = None) -> list[str]:
    """The names of the Mac's reminder lists."""
    await ensure_access(helper, reminders=True)
    return [str(c["title"]) for c in (await vcal("reminder-lists", helper=helper)).get("lists", [])]


async def lists(helper: Path | None = None) -> str:
    await ensure_access(helper, reminders=True)
    found = (await vcal("reminder-lists", helper=helper)).get("lists", [])
    if not found:
        return "No reminder lists on this Mac."
    return "\n".join(
        f"{c['title']} ({c.get('account') or 'local'})"
        + (", default" if c.get("default") else "")
        + ("" if c.get("writable") else ", read-only")
        for c in found
    )


async def reminder_data(
    list_name: Any = None,
    include_completed: Any = False,
    due_before: Any = None,
    helper: Path | None = None,
) -> dict[str, Any]:
    """The helper's structured answer: {"reminders": [...], "total": n}. Without
    include_completed, only open reminders; due_before keeps those due before it."""
    args = ["reminders", "--limit", str(_LIMIT)]
    if name := text_arg(list_name, "list", 200):
        args += ["--list", name]
    if include_completed:
        args.append("--include-completed")
    if due_before:
        args += ["--due-before", iso(parse_time(due_before, "due_before"))]
    await ensure_access(helper, reminders=True)
    return await vcal(*args, helper=helper)


async def reminders(
    list_name: Any = None,
    include_completed: Any = False,
    due_before: Any = None,
    helper: Path | None = None,
) -> str:
    return format_reminders(await reminder_data(list_name, include_completed, due_before, helper))


async def add(a: dict[str, Any], helper: Path | None = None) -> str:
    title = text_arg(a.get("title"), "title", 200)
    if not title:
        raise ToolError("title is required")
    args = ["reminder-add", "--title", title]
    if name := text_arg(a.get("list"), "list", 200):
        args += ["--list", name]
    if due := text_arg(a.get("due"), "due", 40):
        t = parse_time(due, "due")
        args += ["--due", due if "T" not in due and " " not in due else iso(t)]
    if notes := text_arg(a.get("notes"), "notes", 2000):
        args += ["--notes", notes]
    await ensure_access(helper, reminders=True)
    r = (await vcal(*args, helper=helper)).get("reminder", {})
    when = _when(r)
    return f"Added {r.get('title', title)} to {r.get('list') or 'Reminders'}" + (
        f", due {when}." if when else "."
    )


async def complete(a: dict[str, Any], helper: Path | None = None) -> str:
    rid = text_arg(a.get("id"), "id", 200)
    title = text_arg(a.get("title"), "title", 200)
    if not rid or not title:
        raise ToolError("id and title are required (both from reminders_list)")
    await ensure_access(helper, reminders=True)
    r = (await vcal("reminder-complete", "--id", rid, "--title", title, helper=helper)).get("reminder", {})
    return f"Marked {r.get('title', title)} done" + (f" in {r['list']}." if r.get("list") else ".")
