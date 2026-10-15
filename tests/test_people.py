"""One person across sources (people.py; Index.put_people, link_person, person_search):
contacts found by the email addresses the store has seen, joined with their Slack user,
linked same_person to their mail, meetings and Slack messages; local_search's person,
tasks_list's owner, and the record pages. Synthetic data; Contacts and Slack are fakes."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest

from scout import contacts_mac, granola, index, links, people, recordpage, shared_tools, tasks

NOW = time.time()

PAT: dict[str, Any] = {"id": "c1", "name": "Pat Doe", "nickname": "", "score": 0}
PATRICIA: dict[str, Any] = {"id": "c2", "name": "Pat Lee", "score": 0}
SAM: dict[str, Any] = {"id": "c3", "name": "Sam Roe", "score": 0}
FOUND: dict[str, dict[str, Any]] = {"pat@acme.example": {"id": "c1", "name": "Pat Doe"}, "pat@home.example": {"id": "c1", "name": "Pat Doe"},
         "sam@corp.example": {"id": "c3", "name": "Sam Roe"}}  # fmt: skip
SLACK: list[dict[str, Any]] = [
    {"id": "U1", "profile": {"display_name": "patd", "real_name": "Pat Doe", "email": "pat@acme.example"}},
    {"id": "U2", "profile": {"display_name": "", "real_name": "Sam Roe", "email": "sam@corp.example"}},
    {"id": "U3", "profile": {"display_name": "sam", "real_name": "Sam Other", "email": "other@corp.example"}},
    {"id": "U4", "is_bot": True, "profile": {"real_name": "Deploy Bot", "email": "pat@home.example"}},
]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    monkeypatch.setattr(tasks, "_people", lambda: [])
    return s


def meeting(s: index.Index, nid: str, start: float, attendees: list[dict[str, str]]) -> None:
    granola.store_note(s, {
        "id": nid, "title": f"Meeting {nid}", "web_url": f"https://notes.granola.ai/d/{nid}",
        "summary_text": "We talked about the launch plan.", "attendees": attendees,
        "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(start, dt.UTC).isoformat()},
        "transcript": [{"speaker": {"source": "speaker"}, "text": "The launch slips a week."}],
    })  # fmt: skip


def fill(s: index.Index) -> None:
    s.put(
        "mail:1",
        "mail",
        "Launch date",
        "The launch moves to Friday.",
        '"Pat Doe" <Pat@Acme.example>',
        NOW - 100,
    )
    s.put("mail:2", "mail", "Lunch?", "Lunch on Tuesday?", "pat@home.example", NOW - 200)
    s.put("mail:3", "mail", "Invoice", "Your invoice.", "Billing <billing@vendor.example>", NOW - 300)
    s.put(
        "mail:4", "mail", "Hi", "hello", '"Pat Doe" <pat@elsewhere.example>', NOW - 400
    )  # a name alone proves nothing
    meeting(
        s,
        "n1",
        NOW - 3600,
        [{"name": "Pat Doe", "email": "pat@acme.example"}, {"name": "Mike", "email": "me@x.example"}],
    )
    meeting(s, "n2", NOW - 7200, [{"name": "Pat Doe"}, {"name": "Sam Roe"}])  # no addresses: the full name
    meeting(s, "n3", NOW - 9000, [{"name": "Pat Doe", "email": "pat.doe@other.example"}])  # another Pat Doe
    s.put("slack:C1:1.0", "slack", "#launch", "patd: the launch is Friday\nsam: ok", "patd, sam", NOW - 50)
    s.put(
        "slack:C1:1.0#2",
        "slack",
        "#launch",
        "patd: and the deck is ready",
        "patd, sam",
        NOW - 50,
        {"part": 2},
    )
    s.put("slack:C1:2.0", "slack", "#launch", "Sam Roe: lunch", "Sam Roe", NOW - 60)


def test_people_are_contacts_joined_with_their_slack_user_by_email() -> None:
    got = people.build(FOUND, SLACK)
    assert [p["contact"] for p in got] == ["c1", "c3"]
    pat, sam = got
    assert pat["emails"] == ["pat@acme.example", "pat@home.example"]
    assert pat["slack"] == [
        {"id": "U1", "shown": "patd", "real": "Pat Doe"}
    ]  # the bot with Pat's address isn't Pat
    assert sam["slack"] == [{"id": "U2", "shown": "Sam Roe", "real": "Sam Roe"}]
    assert people.names(pat) == ["Pat Doe", "patd"]
    two = people.build(
        FOUND, [*SLACK, {"id": "U5", "profile": {"display_name": "patd", "email": "x@y.example"}}]
    )
    assert two[0]["slack"][0]["shown"] == ""  # a name two Slack users go by matches nobody
    r = people.record(pat)
    assert r["id"] == "person:c1" and r["kind"] == "person" and r["title"] == "Pat Doe"
    assert "pat@" not in r["body"] and r["who"] == "Pat Doe, patd"  # addresses only in meta
    assert "person" in links.PRIVATE_KINDS and "same_person" in links.RELS


def test_the_store_links_a_person_to_what_is_theirs_and_keeps_it_current(store: index.Index) -> None:
    fill(store)
    assert store.people_candidates() == ["pat@acme.example", "pat@home.example", "billing@vendor.example",
                                         "pat@elsewhere.example", "me@x.example", "pat.doe@other.example"]  # fmt: skip
    got = store.put_people(people.build(FOUND, SLACK))
    assert got == {"people": 2, "removed": 0, "links": 7}
    theirs = {x["id"] for x in store.links("person:c1") if x["rel"] == "same_person"}
    assert theirs == {"mail:1", "mail:2", "granola:n1", "granola:n2", "slack:C1:1.0"}
    assert {x["id"] for x in store.links("person:c3")} == {"granola:n2", "slack:C1:2.0"}
    again = store.put_people(people.build(FOUND, SLACK))  # idempotent
    assert again == got and len(store.links("person:c1")) == 5
    assert store.person_items("person:c1").count("slack:C1:1.0#2") == 1  # its parts come along
    # New mail from Pat joins at the next upkeep; nothing else moves.
    store.put("mail:5", "mail", "Deck", "deck attached", "Pat Doe <pat@acme.example>", NOW)
    store.link_people()
    assert "mail:5" in {x["id"] for x in store.links("person:c1")}
    # Pat's gone from Contacts: the next full list drops them, with their links.
    store.put_people(people.build({"sam@corp.example": FOUND["sam@corp.example"]}, SLACK))
    assert store.get("person:c1") is None and store.links("mail:1") == []


def test_each_source_is_bounded(store: index.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(people, "LINKS_PER_SOURCE", 3)
    for n in range(10):
        store.put(f"mail:{n}", "mail", f"m{n}", "x", "pat@acme.example", NOW - n)
    store.put_people(people.build({"pat@acme.example": FOUND["pat@acme.example"]}))
    assert {x["id"] for x in store.links("person:c1")} == {"mail:0", "mail:1", "mail:2"}  # the newest


def test_a_persons_search_is_only_theirs(store: index.Index) -> None:
    fill(store)
    store.put("mail:9", "mail", "Launch", "The launch is cancelled.", "billing@vendor.example", NOW)
    store.put_people(people.build(FOUND, SLACK))
    ids = [h.id for h in store.person_search("person:c1", "launch", limit=10)]
    assert "mail:9" not in ids and {"mail:1", "slack:C1:1.0"} <= set(ids)
    assert [h.id for h in store.person_search("person:c1", "", kinds=["mail"])] == ["mail:1", "mail:2"]
    assert store.person_search("person:nobody", "launch") == []


def test_local_search_by_person(store: index.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    fill(store)
    store.put("mail:9", "mail", "Launch", "The launch is cancelled.", "billing@vendor.example", NOW)
    store.put_people(people.build(FOUND, SLACK))
    roster: list[dict[str, Any]] = [
        dict(PAT, emails=[], phones=[]),
        dict(PATRICIA, emails=[], phones=[]),
        dict(SAM, emails=[], phones=[]),
    ]

    async def search(query: Any, limit: Any = None, group: Any = None, helper: Any = None) -> dict[str, Any]:
        q = str(query).lower()
        hit = [p for p in roster if p["name"].lower() == q] or [
            p for p in roster if p["name"].lower().startswith(q)
        ]
        return {"contacts": hit, "total": len(hit)}

    monkeypatch.setattr(contacts_mac, "search_data", search)
    out: str = asyncio.run(
        shared_tools._local_search({"query": "launch", "person": "Pat Doe", "kinds": ["mail"]})
    )
    assert (
        out.startswith("(what Pat Doe said, sent or was in)") and "ref mail:1" in out and "mail:9" not in out
    )
    assert "Ask the user which one" in asyncio.run(
        shared_tools._local_search({"query": "launch", "person": "Pat"})
    )
    nobody = asyncio.run(shared_tools._local_search({"query": "launch", "person": "Zed"}))
    assert "Searching for the words and the name instead" in nobody


def test_tasks_owed_by_a_person_under_any_name(store: index.Index, monkeypatch: pytest.MonkeyPatch) -> None:
    fill(store)
    store.put_people(people.build(FOUND, SLACK))
    store.task_add("Send the deck", {"owner": "Pat Doe"}, "")
    store.task_add("Review the plan", {"owner": "patd"}, "")
    store.task_add("Book the room", {"owner": "Pat", "source": "mail:1", "from": "mail"}, "")
    store.task_add(
        "Pay the invoice", {"owner": "Pat", "source": "mail:3", "from": "mail"}, ""
    )  # another Pat's
    store.task_add("Lunch", {"owner": "Sam Roe"}, "")
    roster: list[dict[str, Any]] = [dict(PAT, emails=[], phones=[]), dict(PATRICIA, emails=[], phones=[])]

    async def search(query: Any, limit: Any = None, group: Any = None, helper: Any = None) -> dict[str, Any]:
        q = str(query).lower()
        hit = [p for p in roster if p["name"].lower() == q] or [
            p for p in roster if p["name"].lower().startswith(q)
        ]
        return {"contacts": hit, "total": len(hit)}

    monkeypatch.setattr(contacts_mac, "search_data", search)
    out = asyncio.run(tasks.list_text({"owner": "Pat Doe", "who": "all"}, store))
    assert "Send the deck" in out and "Review the plan" in out and "Book the room" in out
    assert "Pay the invoice" not in out and "Lunch" not in out
    assert "Ask the user which one" in asyncio.run(tasks.list_text({"owner": "Pat"}, store))


def test_record_pages_show_who_else_a_person_is(store: index.Index) -> None:
    fill(store)
    store.put_people(
        people.build(
            FOUND,
            [*SLACK[:1], {"id": "U9", "profile": {"display_name": "<b>x</b>", "email": "pat@home.example"}}],
        )
    )
    rec = store.resolve("person:c1")
    assert rec is not None and rec["private"]
    page = recordpage.render(rec, store.get("person:c1"))
    assert "Also known as" in page and "pat@acme.example" in page and "patd" in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert "Theirs" in page
    mail = store.resolve("mail:1")
    assert mail is not None
    assert "Same person" in recordpage.render(mail, store.get("mail:1"))


def test_the_apps_pass_never_asks_and_counts_only(
    store: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill(store)
    asked: list[list[str]] = []

    async def resolve(emails: list[str]) -> dict[str, dict[str, Any]]:
        asked.append(list(emails))
        return {e: FOUND[e] for e in emails if e in FOUND}

    async def slack_users() -> list[dict[str, Any]]:
        return SLACK

    monkeypatch.setattr(people, "_resolve", resolve)
    monkeypatch.setattr(people, "_slack_users", slack_users)
    got = asyncio.run(people.refresh(store))
    assert got == {"emails": 8, "people": 2, "removed": 0, "links": 7}
    assert "other@corp.example" in asked[0]  # Slack's addresses are asked about too

    async def none() -> None:
        return None

    monkeypatch.setattr(people, "_slack_users", none)  # Slack down: nobody is dropped
    asyncio.run(people.refresh(store))
    assert store.get("person:c3") is not None


def test_without_the_contacts_permission_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    exe = tmp_path / "scout-contacts"
    exe.write_text('#!/bin/sh\necho \'{"status": "not_determined"}\'\n')
    exe.chmod(0o755)
    monkeypatch.setattr(contacts_mac, "HELPER", contacts_mac.Helper(binary=exe))
    assert asyncio.run(people._resolve(["pat@acme.example"])) == {}


def test_people_over_the_memory_service(tmp_path: Any) -> None:
    from aiohttp import web

    from scout import memory_client, memory_service
    from scout.memory import Memory

    ix = index.Index(tmp_path / "m.db", None)
    ix.journal_root = tmp_path / "memory"
    tok = tmp_path / "tok"
    svc = memory_service.Service(ix, Memory(tmp_path / "facts.md"), memory_service.token(tok))
    fill(ix)

    async def main() -> None:
        runner = web.AppRunner(svc.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        store = memory_client.Store(memory_client.Client(f"http://127.0.0.1:{port}", tok))
        try:
            assert "pat@acme.example" in await asyncio.to_thread(store.people_candidates, 10)
            got = await asyncio.to_thread(store.put_people, people.build(FOUND, SLACK), True)
            assert got == {"people": 2, "removed": 0, "links": 7}
            hits = await asyncio.to_thread(
                store.person_search, "person:c1", "launch", ["mail"], None, 5, False
            )
            assert [h.id for h in hits] == ["mail:1"]
        finally:
            await runner.cleanup()

    try:
        asyncio.run(main())
    finally:
        ix.close()
