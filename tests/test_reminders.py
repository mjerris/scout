"""Reminders tools: argument checks, the helper protocol (through a fake vcal that
records its arguments), first-use reminders access, and the voice approval."""

import asyncio
import datetime as dt
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from scout import reminders_mac
from scout.brain import describe_tool
from scout.mac import ToolError

ITEMS: dict[str, Any] = {
    "reminders": [
        {
            "id": "r1",
            "title": "Pay rent",
            "list": "Home",
            "completed": False,
            "due": "2026-10-07",
        },
        {
            "id": "r2",
            "title": "Call Sam",
            "list": "Home",
            "completed": False,
            "due": "2026-10-08T17:00:00-04:00",
            "notes": "about\nthe lease",
        },
        {"id": "r3", "title": "Milk", "list": "Shopping", "completed": False},
    ],
    "total": 3,
}
NOW = dt.datetime(2026, 10, 8, 14, 37, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def _fake(tmp_path: Path, status: str = "full_access", granted: bool = True, reply: Any = None) -> Path:
    log = tmp_path / "calls.jsonl"
    exe = tmp_path / "vcal"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "cmd = sys.argv[1]\n"
        f"if cmd == 'status': print(json.dumps({{'status': {status!r}}}))\n"
        f"elif cmd == 'request': print(json.dumps({{'granted': {granted!r}, 'status': 'x'}}))\n"
        f"else: print({json.dumps(json.dumps(reply or ITEMS))})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def _calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_reminders_are_read_and_formatted(tmp_path: Path) -> None:
    exe = _fake(tmp_path)
    data = asyncio.run(reminders_mac.reminder_data("home", True, "2026-10-09", helper=exe))
    out = reminders_mac.format_reminders(data, NOW)
    assert "Pay rent [Home] (due Wed Oct 7, overdue) (id r1, due 2026-10-07)" in out
    assert (
        "Call Sam [Home] (due Thu Oct 8, 5:00 PM) (id r2, due 2026-10-08T17:00:00-04:00)\n  notes: about the lease"
        in out
    )
    assert "Milk [Shopping] (id r3)" in out
    call = _calls(tmp_path)[-1]
    assert call[0] == "reminders" and call[call.index("--list") + 1] == "home"
    assert "--include-completed" in call
    assert call[call.index("--due-before") + 1].startswith("2026-10-09T00:00:00")


def test_overdue_is_by_day_for_date_only_reminders() -> None:
    r = {"id": "x", "due": "2026-10-08"}
    assert not reminders_mac.is_overdue(r, NOW)  # due today, still today
    assert reminders_mac.is_overdue({"id": "x", "due": "2026-10-08T09:00:00-04:00"}, NOW)
    assert not reminders_mac.is_overdue({"id": "x", "due": "2026-10-07", "completed": True}, NOW)


def test_first_use_asks_for_reminders_access_not_calendars(tmp_path: Path) -> None:
    exe = _fake(tmp_path, status="not_determined", granted=True)
    asyncio.run(reminders_mac.reminders(helper=exe))
    assert _calls(tmp_path)[:2] == [["status", "--reminders"], ["request", "--reminders"]]


def test_refused_reminders_access_names_the_reminders_pane(tmp_path: Path) -> None:
    exe = _fake(tmp_path, status="not_determined", granted=False)
    with pytest.raises(ToolError, match=r"Reminders access wasn.t allowed.*Privacy and Security, Reminders"):
        asyncio.run(reminders_mac.reminders(helper=exe))
    exe = _fake(tmp_path, status="denied")
    with pytest.raises(ToolError, match="Reminders access is denied"):
        asyncio.run(reminders_mac.lists(helper=exe))


def test_lists(tmp_path: Path) -> None:
    exe = _fake(
        tmp_path,
        reply={
            "lists": [
                {"title": "Reminders", "account": "iCloud", "writable": True, "default": True},
                {"title": "Shared", "account": "Exchange", "writable": False, "default": False},
            ]
        },
    )
    assert asyncio.run(reminders_mac.lists(helper=exe)) == (
        "Reminders (iCloud), default\nShared (Exchange), read-only"
    )


def test_add_passes_checked_arguments(tmp_path: Path) -> None:
    exe = _fake(
        tmp_path,
        reply={
            "created": True,
            "reminder": {"id": "n", "title": "Milk", "list": "Shopping", "completed": False},
        },
    )
    out = asyncio.run(reminders_mac.add({"title": "Milk", "list": "shopping"}, helper=exe))
    assert out == "Added Milk to Shopping."
    assert _calls(tmp_path)[-1] == ["reminder-add", "--title", "Milk", "--list", "shopping"]


def test_add_with_a_due_time_or_day(tmp_path: Path) -> None:
    exe = _fake(
        tmp_path,
        reply={
            "created": True,
            "reminder": {"id": "n", "title": "Call Sam", "list": "Home", "due": "2026-10-09T15:00:00-04:00"},
        },
    )
    out = asyncio.run(
        reminders_mac.add({"title": "Call Sam", "due": "2026-10-09T15:00", "notes": "n"}, helper=exe)
    )
    assert out == "Added Call Sam to Home, due Fri Oct 9, 3:00 PM."
    call = _calls(tmp_path)[-1]
    assert call[call.index("--due") + 1].startswith("2026-10-09T15:00:00") and "--notes" in call
    asyncio.run(reminders_mac.add({"title": "Bins", "due": "2026-10-09"}, helper=exe))
    call = _calls(tmp_path)[-1]
    assert call[call.index("--due") + 1] == "2026-10-09"  # a day, no time: no alert


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"title": ""}, "title is required"),
        ({"title": "x", "due": "soon"}, "ISO 8601"),
        ({"title": "x" * 201}, "too long"),
        ({"title": "a\x07b"}, "control characters"),
    ],
)
def test_add_rejects_bad_arguments(tmp_path: Path, args: dict[str, Any], error: str) -> None:
    exe = _fake(tmp_path)
    with pytest.raises(ToolError, match=error):
        asyncio.run(reminders_mac.add(args, helper=exe))
    assert not (tmp_path / "calls.jsonl").exists()


