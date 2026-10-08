"""Free and busy time from the calendar, in plain code: busy blocks (timed events,
merged), the free gaps between them inside working hours, and short spoken
sentences about a day. No model is involved, so the answers are exact.

All-day events and events marked "free" don't block time (an all-day holiday or
out-of-office is reported, not subtracted)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import calendar_mac
from .config import load
from .mac import ToolError

Hours = tuple[dt.time, dt.time]
_MAX_DAYS = 31


@dataclass(frozen=True)
class Span:
    start: dt.datetime
    end: dt.datetime
    titles: tuple[str, ...] = ()

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def parse_clock(value: str, what: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{what} must be a time like 09:00, not {value!r}") from None


def work_hours() -> Hours:
    """Working hours from config.toml ([calendar] work_start, work_end)."""
    try:
        cfg = load().calendar
        start = parse_clock(cfg.work_start, "calendar.work_start")
        end = parse_clock(cfg.work_end, "calendar.work_end")
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if end <= start:
        raise ToolError("calendar.work_end must be after calendar.work_start")
    return start, end


def timed(events: list[dict[str, Any]]) -> list[Span]:
    """Events that take up time, soonest first."""
    out = [
        Span(
            dt.datetime.fromisoformat(e["start"]),
            dt.datetime.fromisoformat(e["end"]),
            (e.get("title") or "an untitled event",),
        )
        for e in events
        if not e.get("all_day") and not e.get("free") and e.get("start") and e.get("end")
    ]
    return sorted(out, key=lambda s: (s.start, s.end))


def all_day(events: list[dict[str, Any]], day: dt.date) -> list[str]:
    return [
        e.get("title") or "an untitled event"
        for e in events
        if e.get("all_day")
        and dt.date.fromisoformat(e["start"]) <= day < dt.date.fromisoformat(e.get("end") or e["start"])
    ]


def merge(spans: list[Span]) -> list[Span]:
    out: list[Span] = []
    for s in sorted(spans, key=lambda s: s.start):
        if out and s.start <= out[-1].end:
            last = out[-1]
            out[-1] = Span(last.start, max(last.end, s.end), last.titles + s.titles)
        else:
            out.append(s)
    return out


def gaps(busy: list[Span], start: dt.datetime, end: dt.datetime, min_minutes: float = 1) -> list[Span]:
    """Free stretches of at least min_minutes between start and end."""
    out, cur = [], start
    for b in merge(busy):
        if b.end <= cur:
            continue
        if b.start >= end:
            break
        if b.start > cur:
            out.append(Span(cur, b.start))
        cur = max(cur, b.end)
    if cur < end:
        out.append(Span(cur, end))
    return [g for g in out if g.minutes >= min_minutes]


def window(day: dt.date, hours: Hours, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """That day's working hours, in now's timezone; today's starts no earlier than
    now, rounded up to the quarter hour."""
    tz = now.tzinfo
    start = dt.datetime.combine(day, hours[0], tz)
    end = dt.datetime.combine(day, hours[1], tz)
    if day == now.date() and now > start:
        q = now.replace(second=0, microsecond=0)
        q += dt.timedelta(minutes=-q.minute % 15)
        start = min(max(start, q), end)
    return start, end


def day_range(day: dt.date, now: dt.datetime) -> tuple[str, str]:
    start = dt.datetime.combine(day, dt.time(0), now.tzinfo)
    return start.isoformat(), (start + dt.timedelta(days=1)).isoformat()


# --- the shared tool ------------------------------------------------------------------


def _t(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p")


async def free(
    start: Any = None,
    end: Any = None,
    min_minutes: Any = 30,
    hours: Hours | None = None,
    now: dt.datetime | None = None,
    events: Callable[..., Awaitable[dict[str, Any]]] = calendar_mac.event_data,
) -> str:
    """calendar_free: per day in [start, end), the free gaps in working hours and the busy blocks."""
    now = now or dt.datetime.now().astimezone()
    s = (
        calendar_mac.parse_time(start, "start")
        if start
        else now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    e = calendar_mac.parse_time(end, "end") if end else s + dt.timedelta(days=1)
    if e <= s:
        raise ToolError("end must be after start")
    if e - s > dt.timedelta(days=_MAX_DAYS):
        raise ToolError(f"the range can be at most {_MAX_DAYS} days")
    if (
        isinstance(min_minutes, bool)
        or not isinstance(min_minutes, int | float)
        or not 1 <= min_minutes <= 600
    ):
        raise ToolError("min_minutes must be a number from 1 to 600")
    hours = hours or work_hours()
    data = await events(calendar_mac.iso(s), calendar_mac.iso(e))
    evs = data.get("events", [])
    busy = merge(timed(evs))
    lines = [f"Working hours {hours[0].strftime('%-I:%M %p')} to {hours[1].strftime('%-I:%M %p')}."]
    day = s.astimezone(now.tzinfo).date()
    while dt.datetime.combine(day, dt.time(0), now.tzinfo) < e:
        ws, we = window(day, hours, now)
        ws, we = max(ws, s), min(we, e)
        head = day.strftime("%a %b %-d")
        if day < now.date() or ws >= we:
            lines.append(f"{head}: no working hours left in the range.")
        else:
            lines.append(f"{head}:")
            free_spans = gaps(busy, ws, we, float(min_minutes))
            items = [
                (g.start, f"free {_t(g.start)} to {_t(g.end)} ({round(g.minutes)} min)") for g in free_spans
            ]
            items += [
                (b.start, f"busy {_t(b.start)} to {_t(b.end)}: {', '.join(b.titles)}")
                for b in busy
                if b.start < we and b.end > ws
            ]
            for _, text in sorted(items):
                lines.append(f"  {text}")
            if not free_spans:
                lines.append(f"  no free stretch of {min_minutes} minutes or more")
            if titles := all_day(evs, day):
                lines.append(f"  all day: {', '.join(titles)}")
        day += dt.timedelta(days=1)
    if data.get("total", len(evs)) > len(evs):
        lines.append(
            f"(only the first {len(evs)} of {data['total']} events were checked; use a shorter range)"
        )
    return "\n".join(lines)


# --- saying it out loud ---------------------------------------------------------------


def clock(t: dt.datetime | dt.time) -> str:
    """'2:37 PM', '3 PM', 'noon', 'midnight'."""
    if t.hour == 12 and t.minute == 0:
        return "noon"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    return t.strftime("%-I:%M %p") if t.minute else t.strftime("%-I %p")


def span_words(s: Span) -> str:
    """'9 to 10 AM', '11 AM to noon', '2:30 to 4 PM'."""
    a, b = clock(s.start), clock(s.end)
    if a[-2:] == b[-2:] and a[-2:] in ("AM", "PM"):
        a = a[:-3]
    return f"{a} to {b}"


def duration_words(minutes: float) -> str:
    m = int(round(minutes / 5) * 5) or round(minutes)
    h, m = divmod(m, 60)
    if not h:
        return f"{m} minutes" if m != 30 else "half an hour"
    hours = "an hour" if h == 1 else f"{h} hours"
    if m == 30:
        return "an hour and a half" if h == 1 else f"{h} and a half hours"
    return hours if not m else f"{hours} and {m} minutes"


def join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def day_load(events: list[dict[str, Any]], day: dt.date, hours: Hours, now: dt.datetime, subject: str) -> str:
    """'Today you have 3 events, 2 hours in all, from 9:30 AM to 4 PM. Your longest
    free stretch is 10 AM to noon.' `subject` starts the sentence ("Today", "On Friday")."""
    ds, de = (dt.datetime.fromisoformat(x) for x in day_range(day, now))
    spans = [s for s in timed(events) if s.start < de and s.end > ds]
    allday = all_day(events, day)
    if not spans:
        if allday:
            return f"{subject} there's nothing at a set time; all day: {join(allday)}."
        return f"{subject} your calendar is clear."
    busy = merge(spans)
    total = sum((min(b.end, de) - max(b.start, ds)).total_seconds() / 60 for b in busy)
    n = len(spans)
    out = (
        f"{subject} you have {n} event{'s' * (n != 1)}, {duration_words(total)} in all, "
        f"from {span_words(Span(max(busy[0].start, ds), min(busy[-1].end, de)))}."
    )
    if allday:
        out += f" All day: {join(allday)}."
    ws, we = window(day, hours, now)
    free_spans = gaps(busy, ws, we, 30)
    if free_spans:
        longest = max(free_spans, key=lambda g: g.minutes)
        out += f" Your longest free stretch is {span_words(longest)}."
    elif ws < we:
        out += f" No free half hour between {clock(ws)} and {clock(we)}."
    return out
