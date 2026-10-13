"""The Mac's Reminders (iCloud, Exchange and other synced lists), through the same
native/vcal EventKit helper as the calendar. Reminders have their own macOS
permission, asked for on first use.

Reading is a pre-approved tool. Adding and completing a reminder are confirmed by
voice every time (see brain.REMINDER_WRITE_TOOLS)."""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

from . import calendar_mac, locallists
from .calendar_mac import ensure_access, iso, parse_time, text_arg
from .config import load
from .mac import ToolError

log = logging.getLogger(__name__)
_FALLBACK_S = 600.0  # after Reminders fails, use Scout's own lists this long before retrying
_fallback_until = 0.0
_GRANTED_S = 300.0  # a granted Reminders access is trusted this long before asking the helper again
_DENIED_S = 60.0  # and a refusal this long
_access: dict[str, tuple[float, str]] = {}  # helper path -> (good until, "" or the refusal)
_ACCESS_ERRORS = ("access not granted", "access is ")  # native/vcal's refusals


def _helper_key(helper: Path | None) -> str:
    return str(helper or calendar_mac.VCAL)


async def _ensure_reminders(helper: Path | None) -> None:
    """ensure_access for Reminders, remembered for a while: every operation asks, and the
    pulse and task farm ask often, so the helper isn't spawned for each one."""
    key = _helper_key(helper)
    until, refusal = _access.get(key, (0.0, ""))
    if time.monotonic() < until:
        if refusal:
            raise ToolError(refusal)
        return
    try:
        await ensure_access(helper, reminders=True)
    except ToolError as exc:
        _access[key] = (time.monotonic() + _DENIED_S, str(exc))
        raise
    _access[key] = (time.monotonic() + _GRANTED_S, "")


async def vcal(*args: str, helper: Path | None = None) -> dict[str, Any]:
    """calendar_mac.vcal; a refusal from a real operation forgets the remembered access."""
    try:
        return await calendar_mac.vcal(*args, helper=helper)
    except ToolError as exc:
        if any(e in str(exc) for e in _ACCESS_ERRORS):
            _access.pop(_helper_key(helper), None)
        raise


def _backend() -> str:
    return load().lists.backend


async def _apple(helper: Path | None) -> bool:
    """Use the Mac's Reminders (True) or Scout's own lists (False)? `[lists] backend`:
    "reminders", "scout", or "auto": Reminders when its access works, else Scout's own."""
    global _fallback_until
    backend = _backend()
    if backend == "scout":
        return False
    if backend == "reminders":
        await _ensure_reminders(helper)
        return True
    if time.monotonic() < _fallback_until:
        return False
    try:
        await _ensure_reminders(helper)
        return True
    except (ToolError, OSError) as exc:
        log.info("Reminders unavailable (%s); using Scout's own lists", exc)
        _fallback_until = time.monotonic() + _FALLBACK_S
        return False


_LIMIT = 100
_IDS_PER_CALL = 100  # reminder-get ids per helper call


async def available(helper: Path | None = None) -> bool:
    """Whether the Mac's Reminders (not Scout's own lists) answers right now."""
    try:
        return await _apple(helper)
    except (ToolError, OSError):
        return False


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


async def _kept_by_scout(list_name: Any, include_completed: Any, due_before: Any) -> dict[str, Any]:
    """auto: items added to Scout's own lists while Reminders couldn't be used. They stay
    there, so Reminders reads show them too (empty when there are none)."""
    if _backend() != "auto":
        return {"reminders": [], "total": 0}
    try:
        return await locallists.reminder_data(list_name, include_completed, due_before)
    except ToolError:  # no Scout list by that name
        return {"reminders": [], "total": 0}


async def list_names(helper: Path | None = None) -> list[str]:
    """The names of the Mac's reminder lists."""
    if not await _apple(helper):
        return await locallists.list_names()
    names = [str(c["title"]) for c in (await vcal("reminder-lists", helper=helper)).get("lists", [])]
    if (await _kept_by_scout(None, False, None))["reminders"]:
        names += [n for n in await locallists.list_names() if n.lower() not in {x.lower() for x in names}]
    return names


async def lists(helper: Path | None = None) -> str:
    if not await _apple(helper):
        return await locallists.lists()
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
    if not await _apple(helper):
        return await locallists.reminder_data(list_name, include_completed, due_before)
    kept = await _kept_by_scout(list_name, include_completed, due_before)
    try:
        data = await vcal(*args, helper=helper)
    except ToolError:
        if not kept["reminders"]:
            raise
        return kept  # a list only Scout has
    if not kept["reminders"]:
        return data
    return {
        **data,
        "reminders": [*data.get("reminders", []), *kept["reminders"]],
        "total": int(data.get("total", len(data.get("reminders", [])))) + kept["total"],
    }


