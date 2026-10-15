"""Refs on what Scout says (refs.py), and the chain they make possible: the pulse names a
task found in a meeting, the user says "give me a link to that", and the meeting's link
goes into the Scout reminders list. Synthetic records only; the store's resolve() is
faked to the links contract where the test is about the conversation side."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_routing import make

from scout import index, journal, local_intents, privacy, pulse, refs, reminders_mac, tasks, tier1, tools
from scout.tools import Timers

TZ = dt.timezone(dt.timedelta(hours=-4))
NOW = dt.datetime(2026, 10, 15, 8, 0, tzinfo=TZ)
SYNC_TS = dt.datetime(2026, 10, 13, 14, 0, tzinfo=TZ).timestamp()
GRANOLA_URL = "https://notes.granola.ai/d/n1"
MAIL_URL = "message://%3Cq3@example.com%3E"


def _hit(id: str, kind: str, title: str, ts: float = SYNC_TS, who: str = "", **meta: Any) -> index.Hit:
    return index.Hit(id, kind, title, "", who, ts, meta)


class FakeStore:
    """index.Index's get() and the contract's resolve()."""

    def __init__(self) -> None:
        self.hits = {
            "task:7": _hit(
                "task:7", "task", "Send the revised vendor quote", owner="Pat Doe", **{"from": "meeting"}
            ),
            "task:8": _hit("task:8", "task", "Book flights"),
            "task:9": _hit(
                "task:9", "task", "Renew the lease", reminder="R-9", reminder_url=MAIL_URL, **{"from": "mail"}
            ),
            "granola:n1": _hit("granola:n1", "meeting", "Vendor sync", who="Pat Doe"),
            "mail:41": _hit("mail:41", "mail", "Lease renewal", who="Sam Lee <sam@example.com>"),
        }
        self.resolved = {
            "task:7": {
                "id": "task:7", "kind": "task", "title": "Send the revised vendor quote", "url": GRANOLA_URL,
                "page": "http://scout.test/r/task%3A7", "private": True,
                "related": [{"id": "granola:n1", "rel": "found_in", "dir": "out", "kind": "meeting",
                             "title": "Vendor sync", "url": GRANOLA_URL}],
            },
            "task:8": {
                "id": "task:8", "kind": "task", "title": "Book flights", "url": "http://scout.test/r/task%3A8",
                "page": "http://scout.test/r/task%3A8", "private": False, "related": [],
            },
            "task:9": {
                "id": "task:9", "kind": "task", "title": "Renew the lease", "url": MAIL_URL,
                "page": "http://scout.test/r/task%3A9", "private": True,
                "related": [{"id": "mail:41", "rel": "found_in", "dir": "out", "kind": "mail",
                             "title": "Lease renewal", "url": MAIL_URL}],
            },
            "granola:n1": {
                "id": "granola:n1", "kind": "meeting", "title": "Vendor sync", "url": GRANOLA_URL,
                "page": None, "private": True,
                "related": [{"id": "task:7", "rel": "found_in", "dir": "in", "kind": "task",
                             "title": "Send the revised vendor quote", "url": GRANOLA_URL}],
            },
        }  # fmt: skip

    def get(self, id: str) -> index.Hit | None:
        return self.hits.get(id)

    def resolve(self, id: str) -> dict[str, Any] | None:
        return self.resolved.get(id)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(refs, "_store", lambda: s)
    refs._sent.clear()
    return s


@pytest.fixture
def added(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Reminders written (the Mac's Reminders, faked)."""
    out: list[dict[str, Any]] = []

    async def add_data(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        out.append({**a, "mirror": mirror})
        return {"id": "R-new", "title": a["title"], "list": a.get("list")}

    monkeypatch.setattr(reminders_mac, "add_data", add_data)
    return out


def _task(n: int, title: str, **fields: Any) -> tasks.Task:
    return tasks.Task(n, title, "", fields)


def _pulse() -> pulse.Pulse:
    async def events(start: str, end: str) -> dict[str, Any]:
        return {"events": []}

    async def reminders() -> dict[str, Any]:
        return {"reminders": []}

    async def task_list() -> list[tasks.Task]:
        return [
            _task(
                7, "Send the revised vendor quote", owner="Pat Doe", due="2026-10-09", **{"from": "meeting"}
            ),
            _task(8, "Book flights", due="2026-10-15"),
        ]

    return pulse.Pulse(10, 15, events, reminders, task_list)


# --- the chain, end to end ------------------------------------------------------------------


def test_pulse_task_then_link_to_that(store: FakeStore, added: list[dict[str, Any]]) -> None:
    """The bug this fixes: the 8:00 pulse named a task found in a meeting; "give me a link
    to that" went to Claude with only the words. Now the sentence carries task:7."""

    async def go() -> tuple[list[str], list[str]]:
        r: Any = make()
        said = await _pulse().check(NOW)
        assert [refs.refs_of(s) for s in said] == [("task:8",), ("task:7",)]
        for s in said:
            await r.a.announce(s, "pulse")
        assert await r.a._answer_locally("give me a link to that")
        return r.spk.said, list(r.a.said.items[-1].refs)

    spoken, last = asyncio.run(go())
    assert (
        spoken[-1]
        == "It's from your meeting Vendor sync, on October 13. I put the link in your Scout reminders."
    )
    assert last == ["task:7"]  # "where did that come from" next still knows
    assert added == [
        {
            "title": "Link: Vendor sync",
            "list": "Scout",
            "url": GRANOLA_URL,
            "mirror": True,
        }
    ]


def test_where_did_that_come_from_and_say_that_again_keep_the_refs(
    store: FakeStore, added: list[dict[str, Any]]
) -> None:
    async def go() -> list[str]:
        r: Any = make()
        await r.a.announce(refs.Spoken("Pat Doe owes you the vendor quote.", ["task:7"], True), "pulse")
        assert await r.a._answer_locally("say that again")
        assert await r.a._answer_locally("what meeting was that from")
        assert await r.a._answer_locally("send me that link")
        assert await r.a._answer_locally("send me that link")  # once is enough
        return r.spk.said

    spoken = asyncio.run(go())
    assert spoken[-4:] == [
        "Pat Doe owes you the vendor quote.",
        "It's from your meeting Vendor sync, on October 13.",
        "It's from your meeting Vendor sync, on October 13. I put the link in your Scout reminders.",
        "It's from your meeting Vendor sync, on October 13. The link is already in your Scout reminders.",
    ]
    assert len(added) == 1


def test_a_task_with_its_own_reminder_points_there(store: FakeStore, added: list[dict[str, Any]]) -> None:
    async def go() -> str:
        r: Any = make()
        await r.a.announce(refs.Spoken("Due today: Renew the lease.", ["task:9"], True), "pulse")
        assert await r.a._answer_locally("give me a link to that")
        return r.spk.said[-1]

    said = asyncio.run(go())
    assert (
        said
        == "It's from an email from Sam Lee about Lease renewal, on October 13. It's in that task's reminder, on your phone."
    )
    assert added == []


@pytest.mark.parametrize(
    "first",
    [
        refs.Spoken("It's 8 o'clock."),  # nothing said had a record
        refs.Spoken(
            "Due today: Send the revised vendor quote and Book flights.", ["task:7", "task:8"]
        ),  # which?
    ],
)
def test_no_single_ref_goes_to_claude(store: FakeStore, added: list[dict[str, Any]], first: str) -> None:
    async def go() -> bool:
        r: Any = make()
        await r.a.announce(first, "pulse")
        return await r.a._answer_locally("give me a link to that")

    assert asyncio.run(go()) is False  # never invented: Claude, with the refs in its context
    assert added == []


def test_the_one_before_counts_when_the_last_had_none(store: FakeStore, added: list[dict[str, Any]]) -> None:
    async def go() -> str:
        r: Any = make()
        await r.a.announce(refs.Spoken("Pat Doe owes you the vendor quote.", ["task:7"], True), "pulse")
        await r.a.announce("Heads up: standup in 10 minutes.", "pulse")
        assert await r.a._answer_locally("where did that come from")
        return r.spk.said[-1]

    assert asyncio.run(go()) == "It's from your meeting Vendor sync, on October 13."


def test_recall_answer_then_where_did_that_come_from(store: FakeStore) -> None:
    class Model:
        ready = True

        async def complete(self, system: str, user: str, max_tokens: int) -> str:
            return json.dumps(
                {"found": True, "source": 1, "quote": "Used 10W-30", "answer": "You used 10W-30."}
            )

    class Decider:
        ready = True

        async def decide(self, request: str, now: Any) -> dict[str, Any]:
            return {"tool": "recall", "args": {"query": "generator oil", "question": request}}

    real = index.default()
    assert isinstance(real, index.Index)
    real.put("note:1", "note", "Generator oil change", "Changed the generator oil. Used 10W-30, 1.8 quarts.",
             ts=SYNC_TS)  # fmt: skip
    store.hits["note:1"] = real.get("note:1")  # type: ignore[assignment]
    store.resolved["note:1"] = {"id": "note:1", "kind": "note", "title": "Generator oil change",
                                "url": "http://scout.test/r/note%3A1", "private": False, "related": []}  # fmt: skip

    async def go() -> list[str]:
        r: Any = make()
        r.a.privacy = privacy.configure("balanced", Model())
        r.a.tier1 = Decider()
        try:
            assert await r.a._answer_locally("what oil did I use in the generator")
            assert r.a.said.items[-1].refs == ("note:1",)
            r.a.tier1 = None
            assert await r.a._answer_locally("where did that come from")
            return r.spk.said
        finally:
            privacy.configure("balanced")

    said = asyncio.run(go())
    assert said[-2:] == ["You used 10W-30.", "It's from your note Generator oil change, on October 13."]


@pytest.mark.parametrize("mode", ["strict", "balanced"])
def test_claude_context_lists_refs_with_titles_per_privacy_mode(store: FakeStore, mode: str) -> None:
    async def go() -> str:
        r: Any = make()
        r.a.privacy = privacy.configure(mode)
        try:
            await r.a.announce(
                refs.Spoken("Pat Doe owes you Send the revised vendor quote.", ["task:7"], True), "pulse"
            )
            return await r.a._with_context("send the link for that to my phone please")
        finally:
            privacy.configure("balanced")

    context = asyncio.run(go())
    assert "the pulse check-in" in context and "-> refs: task:7 (" in context
    if mode == "strict":
        assert "task:7 (a task from a meeting)" in context
        assert "vendor quote" not in context  # neither the sentence nor the title
    else:
        assert "task:7 (a task: Send the revised vendor quote)" in context


def test_claude_turn_refs_come_from_its_tools(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    r: Any = make()
    r.a.cfg.claude.local_first = False
    r.a.cfg.claude.request_context = False

    async def ask(text: str) -> AsyncIterator[tuple[str, Any]]:
        yield "tool", ("mcp__voice_app__tasks_list", {"who": "waiting"})
        # what build_server's on_refs does with the tool's result
        r.a._turn_refs.extend(
            refs.from_tool(
                "mcp__voice_app__tasks_list", {}, "7. Send the revised vendor quote [waiting on Pat Doe]"
            )
        )
        yield "text", "Pat owes you the revised vendor quote."

    monkeypatch.setattr(r.a.brain, "ask", ask)

    async def go() -> tuple[refs.Said, str]:
        r.a.start_turn("what am I waiting on from Pat")
        await r.a._turn
        r.a.cfg.claude.request_context = True
        return r.a.said.items[-1], await r.a._with_context("text me that link")

    last, context = asyncio.run(go())
    assert last.refs == ("task:7",) and last.request == "what am I waiting on from Pat"
    assert "answering 'what am I waiting on from Pat'" in context and "task:7 (a task" in context


def test_build_server_reports_each_tools_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real = tools.create_sdk_mcp_server

    def capture(name: str, tools: list[Any]) -> Any:
        captured.update({t.name: t for t in tools})
        return real(name, tools=tools)

    monkeypatch.setattr(tools, "create_sdk_mcp_server", capture)
    got: list[list[str]] = []
    tools.build_server(Timers(lambda label: None), on_refs=got.append)

    async def task_why(a: dict[str, Any]) -> str:
        return "Task 7: Send the revised vendor quote\nsource: meeting:n1"

    monkeypatch.setattr(tasks, "why_text", task_why)
    out = asyncio.run(captured["task_why"].handler({"id": 7}))
    assert "Task 7" in out["content"][0]["text"] and got == [["task:7"]]


@pytest.mark.parametrize(
    ("name", "args", "output", "want"),
    [
        ("tasks_list", {}, "7. Vendor quote [waiting on Pat]: overdue\n12. Book flights: due today", ["task:7", "task:12"]),
        ("mail_recent", {}, "(untrusted)\nid 41: unread, Oct 13, from Sam: Lease (received x)\n  gist", ["mail:41"]),
        ("mail_read", {"id": 41}, "From: Sam", ["mail:41"]),
        ("notes_find", {}, "Note 3 (2026-10-01): Wifi\nbody", ["note:3"]),
        ("local_search", {}, "[meeting] 2026-10-13 14:00: Vendor sync (ref granola:n1)\n[slack] x (ref slack:C1:1.2)", ["granola:n1", "slack:C1:1.2"]),
        ("task_done", {"id": "7"}, "Done.", ["task:7"]),
        ("weather", {}, "Sunny, id 41: nothing", []),
    ],
)  # fmt: skip
def test_refs_from_tool_results(name: str, args: dict[str, Any], output: str, want: list[str]) -> None:
    assert refs.from_tool(f"mcp__voice_app__{name}", args, output) == want


def test_chunk_ids_are_their_records() -> None:
    assert refs.clean(["granola:n1#3", "granola:n1", "slack:C1:1.2#2", "not an id", ""]) == (
        "granola:n1",
        "slack:C1:1.2",
    )


# --- the tools (room, phone, other sessions) -----------------------------------------------


def test_open_link_in_strict_mode_gives_the_link_not_the_title(store: FakeStore) -> None:
    privacy.configure("strict")
    try:
        refs.log_of().add(
            refs.Said(time.time(), "pulse", "Pat Doe owes you the vendor quote.", ("task:7",), True)
        )
        out = asyncio.run(refs.open_link({"about": "that"}))
    finally:
        privacy.configure("balanced")
    assert out.splitlines()[:3] == [
        "ref: task:7",
        "a task: a task from meeting notes",
        f"link: {GRANOLA_URL}",
    ]
    assert "vendor" not in out.lower() and "Vendor sync" not in out


def test_open_link_in_balanced_mode_names_the_source(store: FakeStore) -> None:
    out = asyncio.run(refs.open_link({"ref": "task:7"}))
    assert "a task: Send the revised vendor quote" in out
    assert "from: granola:n1 (Vendor sync)" in out and f"link: {GRANOLA_URL}" in out


def test_send_link_tool(store: FakeStore, added: list[dict[str, Any]]) -> None:
    out = asyncio.run(refs.send_link({"ref": "granola:n1"}))
    assert out == "ref: granola:n1\nI put the link in your Scout reminders."
    assert added[0]["url"] == GRANOLA_URL and added[0]["list"] == "Scout"
    with pytest.raises(Exception, match="no record to send"):
        refs.use(refs.Log())
        asyncio.run(refs.send_link({"about": "that"}))


def test_link_tools_are_shared_with_the_phone_and_sessions() -> None:
    from scout import assistant, mcp_server, shared_tools

    for name in ("open_link", "send_link"):
        assert not shared_tools.BY_NAME[name].asks  # Scout's own list: no spoken yes
        assert f"mcp__voice_app__{name}" in assistant.PHONE_TOOLS
    offered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"open_link", "send_link"} <= offered


def test_local_search_results_carry_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import shared_tools

    ix = index.default()
    assert isinstance(ix, index.Index)
    ix.put("note:5", "note", "Propane tank", "Refilled the propane tank at the Luther station.")
    out = asyncio.run(shared_tools.BY_NAME["local_search"].run({"query": "propane"}))
    assert out.endswith("(ref note:5)")


# --- the journal keeps them -----------------------------------------------------------------


def test_journal_refs_line_round_trips(tmp_path: Path) -> None:
    ts = dt.datetime(2026, 10, 15, 8, 1, 5).timestamp()
    e = journal.Entry(
        ts, "room", "give me a link to that", "It's from your meeting.", [], True, ["task:7", "granola:n1"]
    )
    p = journal.append(tmp_path, e, keep_private=False)
    text = p.read_text()
    assert "refs: task:7, granola:n1\n" in text  # ids only, even for a private entry
    (item,) = journal.parse(p)
    assert item["meta"]["refs"] == ["task:7", "granola:n1"] and item["meta"]["private"]
    assert "task:7" not in item["body"]


def test_the_turn_journals_its_refs_when_the_store_takes_them(
    store: FakeStore, added: list[dict[str, Any]]
) -> None:
    got: list[tuple[Any, ...]] = []

    class Journal:
        def journal_add(self, ts: float, who: str, q: str, a: str, tools: Any = (), private: bool = False,
                        refs: Any = ()) -> str:  # fmt: skip
            got.append((q, list(refs)))
            return "x"

    async def go() -> None:
        r: Any = make()
        r.a.index = Journal()
        await r.a.announce(refs.Spoken("Pat Doe owes you the vendor quote.", ["task:7"], True), "pulse")
        assert await r.a._answer_locally("where did that come from")

    asyncio.run(go())
    assert got == [("where did that come from", ["task:7"])]


def test_tier0_link_phrasings() -> None:
    for text in (
        "give me a link to that",
        "Send me that link.",
        "can I get a link to that",
        "what's the link",
        "where did that come from?",
        "what meeting was that from",
        "what email was that from",
        "where's that from",
    ):
        c = local_intents.Context(Timers(lambda label: None), said=lambda: refs.Log())
        assert asyncio.run(local_intents.answer(text, c)) is None, text  # matched, but nothing said: Claude
    names = {fn.__name__ for p, fn in local_intents._RULES if p.match("give me a link to that")}
    assert names == {"_link_that"}


def test_pulse_owed_sentence_is_one_ref_each() -> None:
    said = asyncio.run(_pulse().check(NOW))
    assert said == [
        "Due today: Book flights.",
        "Pat Doe owes you Send the revised vendor quote, overdue since October 9; maybe follow up.",
    ]
    assert refs.private_of(said[1]) and not refs.private_of(said[0])


def test_tier1_tasks_answer_sets_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    ts = [_task(7, "Send the revised vendor quote", owner="Pat Doe", due="2026-10-09")]
    monkeypatch.setattr(tasks, "all_tasks", lambda store=None: ts)
    monkeypatch.setattr(tasks, "_people", lambda: [])
    decision: dict[str, Any] = {"tool": "tasks", "args": {"which": "waiting"}}
    said = asyncio.run(tier1.run(decision, NOW))
    assert said and "vendor quote" in said and decision["refs"] == ["task:7"]


def test_phone_asks_for_the_link_to_what_the_room_heard(
    store: FakeStore, added: list[dict[str, Any]]
) -> None:
    async def go() -> tuple[dict[str, Any], refs.Said]:
        r: Any = make()
        await r.a.announce(refs.Spoken("Pat Doe owes you the vendor quote.", ["task:7"], True), "pulse")
        out = await r.a.ask("phone", "send me that link")
        return out, r.a.said.items[-1]

    out, last = asyncio.run(go())
    assert out["text"].endswith("I put the link in your Scout reminders.") and out["tier"] == "0"
    assert last.channel == "phone" and last.refs == ("task:7",)
    assert added and added[0]["url"] == GRANOLA_URL


def test_pulse_never_says_them_owes_you() -> None:
    async def none() -> dict[str, Any]:
        return {"events": [], "reminders": []}

    async def events(start: str, end: str) -> dict[str, Any]:
        return {"events": []}

    async def task_list() -> list[tasks.Task]:
        return [_task(4, "Signed contract", owner="them", due="2026-10-09", **{"from": "meeting"})]

    said = asyncio.run(pulse.Pulse(10, 15, events, none, task_list).check(NOW))
    assert said == ["Someone owes you Signed contract, overdue since October 9; maybe follow up."]
    assert refs.refs_of(said[0]) == ("task:4",)
