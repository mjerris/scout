"""The whole chain against the real store (index.Index with its links and resolve()):
the pulse names a task found in a meeting, an email or a Slack thread; "give me a link
to that" puts that source's link in the Scout reminders list; "where did that come
from" names it; the journal keeps the refs and links the turn to the record. Synthetic
records only."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import urllib.parse
from typing import Any

import pytest
from test_links import granola_note
from test_routing import make

from scout import index, journal, memory, privacy, pulse, refs, reminders_mac, tasks

BASE = "https://scout.example.ts.net"
NOW = dt.datetime.now().astimezone().replace(hour=8, minute=0, second=0, microsecond=0)
YESTERDAY = (NOW - dt.timedelta(days=1)).date()
MET = (NOW - dt.timedelta(days=2)).replace(hour=14).timestamp()


def _day(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    return f"{d:%B} {d.day}"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index) and s.journal_root is not None
    s.web_base = lambda: BASE
    monkeypatch.setattr(tasks, "_people", lambda: [])
    refs._sent.clear()
    return s


@pytest.fixture
def added(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    async def add_data(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        out.append(a)
        return {"id": "R1", "title": a["title"], "list": a.get("list"), "url": a.get("url")}

    monkeypatch.setattr(reminders_mac, "add_data", add_data)
    return out


def _sources(s: index.Index) -> dict[str, tuple[str, str, str]]:
    """One task found in each kind of source: (source key, its link, how it's named)."""
    granola_note(s, "not_v", "Vendor sync", MET)
    s.put("mail:7", "mail", "Lease renewal", "please renew", "Sam Lee <sam@example.com>", MET,
          {"mail_id": 7, "message_id": "lease7@example.com"})  # fmt: skip
    s.put("slack:C1:1712.000100", "slack", "#launch", "Sam: can you review?", "Sam Lee", MET,
          {"channel": "C1", "ts": "1712.000100", "last_ts": "1712.000300",
           "permalink": "https://team.slack.com/archives/C1/p1712000100"})  # fmt: skip
    return {
        "meeting": ("meeting:not_v", "https://notes.granola.ai/d/not_v", f"your meeting Vendor sync, on {_day(MET)}"),
        "mail": ("mail:7", "message://%3Clease7@example.com%3E",
                 f"an email from Sam Lee about Lease renewal, on {_day(MET)}"),
        "slack": ("slack:C1@1712.000300", "https://team.slack.com/archives/C1/p1712000100",
                  f"a Slack message from Sam Lee, on {_day(MET)}"),
    }  # fmt: skip


@pytest.mark.parametrize("kind", ["meeting", "mail", "slack"])
def test_pulse_then_link_to_that_per_source(
    store: index.Index, added: list[dict[str, Any]], kind: str
) -> None:
    key, url, named = _sources(store)[kind]
    n = store.task_add(
        "Send the revised quote",
        {"source": key, "from": kind, "owner": "Pat Doe", "due": YESTERDAY.isoformat()},
        "",
    )

    async def go() -> list[str]:
        r: Any = make()
        r.a.index = store
        p = pulse.Pulse(10, 15, _no_events, _no_reminders, lambda: asyncio.to_thread(tasks.all_tasks))
        said = await p.check(NOW)
        assert [refs.refs_of(x) for x in said] == [(f"task:{n}",)]
        await r.a.announce(said[0], "pulse")
        assert await r.a._answer_locally("give me a link to that")
        assert await r.a._answer_locally("where did that come from")
        return list(r.spk.said)

    spoken = asyncio.run(go())
    assert spoken[0].startswith("Pat Doe owes you Send the revised quote, overdue since")
    assert spoken[1] == f"It's from {named}. I put the link in your Scout reminders."
    assert spoken[2] == f"It's from {named}."
    assert [(a["list"], a["url"]) for a in added] == [("Scout", url)]


def test_the_journal_keeps_refs_and_links_the_turn(store: index.Index, added: list[dict[str, Any]]) -> None:
    key, _, _ = _sources(store)["meeting"]
    n = store.task_add("Send the revised quote", {"source": key, "from": "meeting", "owner": "Pat Doe"}, "")

    async def go() -> None:
        r: Any = make()
        r.a.index = store
        await r.a.announce(
            refs.Spoken("Pat Doe owes you Send the revised quote.", [f"task:{n}"], True), "pulse"
        )
        assert await r.a._answer_locally("what meeting was that from")

    asyncio.run(go())
    (day,) = journal.day_files(store.journal_root)  # type: ignore[arg-type]
    assert f"refs: task:{n}\n" in day.read_text()
    (turn,) = store.recent(["turn"], 5)
    assert turn.meta.get("refs") == [f"task:{n}"]
    assert {"id": f"task:{n}", "rel": "about", "dir": "out"} in store.links(turn.id)


def test_claude_context_names_the_refs_from_the_real_store(store: index.Index) -> None:
    key, _, _ = _sources(store)["meeting"]
    n = store.task_add("Send the revised quote", {"source": key, "from": "meeting", "owner": "Pat Doe"}, "")

    async def go(mode: str) -> str:
        r: Any = make()
        r.a.privacy = privacy.configure(mode)
        try:
            await r.a.announce(
                refs.Spoken("Pat Doe owes you Send the revised quote.", [f"task:{n}"], True), "pulse"
            )
            return str(await r.a._with_context("send that to my phone"))
        finally:
            privacy.configure("balanced")

    assert f"task:{n} (a task: Send the revised quote)" in asyncio.run(go("balanced"))
    strict = asyncio.run(go("strict"))
    assert f"task:{n} (a task from a meeting)" in strict and "revised quote" not in strict


def test_open_link_over_the_real_store(store: index.Index) -> None:
    key, url, _ = _sources(store)["mail"]
    n = store.task_add("Renew the lease", {"source": key, "from": "mail", "owner": "me"}, "")
    refs.log_of().add(refs.Said(time.time(), "Scout", "Renew the lease.", (f"task:{n}",), True))
    out = asyncio.run(refs.open_link({"about": "that"}))
    assert f"ref: task:{n}" in out and f"link: {url}" in out
    assert f"Scout's page for it: {BASE}/r/task%3A{n}" in out and "from: mail:7 (Lease renewal)" in out


async def _no_events(start: str, end: str) -> dict[str, Any]:
    return {"events": []}


async def _no_reminders() -> dict[str, Any]:
    return {"reminders": []}


def test_remember_then_where_did_that_come_from(store: index.Index, added: list[dict[str, Any]]) -> None:
    """A fact told in the room is linked to its turn; asked later, "where did that come
    from" says when and where, and its link is that turn's page."""
    from scout import memory_client

    mem = memory_client.memory()
    assert isinstance(mem, memory.Memory)

    def index_facts() -> None:
        store.sync("memory", mem.as_documents())

    mem.on_change = index_facts

    async def go() -> list[str]:
        r: Any = make()
        r.a.index = store
        assert await r.a._answer_locally("remember that my dentist is Dr. Lee")
        assert await r.a._answer_locally("who is my dentist")
        assert await r.a._answer_locally("where did that come from")
        assert await r.a._answer_locally("give me a link to that")
        return list(r.spk.said)

    spoken = asyncio.run(go())
    fid = memory.fact_id("my dentist is Dr. Lee")
    (told,) = [ln["id"] for ln in store.links(fid) if ln["rel"] == "told_in"]
    turn = store.get(told)
    assert turn is not None and turn.title == "remember that my dentist is Dr. Lee"
    assert spoken[:3] == [
        "Okay, I'll remember that your dentist is Dr. Lee.",
        "You told me your dentist is Dr. Lee.",
        f"You told me on {_day(turn.ts)}, in the room.",
    ]
    assert (
        spoken[3] == f"You told me on {_day(turn.ts)}, in the room. I put the link in your Scout reminders."
    )
    assert [(a["list"], a["url"]) for a in added] == [
        ("Scout", f"{BASE}/r/{urllib.parse.quote(told, safe='')}")
    ]
    turns = store.recent(["turn"], 10)
    assert len(turns) == 4 and all(t.meta.get("refs") == [fid] for t in turns)
    assert [ln["id"] for ln in store.links(fid) if ln["rel"] == "told_in"] == [told]  # asking didn't move it