async def reminders(
    list_name: Any = None,
    include_completed: Any = False,
    due_before: Any = None,
    helper: Path | None = None,
) -> str:
    return format_reminders(await reminder_data(list_name, include_completed, due_before, helper))


async def add(a: dict[str, Any], helper: Path | None = None) -> str:
    r = await add_data(a, helper)
    if "said" in r:  # Scout's own lists answered
        return str(r["said"])
    when = _when(r)
    return f"Added {r.get('title', a.get('title'))} to {r.get('list') or 'Reminders'}" + (
        f", due {when}." if when else "."
    )


async def add_data(a: dict[str, Any], helper: Path | None = None, mirror: bool = False) -> dict[str, Any]:
    """Add a reminder; the helper's record of it (with its id), or {"said": ...} when
    Scout's own lists took it. mirror (Scout's task list): the Mac's Reminders only,
    making the list when it's missing."""
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
    if mirror:
        args.append("--create-list")
    if not await _apple(helper):  # after the checks: bad arguments never touch either store
        if mirror:
            raise ToolError("Reminders can't be reached")
        return {"said": await locallists.add(a)}
    try:
        r: dict[str, Any] = (await vcal(*args, helper=helper)).get("reminder", {})
    except ToolError as exc:
        if not mirror or "--create-list" not in str(exc):
            raise
        # A helper built before --create-list: add to the list if it's there.
        r = (await vcal(*args[:-1], helper=helper)).get("reminder", {})
    return r


async def complete(a: dict[str, Any], helper: Path | None = None, mirror: bool = False) -> str:
    if str(a.get("id") or "").startswith("scout:") and not mirror:
        return await locallists.complete(a)
    if not await _apple(helper):
        if _backend() == "scout" and not mirror:
            return await locallists.complete(a)
        raise ToolError(  # a Reminders item: Scout's own lists don't have it
            "Reminders can't be reached right now, so that reminder can't be marked done yet. "
            "Try again in a few minutes."
        )
    rid = text_arg(a.get("id"), "id", 200)
    title = text_arg(a.get("title"), "title", 200)
    if not rid or not title:
        raise ToolError("id and title are required (both from reminders_list)")
    r = (await vcal("reminder-complete", "--id", rid, "--title", title, helper=helper)).get("reminder", {})
    return f"Marked {r.get('title', title)} done" + (f" in {r['list']}." if r.get("list") else ".")


async def states(ids: list[str], helper: Path | None = None) -> dict[str, Any]:
    """Reminders by id, wherever they are and however many: {"reminders": [...], "missing":
    [ids not found]}. The Mac's Reminders only."""
    if not await _apple(helper):
        raise ToolError("Reminders can't be reached")
    out: dict[str, Any] = {"reminders": [], "missing": []}
    for i in range(0, len(ids), _IDS_PER_CALL):
        args = ["reminder-get"]
        for rid in ids[i : i + _IDS_PER_CALL]:
            args += ["--id", text_arg(rid, "id", 200)]
        try:
            data = await vcal(*args, helper=helper)
        except ToolError as exc:
            if "unknown command" not in str(exc):
                raise
            return await _states_from_all(ids, helper)  # a helper built before reminder-get
        out["reminders"] += data.get("reminders", [])
        out["missing"] += data.get("missing", [])
    return out


async def _states_from_all(ids: list[str], helper: Path | None) -> dict[str, Any]:
    data = await vcal("reminders", "--include-completed", "--limit", "1000000", helper=helper)
    want = set(ids)
    found = [r for r in data.get("reminders", []) if str(r.get("id")) in want]
    have = {str(r.get("id")) for r in found}
    return {"reminders": found, "missing": [i for i in ids if i not in have]}


async def update(a: dict[str, Any], helper: Path | None = None) -> dict[str, Any]:
    """Change a reminder's title or due date (due "" clears it). The Mac's Reminders only."""
    rid = text_arg(a.get("id"), "id", 200)
    if not rid:
        raise ToolError("id is required")
    args = ["reminder-update", "--id", rid]
    if "title" in a:
        title = text_arg(a.get("title"), "title", 200)
        if not title:
            raise ToolError("title can't be empty")
        args += ["--title", title]
    if "due" in a:
        due = text_arg(a.get("due"), "due", 40)
        if due:
            parse_time(due, "due")
        args += ["--due", due or "none"]
    if not await _apple(helper):
        raise ToolError("Reminders can't be reached")
    r: dict[str, Any] = (await vcal(*args, helper=helper)).get("reminder", {})
    return r