def test_complete_sends_the_id_and_the_title_to_check(tmp_path: Path) -> None:
    exe = _fake(
        tmp_path, reply={"completed": True, "reminder": {"id": "r3", "title": "Milk", "list": "Shopping"}}
    )
    assert asyncio.run(reminders_mac.complete({"id": "r3", "title": "Milk"}, helper=exe)) == (
        "Marked Milk done in Shopping."
    )
    assert _calls(tmp_path)[-1] == ["reminder-complete", "--id", "r3", "--title", "Milk"]
    with pytest.raises(ToolError, match="id and title are required"):
        asyncio.run(reminders_mac.complete({"id": "r3"}, helper=exe))


def test_helper_errors_are_reported(tmp_path: Path) -> None:
    exe = _fake(tmp_path, reply={"error": 'reminder r3 is "Milk", not "Eggs"'})
    with pytest.raises(ToolError, match='not "Eggs"'):
        asyncio.run(reminders_mac.complete({"id": "r3", "title": "Eggs"}, helper=exe))


def test_adding_and_completing_are_asked_plainly() -> None:
    assert describe_tool("mcp__voice_app__reminder_add", {"title": "milk", "list": "Shopping"})[0] == (
        "Add milk to Shopping?"
    )
    assert describe_tool("reminder_add", {"title": "Call Sam", "due": "2026-10-09T15:00"})[0] == (
        "Add Call Sam to Reminders, due Friday October 9, 3:00 PM?"
    )
    assert describe_tool("reminder_add", {"title": "Bins", "due": "2026-10-09"})[0] == (
        "Add Bins to Reminders, due Friday October 9?"
    )
    spoken, detail = describe_tool("mcp__voice_app__reminder_complete", {"id": "r3", "title": "Milk"})
    assert spoken == "Mark Milk done?" and "id r3" in detail


def test_reminder_writes_always_ask_and_reading_is_preapproved(tmp_path: Path) -> None:
    from test_calendar import _brain

    async def go() -> tuple[list[str], list[dict[str, Any]]]:
        asked: list[str] = []
        b = _brain(tmp_path)

        async def confirm(spoken: str, detail: str) -> bool | str | None:
            asked.append(spoken)
            return "always"

        b.confirm = confirm
        for _ in range(2):
            assert await b._ask("mcp__voice_app__reminder_add", {"title": "milk", "list": "Shopping"}) is True
        return asked, b.rules.listing()

    asked, rules = asyncio.run(go())
    assert asked == ["Add milk to Shopping?"] * 2 and rules == []
    perms = json.loads(_brain(tmp_path)._options().settings)["permissions"]
    assert "mcp__voice_app__reminders_list" in perms["allow"]
    assert "mcp__voice_app__calendar_free" in perms["allow"]
    for name in ("reminder_add", "reminder_complete"):
        assert f"mcp__voice_app__{name}" in perms["ask"]
        assert f"mcp__voice_app__{name}" not in perms["allow"]
