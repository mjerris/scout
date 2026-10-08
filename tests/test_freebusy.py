"""Free and busy time, worked out in plain code from calendar events."""

import asyncio
import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest

from scout import freebusy
from scout.config import DATA
from scout.mac import ToolError

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 8, 7, 0, tzinfo=TZ)  # before the working day
HOURS = (dt.time(9), dt.time(17))


def ev(title: str, start: str, end: str, **kw: Any) -> dict[str, Any]:
    return {"title": title, "all_day": False, "start": f"{start}-04:00", "end": f"{end}-04:00", **kw}


DAY = [
    ev("Standup", "2026-10-08T09:30:00", "2026-10-08T09:45:00"),
    ev("Review", "2026-10-08T09:40:00", "2026-10-08T10:30:00"),  # overlaps standup
    ev("Lunch", "2026-10-08T12:00:00", "2026-10-08T13:00:00", free=True),  # shown as free
    ev("Dentist", "2026-10-08T15:00:00", "2026-10-08T16:00:00"),
    {"title": "Holiday", "all_day": True, "start": "2026-10-08", "end": "2026-10-09"},
]


def test_overlapping_events_merge_into_one_busy_block() -> None:
    busy = freebusy.merge(freebusy.timed(DAY))
    assert [(b.start.strftime("%H:%M"), b.end.strftime("%H:%M"), b.titles) for b in busy] == [
        ("09:30", "10:30", ("Standup", "Review")),
        ("15:00", "16:00", ("Dentist",)),
    ]


def test_gaps_inside_working_hours() -> None:
    ws, we = freebusy.window(NOW.date(), HOURS, NOW)
    gaps = freebusy.gaps(freebusy.timed(DAY), ws, we, 30)
    assert [(g.start.strftime("%H:%M"), g.end.strftime("%H:%M")) for g in gaps] == [
        ("09:00", "09:30"),
        ("10:30", "15:00"),
        ("16:00", "17:00"),
    ]
    assert freebusy.gaps(freebusy.timed(DAY), ws, we, 61)[0].minutes == 270


def test_today_starts_from_now_rounded_to_the_quarter_hour() -> None:
    now = NOW.replace(hour=14, minute=37)
    assert freebusy.window(now.date(), HOURS, now)[0].strftime("%H:%M") == "14:45"
    late = NOW.replace(hour=18)
    ws, we = freebusy.window(late.date(), HOURS, late)
    assert ws == we  # nothing left today
    tomorrow = freebusy.window(late.date() + dt.timedelta(days=1), HOURS, late)
    assert tomorrow[0].strftime("%d %H:%M") == "09 09:00"


def test_spoken_pieces() -> None:
    s = freebusy.Span(NOW.replace(hour=11), NOW.replace(hour=12))
    assert freebusy.span_words(s) == "11 AM to noon"
    assert freebusy.span_words(freebusy.Span(NOW.replace(hour=14, minute=30), NOW.replace(hour=16))) == (
        "2:30 to 4 PM"
    )
    assert [freebusy.duration_words(m) for m in (15, 30, 60, 90, 135, 210)] == [
        "15 minutes",
        "half an hour",
        "an hour",
        "an hour and a half",
        "2 hours and 15 minutes",
        "3 and a half hours",
    ]


def test_day_load() -> None:
    assert freebusy.day_load(DAY, NOW.date(), HOURS, NOW, "Today") == (
        "Today you have 3 events, 2 hours in all, from 9:30 AM to 4 PM. All day: Holiday. "
        "Your longest free stretch is 10:30 AM to 3 PM."
    )
    assert freebusy.day_load([], NOW.date(), HOURS, NOW, "Tomorrow") == "Tomorrow your calendar is clear."
    packed = [ev("Offsite", "2026-10-08T08:00:00", "2026-10-08T18:00:00")]
    assert freebusy.day_load(packed, NOW.date(), HOURS, NOW, "Today").endswith(
        "No free half hour between 9 AM and 5 PM."
    )


def test_the_shared_tool_lists_gaps_and_busy_blocks_per_day() -> None:
    asked: list[tuple[str, str]] = []

    async def events(start: str, end: str) -> dict[str, Any]:
        asked.append((start, end))
        return {"events": DAY, "total": len(DAY)}

    out = asyncio.run(freebusy.free("2026-10-08", "2026-10-10", 30, HOURS, NOW, events))
    assert asked[0][0].startswith("2026-10-08T00:00:00") and asked[0][1].startswith("2026-10-10T00:00:00")
    assert out.splitlines()[:7] == [
        "Working hours 9:00 AM to 5:00 PM.",
        "Thu Oct 8:",
        "  free 9:00 AM to 9:30 AM (30 min)",
        "  busy 9:30 AM to 10:30 AM: Standup, Review",
        "  free 10:30 AM to 3:00 PM (270 min)",
        "  busy 3:00 PM to 4:00 PM: Dentist",
        "  free 4:00 PM to 5:00 PM (60 min)",
    ]
    assert "  all day: Holiday" in out
    assert "Fri Oct 9:\n  free 9:00 AM to 5:00 PM (480 min)" in out


@pytest.mark.parametrize(
    ("start", "end", "minutes", "error"),
    [
        ("2026-10-08", "2026-10-07", 30, "after start"),
        ("2026-10-01", "2026-12-01", 30, "at most 31 days"),
        ("2026-10-08", None, 0, "min_minutes"),
        ("2026-10-08", None, "ten", "min_minutes"),
        ("soon", None, 30, "ISO 8601"),
    ],
)
def test_the_shared_tool_checks_its_arguments(start: str, end: str | None, minutes: Any, error: str) -> None:
    async def events(start: str, end: str) -> dict[str, Any]:
        raise AssertionError("must not read the calendar")

    with pytest.raises(ToolError, match=error):
        asyncio.run(freebusy.free(start, end, minutes, HOURS, NOW, events))


@pytest.fixture
def config_file() -> Iterator[Any]:
    path = DATA / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path
    path.unlink(missing_ok=True)


def test_working_hours_come_from_config(config_file: Any) -> None:
    assert freebusy.work_hours() == HOURS
    config_file.write_text('[calendar]\nwork_start = "08:30"\nwork_end = "18:00"\n')
    assert freebusy.work_hours() == (dt.time(8, 30), dt.time(18))
    config_file.write_text('[calendar]\nwork_start = "8am"\n')
    with pytest.raises(ToolError, match=r"calendar\.work_start must be a time like 09:00"):
        freebusy.work_hours()
    config_file.write_text('[calendar]\nwork_start = "18:00"\nwork_end = "09:00"\n')
    with pytest.raises(ToolError, match=r"after calendar\.work_start"):
        freebusy.work_hours()
