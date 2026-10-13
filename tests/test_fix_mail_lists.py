"""Notes and Scout's own lists stay consistent when the memory service is down, and in
auto mode items kept by Scout while Reminders was unavailable stay visible after it
comes back. Fake vcal helper and synthetic items only."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from scout import appdb, index, locallists, mac, memory_client, notes, reminders_mac
from scout.mac import ToolError


@pytest.fixture(autouse=True)
def _no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may reach osascript: Notes refuses, as if not allowed."""

    async def run(*argv: str, timeout: float = 10.0) -> str:
        raise ToolError("Not authorized to send Apple events to Notes. (-1743)")

    monkeypatch.setattr(mac, "_run", run)


class Down:
    def __getattr__(self, name: str) -> Any:
        def fail(*a: Any, **k: Any) -> Any:
            raise ToolError(memory_client.DOWN)

        return fail


def _rows(sql: str) -> list[tuple[Any, ...]]:
    return appdb.default().sql(sql)


# --- #31 ----------------------------------------------------------------------------------


def test_a_note_is_not_half_saved_when_the_index_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    store = index.default()
    monkeypatch.setattr(index, "default", lambda: Down())
    with pytest.raises(ToolError, match="memory service"):
        asyncio.run(notes.add("water filter is due"))
    assert _rows("SELECT COUNT(*) FROM notes") == [(0,)]  # a retry won't make a duplicate
    monkeypatch.setattr(index, "default", lambda: store)
    asyncio.run(notes.add("water filter is due"))
    assert [n["title"] for n in asyncio.run(notes.find("filter"))] == ["water filter is due"]
    monkeypatch.setattr(index, "default", lambda: Down())
    with pytest.raises(ToolError):
        asyncio.run(notes.delete({"id": 1}))
    assert _rows("SELECT COUNT(*) FROM notes") == [(1,)]  # still there and still findable


def test_a_list_item_is_not_half_added_or_half_done_when_the_index_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = index.default()
    monkeypatch.setattr(index, "default", lambda: Down())
    with pytest.raises(ToolError, match="memory service"):
        asyncio.run(locallists.add({"title": "Milk", "list": "Shopping"}))
    assert _rows("SELECT COUNT(*) FROM list_items") == [(0,)]
    monkeypatch.setattr(index, "default", lambda: store)
    asyncio.run(locallists.add({"title": "Milk", "list": "Shopping"}))
    rid = asyncio.run(locallists.reminder_data())["reminders"][0]["id"]
    monkeypatch.setattr(index, "default", lambda: Down())
    with pytest.raises(ToolError, match="memory service"):
        asyncio.run(locallists.complete({"id": rid, "title": "Milk"}))
    monkeypatch.setattr(index, "default", lambda: store)
    assert asyncio.run(locallists.complete({"id": rid, "title": "Milk"})) == "Marked Milk done in Shopping."


# --- #29 ----------------------------------------------------------------------------------


def _vcal(tmp_path: Path, name: str, status: str, reply: Any) -> Path:
    exe = tmp_path / name
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "cmd = sys.argv[1]\n"
        f"if cmd == 'status': print(json.dumps({{'status': {status!r}}}))\n"
        f"else: print({json.dumps(json.dumps(reply))})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_items_kept_by_scout_while_reminders_was_down_stay_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reminders_mac, "_backend", lambda: "auto")
    denied = _vcal(tmp_path, "denied", "denied", {})
    ok = _vcal(
        tmp_path,
        "ok",
        "full_access",
        {
            "reminders": [{"id": "r1", "title": "Pay rent", "list": "Home", "completed": False}],
            "total": 1,
            "lists": [{"title": "Home"}],
        },
    )

    async def go() -> tuple[str, str, list[str]]:
        added = await reminders_mac.add({"title": "Milk", "list": "Shopping"}, helper=denied)
        reminders_mac._fallback_until = 0.0  # ten minutes later: Reminders works again
        return added, await reminders_mac.reminders(helper=ok), await reminders_mac.list_names(helper=ok)

    added, listed, names = asyncio.run(go())
    assert added == "Added Milk to Shopping."
    assert "Pay rent [Home] (id r1)" in listed and "Milk [Shopping] (id scout:1)" in listed
    assert names == ["Home", "Shopping"]
    done = asyncio.run(reminders_mac.complete({"id": "scout:1", "title": "Milk"}, helper=ok))
    assert done == "Marked Milk done in Shopping."
    assert "Milk" not in asyncio.run(reminders_mac.reminders(helper=ok))


def test_a_reminders_item_is_not_sent_to_scouts_lists_during_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reminders_mac, "_backend", lambda: "auto")
    denied = _vcal(tmp_path, "denied", "denied", {})
    with pytest.raises(ToolError, match="Reminders can't be reached right now"):
        asyncio.run(
            reminders_mac.complete({"id": "x-apple-reminder://r1", "title": "Pay rent"}, helper=denied)
        )
