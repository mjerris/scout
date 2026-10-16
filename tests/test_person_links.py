"""Every record linked to the people it's with (people.py's resolver, Index.link_participants,
put_people's merge, backfill_people; docs/contacts.md, People): a Slack DM to the other
party by user id, a channel message to its author and the people it @-mentions, mail to
its sender and recipients, a meeting to its attendees, a GitHub item to its author and
assignees. One person per real person across Slack, email, Contacts and GitHub; the user
("me") is never one of them. Synthetic data only."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest

from scout import contacts_mac, granola, index, links, people, recordpage, shared_tools, slack
from scout.mac import ToolError

NOW = time.time()

CTX = people.Ctx(
    me_slack="U0",
    me_emails=frozenset({"me@corp.example"}),
    me_github=frozenset({"melogin"}),
    me_names=frozenset({"mike", "michael jerris"}),
    dm_user={"D1": "U9"},
    slack_names={"U0": ["Michael Jerris"], "U9": ["Joao Santos", "joao"], "U1": ["Pat Doe", "patd"]},
)


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    s.people_ctx = lambda: CTX
    return s


def theirs(s: index.Index, pid: str) -> dict[str, str]:
    """The records linked to a person: {record id: rel}."""
    return {ln["id"]: ln["rel"] for ln in s.links(pid) if ln["dir"] == "in" and ln["rel"] in people.RELS}


def to_people(s: index.Index, rid: str) -> dict[str, str]:
    return {
        ln["id"]: ln["rel"] for ln in s.links(rid) if ln["dir"] == "out" and ln["id"].startswith("person:")
    }


def names_for(uid: str | None) -> str:
    return {"U0": "Michael Jerris", "U9": "Joao Santos", "U1": "patd", "U2": "sam", "U3": "lee"}.get(
        uid or "", uid or "someone"
    )


def dm(s: index.Index, ts: str, msgs: list[dict[str, Any]]) -> str:
    slack.store_docs(s, slack.documents("D1", "DM with Joao Santos", msgs, names_for, dm_user="U9"))
    return f"slack:D1:{ts}"


def test_a_slack_dm_links_the_other_party_by_user_id(store: index.Index) -> None:
    rid = dm(
        store, "1.0", [{"ts": "1.0", "user": "U9", "text": "hi"}, {"ts": "1.1", "user": "U0", "text": "yo"}]
    )
    assert to_people(store, rid) == {"person:slack:U9": "with"}
    # Only the user spoke: still the DM's other party.
    only_me = dm(store, "2.0", [{"ts": "2.0", "user": "U0", "text": "ping"}])
    assert to_people(store, only_me) == {"person:slack:U9": "with"}
    p = store.get("person:slack:U9")
    assert p is not None and p.kind == "person" and p.title == "Joao Santos"
    assert store.get("person:slack:U0") is None  # the user is never a person
    assert set(theirs(store, "person:slack:U9")) == {rid, only_me}


def test_a_dm_stored_before_dm_user_was_kept_uses_the_conversation_list(store: index.Index) -> None:
    store.put("slack:D1:3.0", "slack", "DM with Joao Santos", "Joao Santos: x", "Joao Santos", NOW,
              {"channel": "D1"})  # fmt: skip
    assert to_people(store, "slack:D1:3.0") == {"person:slack:U9": "with"}


def test_a_channel_message_links_its_authors_and_mentions(store: index.Index) -> None:
    msgs: list[dict[str, Any]] = [
        {"ts": "5.0", "user": "U1", "text": "<@U2> and <@U0> please look", "reply_count": 1},
        {"ts": "5.1", "user": "U3", "text": "on it"},
    ]
    slack.store_docs(store, slack.documents("C1", "#launch", msgs, names_for))
    assert to_people(store, "slack:C1:5.0") == {
        "person:slack:U1": "from",
        "person:slack:U3": "from",
        "person:slack:U2": "mentions",
    }


def test_a_renamed_slack_user_is_still_one_person(store: index.Index) -> None:
    slack.store_docs(
        store, slack.documents("C1", "#a", [{"ts": "6.0", "user": "U1", "text": "x"}], lambda u: "patd")
    )
    slack.store_docs(
        store, slack.documents("C1", "#a", [{"ts": "7.0", "user": "U1", "text": "y"}], lambda u: "Pat D.")
    )
    # Stored before Slack messages kept user ids: the name, through the users Scout knows.
    store.put("slack:C1:8.0", "slack", "#a", "patd: z", "patd", NOW, {"channel": "C1"})
    persons = store.recent(["person"], 50)
    assert [p.id for p in persons] == ["person:slack:U1"]
    assert set(theirs(store, "person:slack:U1")) == {"slack:C1:6.0", "slack:C1:7.0", "slack:C1:8.0"}
    assert {"patd", "Pat D."} <= set(persons[0].meta["names"])


def test_mail_links_its_sender_and_recipients_and_never_the_user(store: index.Index) -> None:
    store.put("mail:1", "mail", "Launch", "x", '"Pat Doe" <Pat@Acme.example>', NOW,
              {"to": ["me@corp.example", "sam@corp.example"], "cc": ["Lee@Corp.example"]})  # fmt: skip
    assert to_people(store, "mail:1") == {
        "person:email:pat@acme.example": "from",
        "person:email:sam@corp.example": "to",
        "person:email:lee@corp.example": "to",
    }
    pat = store.get("person:email:pat@acme.example")
    assert pat is not None and pat.title == "Pat Doe"
    assert store.get("person:email:me@corp.example") is None
    # A machine's address makes nobody.
    store.put("mail:2", "mail", "Receipt", "x", "Shop <no-reply@shop.example>", NOW)
    assert to_people(store, "mail:2") == {}


def meeting(s: index.Index, nid: str, attendees: list[dict[str, str]]) -> None:
    granola.store_note(s, {
        "id": nid, "title": f"Meeting {nid}", "web_url": f"https://notes.granola.ai/d/{nid}",
        "summary_text": "We talked.", "attendees": attendees,
        "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(NOW, dt.UTC).isoformat()},
        "transcript": [{"speaker": {"source": "speaker"}, "text": "Hello."}],
    })  # fmt: skip


def test_a_meeting_links_its_attendees(store: index.Index) -> None:
    meeting(
        store,
        "n1",
        [{"name": "Pat Doe", "email": "pat@acme.example"}, {"name": "Mike", "email": "me@corp.example"}],
    )
    assert to_people(store, "granola:n1") == {"person:email:pat@acme.example": "with"}
    assert to_people(store, "granola:n1#1") == {}  # its parts come with it, not linked one by one
    assert "granola:n1#1" in store.person_items("person:email:pat@acme.example")
    # No address: a full name only one person goes by.
    meeting(store, "n2", [{"name": "Pat Doe"}, {"name": "Nobody Known"}, {"name": "Michael Jerris"}])
    assert to_people(store, "granola:n2") == {"person:email:pat@acme.example": "with"}
    assert store.count("person") == 1


def test_a_github_item_links_its_author_and_assignees(store: index.Index) -> None:
    store.put("github:o/r#1", "github", "Fix", "x", "octo, dev", NOW,
              {"author": "octo", "assignees": ["dev", "melogin"], "type": "issue"})  # fmt: skip
    assert to_people(store, "github:o/r#1") == {"person:github:octo": "from", "person:github:dev": "to"}
    store.put(
        "github:o/r@abc", "github", "Commit", "x", "dependabot[bot]", NOW, {"author": "dependabot[bot]"}
    )
    assert to_people(store, "github:o/r@abc") == {}


FOUND: dict[str, dict[str, Any]] = {"pat@acme.example": {"id": "c1", "name": "Pat Doe"}}
SLACK: list[dict[str, Any]] = [
    {"id": "U1", "profile": {"display_name": "patd", "real_name": "Pat Doe", "email": "pat@acme.example"}},
    {"id": "U7", "profile": {"display_name": "never", "real_name": "Never Seen", "email": "n@corp.example"}},
]


def test_slack_email_and_contacts_merge_into_one_person(store: index.Index) -> None:
    slack.store_docs(
        store, slack.documents("C1", "#a", [{"ts": "9.0", "user": "U1", "text": "x"}], names_for)
    )
    store.put("mail:1", "mail", "Hi", "x", "Pat Doe <pat@acme.example>", NOW)
    assert store.count("person") == 2
    # Slack's profile email joins them, without Contacts: the Slack user's record holds both.
    store.put_people(people.build({}, SLACK))
    assert [h.id for h in store.recent(["person"], 10)] == ["person:slack:U1"]
    assert set(theirs(store, "person:slack:U1")) == {"slack:C1:9.0", "mail:1"}
    assert store.get("person:slack:U7") is None  # someone Scout has no record with makes nobody
    # Contacts knows the address: the contact's record, with every identity and link.
    got = store.put_people(people.build(FOUND, SLACK))
    assert got["people"] == 1
    assert [h.id for h in store.recent(["person"], 10)] == ["person:c1"]
    p = store.get("person:c1")
    assert p is not None and p.meta["contact"] == "c1" and p.meta["emails"] == ["pat@acme.example"]
    assert [s["id"] for s in p.meta["slack"]] == ["U1"]
    assert set(theirs(store, "person:c1")) == {"slack:C1:9.0", "mail:1"}
    # New records find the merged person by any identity.
    store.put("mail:2", "mail", "Again", "x", "pat@acme.example", NOW)
    slack.store_docs(
        store, slack.documents("C1", "#a", [{"ts": "9.5", "user": "U1", "text": "y"}], names_for)
    )
    assert set(theirs(store, "person:c1")) == {"slack:C1:9.0", "slack:C1:9.5", "mail:1", "mail:2"}
    assert store.put_people(people.build(FOUND, SLACK)) == {**got, "links": 4}  # idempotent
    assert [h.id for h in store.recent(["person"], 10)] == ["person:c1"]
    # Gone from Contacts: still the same person by Slack and email, without the contact.
    store.put_people(people.build({}, SLACK))
    q = store.get("person:c1")
    assert q is not None and not q.meta.get("contact")
    assert len(theirs(store, "person:c1")) == 4


def test_the_backfill_links_what_was_stored_before_and_is_idempotent(store: index.Index) -> None:
    store.people_ctx = lambda: people.Ctx()  # nothing linked as they're stored...
    store.put("mail:1", "mail", "Hi", "x", "Pat Doe <pat@acme.example>", NOW, {"to": ["sam@corp.example"]})
    store.put(
        "slack:D1:1.0",
        "slack",
        "DM with Joao Santos",
        "Joao Santos: x",
        "Joao Santos",
        NOW,
        {"channel": "D1"},
    )
    meeting(store, "n1", [{"name": "Pat Doe", "email": "pat@acme.example"}])
    store.put("github:o/r#1", "github", "Fix", "x", "octo", NOW, {"author": "octo", "assignees": []})
    store.put("note:1", "note", "Pat Doe", "a note", "", NOW)
    store.link("person:c9", "mail:1", "same_person")  # the old matcher's link
    with store._lock:
        store._conn.execute("DELETE FROM links WHERE dst LIKE 'person:%'")
        store._conn.execute("DELETE FROM person_keys")
        store._conn.execute("DELETE FROM docs WHERE kind = 'person'")
    store.people_ctx = lambda: CTX  # ...then the backfill, a few at a time
    steps = []
    while not (r := store.backfill_people(batch=2))["done"]:
        steps.append(r)
    assert len(steps) >= 2 and r["records"] == 4
    want = {
        "person:email:pat@acme.example": {"mail:1": "from", "granola:n1": "with"},
        "person:email:sam@corp.example": {"mail:1": "to"},
        "person:slack:U9": {"slack:D1:1.0": "with"},
        "person:github:octo": {"github:o/r#1": "from"},
    }
    assert {p.id: theirs(store, p.id) for p in store.recent(["person"], 50)} == want
    assert not [ln for ln in store.links("mail:1") if ln["rel"] == "same_person"]
    assert store.backfill_people()["done"]  # done stays done: nothing read again
    store.restart_people_backfill()
    while not store.backfill_people(batch=3)["done"]:
        pass
    assert {p.id: theirs(store, p.id) for p in store.recent(["person"], 50)} == want  # the same again


def test_local_search_follows_the_links_without_contacts(
    store: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = dm(store, "1.0", [{"ts": "1.0", "user": "U9", "text": "the budget is fine"}])
    store.put("mail:9", "mail", "Budget", "the budget", "Other <o@x.example>", NOW)

    async def no_contacts(*a: Any, **k: Any) -> Any:
        raise ToolError("Contacts isn't allowed.")

    monkeypatch.setattr(contacts_mac, "one_person", no_contacts)
    out = asyncio.run(shared_tools._local_search({"query": "", "person": "Joao"}))
    assert (
        out.startswith("(what Joao Santos said, sent or was in)")
        and f"ref {rid}" in out
        and "mail:9" not in out
    )
    assert store.people_named("joao santos")[0]["id"] == "person:slack:U9"


def test_record_pages_name_the_new_links(store: index.Index) -> None:
    rid = dm(store, "1.0", [{"ts": "1.0", "user": "U9", "text": "x"}])
    rec = store.resolve("person:slack:U9")
    assert rec is not None and rec["private"]
    assert "With them" in recordpage.render(rec, store.get("person:slack:U9"))
    r = store.resolve(rid)
    assert r is not None and "With" in recordpage.render(r, store.get(rid))
    assert {"with", "from", "to"} <= set(links.RELS)


def test_scout_memory_runs_the_backfill_at_startup_and_logs_counts(
    store: index.Index, caplog: pytest.LogCaptureFixture
) -> None:
    from scout import memory_service

    for n in range(5):
        store.put(f"mail:{n}", "mail", "Hi", "x", f"p{n}@acme.example", NOW)
    store.restart_people_backfill()
    with caplog.at_level("INFO", logger="scout.memory_service"):
        st = asyncio.run(memory_service.people_backfill(store, batch=2, gap_s=0))
        assert st["done"] and st["records"] == 5 and st["links"] == 5 and st["people"] == 5
        again = asyncio.run(memory_service.people_backfill(store, batch=2, gap_s=0))
    assert again["done"] and again["records"] == 5  # done: not read again
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("people: backfill done: 5 records, 5 links, 5 people") for m in said)
    assert not any("acme" in m for m in said)  # counts only
