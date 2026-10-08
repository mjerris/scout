"""Calendar tools: argument checks, the helper protocol (through a fake vcal that
records its arguments), first-use access, and the voice approval for adding events."""

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from claude_voice import calendar_mac
from claude_voice.mac import ToolError

EVENTS = {
    "events": [
        {
            "title": "Standup",
            "calendar": "Work",
            "all_day": False,
            "start": "2026-10-08T09:30:00-05:00",
            "end": "2026-10-08T09:45:00-05:00",
            "recurring": True,
        },
        {
            "title": "Dentist",
            "calendar": "Home",
            "all_day": False,
            "location": "Main St",
            "start": "2026-10-08T15:00:00-05:00",
            "end": "2026-10-08T16:00:00-05:00",
        },
        {"title": "Holiday", "calendar": "Home", "all_day": True, "start": "2026-10-09", "end": "2026-10-10"},
    ],
    "total": 3,
}


def _fake(tmp_path: Path, status: str = "full_access", granted: bool = True, reply: Any = None) -> Path:
    """A stand-in vcal: logs each argv as a JSON line and answers like the real one."""
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "vcal"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        f"if cmd == 'status': print(json.dumps({{'status': {status!r}}}))\n"
        f"elif cmd == 'request': print(json.dumps({{'granted': {granted!r}, 'status': 'x'}}))\n"
        f"else: print({json.dumps(json.dumps(reply or EVENTS))})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_events_are_read_and_formatted(tmp_path: Path) -> None:
    exe = _fake(tmp_path)
    out = asyncio.run(calendar_mac.events("2026-10-08", "2026-10-10", "dent", "Home", helper=exe))
    assert "Thu Oct 8, 9:30 AM to 9:45 AM: Standup [Work] (repeats)" in out
    assert "Thu Oct 8, 3:00 PM to 4:00 PM: Dentist [Home] at Main St" in out
    assert "Fri Oct 9, all day: Holiday [Home]" in out
    assert "Dentist [Home] at Main St (2026-10-08T15:00:00-05:00 to 2026-10-08T16:00:00-05:00)" in out
    call = _calls(tmp_path)[-1]
    assert call[0] == "events"
    assert call[call.index("--query") + 1] == "dent" and call[call.index("--calendar") + 1] == "Home"
    assert call[call.index("--from") + 1].startswith("2026-10-08T00:00:00")
    assert call[call.index("--to") + 1].startswith("2026-10-10T00:00:00")


def test_default_range_is_today(tmp_path: Path) -> None:
    exe = _fake(tmp_path, reply={"events": [], "total": 0})
    assert asyncio.run(calendar_mac.events(helper=exe)) == "No events in that range."
    call = _calls(tmp_path)[-1]
    import datetime as dt

    today = dt.date.today().isoformat()
    assert call[call.index("--from") + 1].startswith(today + "T00:00:00")


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        ("tomorrow", None, "ISO 8601"),
        ("2026-10-08", "2026-10-07", "after start"),
        ("2026-01-01", "2027-06-01", "at most 400 days"),
    ],
)
def test_bad_ranges_never_reach_the_helper(tmp_path: Path, start: str, end: str | None, error: str) -> None:
    exe = _fake(tmp_path)
    with pytest.raises(ToolError, match=error):
        asyncio.run(calendar_mac.events(start, end, helper=exe))
    assert not (tmp_path / "calls.jsonl").exists()


def test_first_use_asks_macos_for_access(tmp_path: Path) -> None:
    exe = _fake(tmp_path, status="not_determined", granted=True)
    asyncio.run(calendar_mac.events("2026-10-08", helper=exe))
    assert [c[0] for c in _calls(tmp_path)] == ["status", "request", "events"]


def test_refused_access_is_explained(tmp_path: Path) -> None:
    exe = _fake(tmp_path, status="not_determined", granted=False)
    with pytest.raises(ToolError, match="wasn't allowed"):
        asyncio.run(calendar_mac.events("2026-10-08", helper=exe))
    exe = _fake(tmp_path, status="denied")
    with pytest.raises(ToolError, match="System Settings"):
        asyncio.run(calendar_mac.calendars(helper=exe))


