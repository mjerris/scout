"""The Mac's calendars (Google, iCloud, Exchange: whatever is synced through
Internet Accounts), read and added to through the native/vcal EventKit helper.
macOS holds the account credentials; nothing here sees a token.

Reading is a pre-approved tool. Adding an event is confirmed by voice every time
(see brain.CALENDAR_WRITE_TOOLS)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .config import DATA
from .mac import ToolError

VCAL = DATA / "bin" / "vcal"  # scripts/build-vcal.sh
_MAX_DAYS = 400
_MAX_TEXT = 300


async def _vcal(*args: str, timeout: float = 15.0, helper: Path | None = None) -> dict[str, Any]:
    exe = helper or VCAL
    if not exe.exists():
        raise ToolError("The calendar helper isn't built; ask the owner to run scripts/build-vcal.sh.")
    proc = await asyncio.create_subprocess_exec(
        str(exe), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise ToolError("the calendar helper timed out") from None
    try:
        data: dict[str, Any] = json.loads(out.decode(errors="replace") or "{}")
    except json.JSONDecodeError:
        raise ToolError(err.decode(errors="replace").strip()[:300] or "the calendar helper failed") from None
    if "error" in data and (proc.returncode != 0 or data["error"]):
        raise ToolError(str(data["error"]))
    return data


async def ensure_access(helper: Path | None = None) -> None:
    """Ask macOS for calendar access the first time (a prompt on the Mac's screen)."""
    status = (await _vcal("status", helper=helper)).get("status")
    if status == "full_access":
        return
    if status == "not_determined":
        res = await _vcal("request", timeout=120, helper=helper)
        if res.get("granted"):
            return
        raise ToolError(
            "Calendar access wasn't allowed. A prompt appears on the Mac's screen the first "
            "time; it can also be turned on in System Settings, Privacy and Security, Calendars."
        )
    raise ToolError(
        f"Calendar access is {status}. Turn it on in System Settings, Privacy and Security, Calendars."
    )


def parse_time(value: Any, what: str) -> dt.datetime:
    """ISO 8601 date or date-time; without an offset it's local time."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{what} must be an ISO 8601 date or time, like 2026-10-08T15:00")
    try:
        t = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(
            f"{what} must be an ISO 8601 date or time, like 2026-10-08T15:00 (got {value!r})"
        ) from None
    return t if t.tzinfo else t.astimezone()  # naive = local


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _text(value: Any, what: str, limit: int = _MAX_TEXT) -> str:
    s = str(value or "").strip()
    if len(s) > limit:
        raise ToolError(f"{what} is too long (max {limit} characters)")
    if any(ord(c) < 32 and c not in "\n\t" for c in s):
        raise ToolError(f"{what} has control characters")
    return s


def _when(e: dict[str, Any]) -> str:
    if e.get("all_day"):
        start = dt.date.fromisoformat(e["start"])
        end = dt.date.fromisoformat(e["end"])
        day = start.strftime("%a %b %-d")
        return (
            f"{day}, all day"
            if end <= start + dt.timedelta(days=1)
            else f"{day} to {end.strftime('%a %b %-d')}"
        )
    s, t = dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"])
    same_day = s.date() == t.date()
    return f"{s.strftime('%a %b %-d, %-I:%M %p')} to {t.strftime('%-I:%M %p' if same_day else '%a %b %-d, %-I:%M %p')}"


_VIDEO = re.compile(r"meet\.google\.com|zoom\.us/j|teams\.microsoft\.com|webex\.com", re.I)
_BOILERPLATE = re.compile(
    r"^(join with google meet|join zoom meeting|or dial|more phone numbers|learn more about meet|"
    r"meeting id|passcode|pin:|join on your computer|microsoft teams meeting|dial in|one tap mobile|"
    r"please do not edit this section|invitation from google calendar)",
    re.I,
)


def clean_notes(text: str) -> tuple[str, bool]:
    """Drop conferencing boilerplate (dial-ins, separators, join links); report a video link."""
    video = bool(_VIDEO.search(text or ""))
    keep = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or not re.search(r"[A-Za-z]{2}", s) or _BOILERPLATE.match(s) or _VIDEO.search(s):
            continue
        if re.match(r"^(https?://|tel:|\+?\d[\d\s().-]{6,}$)", s):
            continue
        keep.append(s)
    return " ".join(keep)[:200], video


def format_events(data: dict[str, Any]) -> str:
    events = data.get("events", [])
    if not events:
        return "No events in that range."
    lines = []
    for e in events:
        line = f"{_when(e)}: {e.get('title') or '(no title)'} [{e.get('calendar', '')}]"
        if e.get("location"):
            line += f" at {e['location']}"
        if e.get("recurring"):
            line += " (repeats)"
        if e.get("attendees"):
            line += f" ({e['attendees']} people)"
        line += f" ({e['start']} to {e['end']})"  # exact values, for programs
        notes, video = clean_notes(e.get("notes", ""))
        if video:
            line += " (has a video link)"
        if notes:
            line += f"\n  notes: {notes}"
        lines.append(line)
    total = data.get("total", len(events))
    if total > len(events):
        lines.append(f"(showing {len(events)} of {total}; narrow the range or add a query)")
    return "\n".join(lines)


async def events(
    start: Any = None,
    end: Any = None,
    query: Any = None,
    calendar: Any = None,
    helper: Path | None = None,
) -> str:
    """Events from start (default: now's start of day) to end (default: start + 1 day)."""
    s = (
        parse_time(start, "start")
        if start
        else dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )
    e = parse_time(end, "end") if end else s + dt.timedelta(days=1)
    if e <= s:
        raise ToolError("end must be after start")
    if e - s > dt.timedelta(days=_MAX_DAYS):
        raise ToolError(f"the range can be at most {_MAX_DAYS} days")
    await ensure_access(helper)
    args = ["events", "--from", _iso(s), "--to", _iso(e), "--limit", "100"]
    if q := _text(query, "query", 100):
        args += ["--query", q]
    if c := _text(calendar, "calendar", 200):
        args += ["--calendar", c]
    return format_events(await _vcal(*args, helper=helper))


async def calendars(helper: Path | None = None) -> str:
    await ensure_access(helper)
    cals = (await _vcal("calendars", helper=helper)).get("calendars", [])
    if not cals:
        return "No calendars on this Mac. Add an account in System Settings, Internet Accounts."
    return "\n".join(
        f"{c['title']} ({c.get('account') or 'local'}){'' if c.get('writable') else ', read-only'}"
        for c in cals
    )


async def create_event(a: dict[str, Any], helper: Path | None = None) -> str:
    title = _text(a.get("title"), "title", 200)
    if not title:
        raise ToolError("title is required")
    all_day = bool(a.get("all_day"))
    start = parse_time(a.get("start"), "start")
    end = (
        parse_time(a["end"], "end")
        if a.get("end")
        else start + dt.timedelta(days=1 if all_day else 0, hours=0 if all_day else 1)
    )
    if end < start:
        raise ToolError("end must not be before start")
    if end - start > dt.timedelta(days=31):
        raise ToolError("an event can be at most 31 days long")
    await ensure_access(helper)
    args = ["create", "--title", title, "--start", _iso(start), "--end", _iso(end)]
    for key, flag, limit in (
        ("calendar", "--calendar", 200),
        ("location", "--location", 200),
        ("notes", "--notes", 2000),
    ):
        if v := _text(a.get(key), key, limit):
            args += [flag, v]
    if all_day:
        args.append("--all-day")
    res = await _vcal(*args, helper=helper)
    return f"Added {res.get('title', title)} to {res.get('calendar', 'the calendar')}, {_when({**res, 'all_day': False}) if not all_day else start.strftime('%a %b %-d')}."
