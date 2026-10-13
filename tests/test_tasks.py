"""Scout's tasks (tasks.py, taskfiles.py): files, order, tools and the Reminders mirror,
with Reminders faked (nothing reaches the real app) and synthetic tasks only."""

import asyncio
import datetime as dt
import os
import time
from pathlib import Path
from typing import Any

import pytest

from scout import index, reminders_mac, taskfiles, tasks
from scout.mac import ToolError

TODAY = dt.date(2026, 10, 13)


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index) and s.journal_root is not None
    return s


class FakeReminders:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.done_ids: set[str] = set()
        self.asked: list[list[str]] = []
        self.up = True

    async def available(self, helper: Any = None) -> bool:
        return self.up

    async def add_data(self, a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        assert mirror  # the task mirror only ever writes to the Mac's Reminders
        self.added.append(a)
        return {"id": f"r{len(self.added)}", "title": a["title"], "list": a["list"]}

    async def complete(self, a: dict[str, Any], helper: Any = None, mirror: bool = False) -> str:
        assert mirror
        self.completed.append(a)
        return "ok"

    async def update(self, a: dict[str, Any], helper: Any = None) -> dict[str, Any]:
        self.updated.append(a)
        return {"id": a["id"]}

    async def states(self, ids: list[str], helper: Any = None) -> dict[str, Any]:
        self.asked.append(list(ids))
        known = {f"r{i + 1}" for i in range(len(self.added))}
        return {"reminders": [{"id": i, "completed": i in self.done_ids} for i in ids if i in known],
                "missing": [i for i in ids if i not in known]}  # fmt: skip


@pytest.fixture
def rem(monkeypatch: pytest.MonkeyPatch) -> FakeReminders:
    fake = FakeReminders()
    for name in ("available", "add_data", "complete", "update", "states"):
        monkeypatch.setattr(reminders_mac, name, getattr(fake, name))
    monkeypatch.setattr(tasks, "_people", lambda: ["Pat"])
    return fake


def test_a_task_file_round_trips(tmp_path: Path) -> None:
    p = taskfiles.write(tmp_path, "Send the travel budget to Pat", {"due": "2026-10-20", "source": "granola:not_x"},
                        "Pat asked before Monday's review.")  # fmt: skip
    assert p.name == "0001-send-the-travel-budget-to-pat.md"
    title, fields, context = taskfiles.read(p)
    assert title == "Send the travel budget to Pat" and context == "Pat asked before Monday's review."
    assert fields["status"] == "open" and fields["owner"] == "me" and fields["due"] == "2026-10-20"
    taskfiles.update(p, {"status": "done", "due": None, "title": "Send budget"}, "Sent Friday.")
    title, fields, context = taskfiles.read(p)
    assert title == "Send budget" and fields["status"] == "done" and "due" not in fields
    assert context == "Pat asked before Monday's review.\n\nSent Friday."
    [item] = taskfiles.parse(p)
    assert item["id"] == "task:1" and item["meta"]["n"] == 1 and item["who"] == ""


def test_the_order_is_plain_code_and_says_why() -> None:
    def t(n: int, **f: Any) -> tasks.Task:
        return tasks.Task(n, f.pop("title", f"task {n}"), "", {"status": "open", "owner": "me", **f})

    rows = tasks.ordered(
        [
            t(1, due="2026-10-30"),
            t(2, due="2026-10-12"),  # overdue
            t(3, importance="high"),
            t(4, effort="small"),
            t(5, owner="Sam", due="2026-10-30"),  # waiting on someone, not due yet
            t(6, pin="top"),
            t(7, status="done", due="2026-10-01"),
            t(8, title="Reply to Pat about the vendor"),
        ],
        TODAY,
        people=["Pat"],
    )
    assert [r[0].n for r in rows] == [6, 2, 3, 8, 1, 4, 5]  # 1 and 4 tie; the one with a date first
    why = {r[0].n: r[2] for r in rows}
    assert why[6] == "you made it top priority" and why[2] == "overdue since Oct 12"
    assert why[8] == "for someone you've told me about" and why[4] == "a quick one"
    assert [r[0].n for r in tasks.ordered([t(1), t(5, owner="Sam")], TODAY, who="waiting")] == [5]


def test_stale_undated_tasks_sink() -> None:
    old = tasks.Task(1, "old", "", {"status": "open", "found": "2026-08-01 09:00"})
    new = tasks.Task(2, "new", "", {"status": "open", "found": "2026-10-12 09:00"})
    assert [r[0].n for r in tasks.ordered([old, new], TODAY)] == [2, 1]


def test_people_come_from_remembered_facts() -> None:
    assert tasks.people_from_facts(
        ["Pat is my manager", "My dentist is Dr. Okafor", "The RV is 36 feet"]
    ) == ["Pat", "Okafor"]


def test_add_list_done_and_the_reminders_mirror(store: index.Index, rem: FakeReminders) -> None:
    async def go() -> list[str]:
        a = await tasks.add_text(
            {"title": "Send the budget to Pat", "due": "2026-10-20", "importance": "high"}
        )
        b = await tasks.add_text({"title": "Vendor quote", "owner": "Sam", "due": "2026-10-15"})
        listed = await tasks.list_text({})
        done = await tasks.done({"id": 1})
        after = await tasks.list_text({"who": "mine"})
        return [a, b, listed, done, after]

    a, b, listed, done, after = asyncio.run(go())
    assert a == "Added task 1: Send the budget to Pat, due 2026-10-20."
    assert b == "Added task 2: Vendor quote (waiting on Sam), due 2026-10-15."
    assert listed.splitlines()[0].startswith("1. Send the budget to Pat (due 2026-10-20)")
    assert "2. Vendor quote [waiting on Sam]" in listed
    assert done == "Done: task 1, Send the budget to Pat." and after == "No open tasks."
    assert [(x["list"], x["title"]) for x in rem.added] == [
        ("Scout", "Send the budget to Pat"),
        ("Waiting On", "Sam: Vendor quote"),
    ]
    assert rem.completed == [{"id": "r1", "title": "Send the budget to Pat"}]
    t2 = store.get("task:2")
    assert t2 is not None and t2.meta["reminder"] == "r2"


def test_done_in_reminders_finishes_the_task(store: index.Index, rem: FakeReminders) -> None:
    asyncio.run(tasks.add({"title": "Renew registration"}))
    rem.done_ids.add("r1")
    assert asyncio.run(tasks.sync_from_reminders()) == 1
    t = store.get("task:1")
    assert t is not None and t.meta["status"] == "done"
    assert asyncio.run(tasks.sync_from_reminders()) == 0


def test_update_pin_and_why(store: index.Index, rem: FakeReminders) -> None:
    asyncio.run(tasks.add({"title": "Call the insurer", "context": "Claim 88231.", "source": "mail:42"}))
    assert (
        asyncio.run(tasks.update({"id": 1, "pin": "top", "due": "2026-10-14"}))
        == "Updated task 1: Call the insurer."
    )
    t = store.get("task:1")
    assert t is not None and t.meta["pin"] == "top" and t.meta["due"] == "2026-10-14"
    asyncio.run(tasks.update({"id": 1, "pin": "none"}))
    t = store.get("task:1")
    assert t is not None and "pin" not in t.meta
    why = asyncio.run(tasks.why_text({"id": 1}))
    assert "source: mail:42" in why and "Claim 88231." in why


def test_bad_input_is_refused(store: index.Index, rem: FakeReminders) -> None:
    for bad in ({"title": ""}, {"title": "x", "importance": "huge"}, {"title": "x", "due": "someday-ish"}):
        with pytest.raises(ToolError):
            asyncio.run(tasks.add(bad))
    with pytest.raises(ToolError, match="no task 9"):
        asyncio.run(tasks.done({"id": 9}))
    assert rem.added == []


def test_a_hand_edited_task_reaches_the_list(store: index.Index, rem: FakeReminders) -> None:
    asyncio.run(tasks.add({"title": "Fix the awning"}))
    root = store.journal_root
    assert root is not None
    p = next((root / "tasks").glob("0001-*.md"))
    p.write_text(p.read_text().replace("- owner: me", "- owner: me\n- due: 2026-10-01"))
    os.utime(p, (time.time() + 5, time.time() + 5))
    store.sync_files()
    assert "due 2026-10-01" in asyncio.run(tasks.list_text({}))


def test_without_reminders_access_tasks_still_work(
    store: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        raise ToolError("Reminders access was refused")

    async def up(helper: Any = None) -> bool:
        return True

    monkeypatch.setattr(reminders_mac, "available", up)
    monkeypatch.setattr(reminders_mac, "add_data", refuse)
    monkeypatch.setattr(tasks, "_people", lambda: [])
    out = asyncio.run(tasks.add_text({"title": "Order filters"}))
    t = store.get("task:1")
    assert out.startswith("Added task 1") and t is not None and "reminder" not in t.meta


def test_the_briefing_names_the_top_tasks_and_follow_ups() -> None:
    from scout import briefing

    def t(n: int, **f: Any) -> tasks.Task:
        return tasks.Task(n, f.pop("title"), "", {"status": "open", "owner": "me", **f})

    rows = tasks.ordered(
        [
            t(1, title="Send Pat the budget", due="2026-10-13"),
            t(2, title="Call the insurer", importance="high"),
            t(3, title="Order filters", effort="small"),
            t(4, title="Renew registration", due="2026-10-01"),
            t(5, title="Old thing", due="2026-10-02"),
            t(6, title="the vendor quote", owner="Sam", due="2026-10-09"),
        ],
        TODAY,
    )
    said = briefing.tasks_part(rows, TODAY)
    assert said.startswith("Your top tasks: Renew registration, overdue since Oct 1;")
    assert "Sam owes you the vendor quote, due Friday, October 9; maybe follow up." in said
    assert briefing.tasks_part([], TODAY) == ""