def test_helper_errors_are_reported(tmp_path: Path) -> None:
    exe = _fake(tmp_path, reply={"error": "no calendar matches Nope"})
    with pytest.raises(ToolError, match="no calendar matches Nope"):
        asyncio.run(calendar_mac.events("2026-10-08", calendar="Nope", helper=exe))


def test_missing_helper(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="build-vcal"):
        asyncio.run(calendar_mac.events(helper=tmp_path / "nope"))


def test_create_event_passes_checked_arguments(tmp_path: Path) -> None:
    exe = _fake(
        tmp_path,
        reply={
            "created": True,
            "title": "Dentist",
            "calendar": "Home",
            "start": "2026-10-09T15:00:00-05:00",
            "end": "2026-10-09T16:00:00-05:00",
        },
    )
    out = asyncio.run(
        calendar_mac.create_event(
            {"title": "Dentist", "start": "2026-10-09T15:00", "calendar": "Home", "location": "Main St"},
            helper=exe,
        )
    )
    assert out == "Added Dentist to Home, Fri Oct 9, 3:00 PM to 4:00 PM."
    call = _calls(tmp_path)[-1]
    assert call[0] == "create" and call[call.index("--title") + 1] == "Dentist"
    end = call[call.index("--end") + 1]
    assert end.startswith("2026-10-09T16:00:00")  # one hour by default
    assert call[call.index("--location") + 1] == "Main St" and "--all-day" not in call


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"title": "", "start": "2026-10-09T15:00"}, "title is required"),
        ({"title": "x", "start": "soon"}, "ISO 8601"),
        ({"title": "x", "start": "2026-10-09T15:00", "end": "2026-10-09T14:00"}, "before start"),
        ({"title": "x" * 201, "start": "2026-10-09T15:00"}, "too long"),
        ({"title": "a\x07b", "start": "2026-10-09T15:00"}, "control characters"),
        ({"title": "x", "start": "2026-10-01", "end": "2026-12-01"}, "31 days"),
    ],
)
def test_create_event_rejects_bad_arguments(tmp_path: Path, args: dict[str, Any], error: str) -> None:
    exe = _fake(tmp_path)
    with pytest.raises(ToolError, match=error):
        asyncio.run(calendar_mac.create_event(args, helper=exe))
    assert not (tmp_path / "calls.jsonl").exists()


def _brain(tmp_path: Path) -> Any:
    from claude_voice.brain import Brain
    from claude_voice.config import ClaudeConfig
    from claude_voice.rules import Rules
    from claude_voice.tools import Timers, build_server

    async def never(spoken: str, detail: str) -> bool | None:
        return None

    server, names = build_server(Timers(lambda label: None))
    return Brain(ClaudeConfig(), never, server, names, Rules(tmp_path / "r.json"), lambda k, t: None)


def test_reading_is_preapproved_and_adding_always_asks(tmp_path: Path) -> None:
    for policy in ("strict", "settings", "settings_no_hooks"):
        b = _brain(tmp_path)
        b.cfg.approval_policy = policy
        perms = json.loads(b._options().settings)["permissions"]
        assert "mcp__voice_app__calendar_events" in perms["allow"]
        assert "mcp__voice_app__calendar_create_event" in perms["ask"]
        assert "mcp__voice_app__calendar_create_event" not in perms["allow"]


def test_adding_an_event_is_spoken_plainly_and_never_saved_as_always(tmp_path: Path) -> None:
    async def go() -> tuple[list[str], list[dict[str, Any]]]:
        asked: list[str] = []
        b = _brain(tmp_path)

        async def confirm(spoken: str, detail: str) -> bool | str | None:
            asked.append(spoken)
            return "always"

        b.confirm = confirm
        args = {"title": "Dentist", "start": "2026-10-09T15:00", "calendar": "Home"}
        assert await b._ask("mcp__voice_app__calendar_create_event", args) is True
        assert await b._ask("mcp__voice_app__calendar_create_event", args) is True
        return asked, b.rules.listing()

    asked, rules = asyncio.run(go())
    assert asked == ["Add Dentist, Friday October 9, 3:00 PM, to Home?"] * 2
    assert rules == []
