"""The inbox in the local store: a background pass stores new messages (once), ages
old ones out, and mail searches use the index (falling back to Mail when it's empty).
Synthetic messages only."""

import asyncio
import datetime as dt
import json
from typing import Any

from scout import index, mail_mac, mailindex

NOW = dt.datetime.now().astimezone()
INBOX = [
    {"id": 41, "date": (NOW - dt.timedelta(hours=1)).isoformat(), "sender": "Rover <rover@e.rover.com>", "subject": "New message from Catherine", "read": False},
    {"id": 40, "date": (NOW - dt.timedelta(days=2)).isoformat(), "sender": "Sam <sam@example.com>", "subject": "Lunch?", "read": True},
    {"id": 39, "date": (NOW - dt.timedelta(days=200)).isoformat(), "sender": "Old <old@example.com>", "subject": "Ancient", "read": True},
]  # fmt: skip
BODIES = {
    41: "Catherine wrote: can you walk Biscuit on Saturday morning?\n\nTo unsubscribe from these emails, click here.",
    40: "Are you free for lunch Thursday at the taco place?",
    39: "Very old news.",
}


def fake_mail(calls: list[str]) -> Any:
    async def run(script: str, *args: str) -> str:
        if script == mail_mac._LIST:
            calls.append("list")
            return json.dumps({"messages": INBOX})
        if script == mail_mac._READ_MANY:
            ids = [int(i) for i in args[0].split(",")]
            calls.append(f"read {len(ids)}")
            return json.dumps({"messages": [{**m, "body": BODIES[m["id"]]} for m in INBOX if m["id"] in ids]})
        raise AssertionError("unexpected Mail script")

    return run


def test_passes_index_only_new_mail() -> None:
    """The first pass takes the last two days; later ones only what's newer than the
    newest indexed message. Older mail isn't backfilled."""
    calls: list[str] = []
    ix = index.default()
    first = asyncio.run(mailindex.sync_pass(ix, fake_mail(calls)))
    assert (first["listed"], first["new"], first["stored"], first["total"]) == (
        3,
        1,
        1,
        1,
    )  # only the 1-hour-old one
    again = asyncio.run(mailindex.sync_pass(ix, fake_mail(calls)))
    assert (again["new"], again["stored"]) == (0, 0)
    assert calls == ["list", "read 1", "list"]  # nothing read twice
    rover = ix.get("mail:41")
    assert rover is not None and rover.title == "New message from Catherine" and rover.who.startswith("Rover")
    assert "unsubscribe" not in rover.body  # footers dropped, like spoken summaries


def test_older_mail_is_remembered_when_touched_and_filled_in_later() -> None:
    """A Mail search result is stored as sender and subject; the next pass reads its text."""
    ix = index.default()
    assert mailindex.remember([INBOX[1]], with_body=False) == 1  # found by a search
    assert mailindex.remember([INBOX[1]], with_body=False) == 0  # already known
    sam = ix.get("mail:40")
    assert sam is not None and sam.body == "" and sam.meta["partial"] is True
    calls: list[str] = []
    out = asyncio.run(mailindex.sync_pass(ix, fake_mail(calls)))
    assert out["filled"] == 1
    sam = ix.get("mail:40")
    assert sam is not None and "taco place" in sam.body and not sam.meta.get("partial")
    mailindex.remember([{**INBOX[2], "body": BODIES[39]}], with_body=True)  # read in full for a task
    assert ix.get("mail:39") is not None  # old mail stays: it mattered


def test_mail_read_for_a_task_lands_in_the_index() -> None:
    async def run(script: str, *args: str) -> str:
        assert script == mail_mac._READ
        return json.dumps({**INBOX[1], "body": BODIES[40]})

    async def go() -> None:
        await mail_mac.message(40, run=run)
        await asyncio.sleep(0.05)  # remembered off the request path

    asyncio.run(go())
    hit = index.default().get("mail:40")
    assert hit is not None and "taco place" in hit.body


def test_mail_search_uses_the_index_and_matches_message_text() -> None:
    asyncio.run(mailindex.sync_pass(index.default(), fake_mail([])))

    async def mail_must_not_be_asked(script: str, *args: str) -> str:
        raise AssertionError("searched Mail instead of the index")

    found = asyncio.run(mail_mac.listing(5, query="walk Biscuit", run=mail_must_not_be_asked))
    assert [m["id"] for m in found["messages"]] == [41] and found["source"] == "Scout's local index"


def test_mail_search_falls_back_to_mail_when_the_index_is_empty() -> None:
    assert mailindex.search("anything", 5, 180) is None


GEMINI: dict[str, Any] = {"id": 77, "date": (NOW - dt.timedelta(days=3)).isoformat(),
          "sender": "Gemini <gemini-notes@google.com>", "subject": "Notes: “Budget review” Oct 10, 2026", "read": True}  # fmt: skip
GEMINI_BODY = (
    "Summary\nThe team agreed to cut travel by 10%.\n\nNext steps\n[Mike] Send the revised budget to Pat by Friday.\n"
    + "Details. " * 800
)


def test_meeting_notes_by_email_are_stored_as_meetings_in_full() -> None:
    assert mailindex.is_meeting_summary(GEMINI["sender"], GEMINI["subject"])
    assert mailindex.is_meeting_summary("Zoom <no-reply@zoom.us>", "Meeting summary for Weekly sync")
    assert not mailindex.is_meeting_summary("Zoom <no-reply@zoom.us>", "Your Zoom bill")  # not a summary
    assert not mailindex.is_meeting_summary(
        "Pat <pat@example.com>", "Notes from lunch"
    )  # not a notes service
    ix = index.default()
    mailindex._put(ix, GEMINI, GEMINI_BODY)
    m = ix.get("mailmeeting:77")
    assert m is not None and m.kind == "meeting" and m.title == "Budget review"
    assert len(m.body) > 4000 and "[Mike] Send the revised budget" in m.body
    assert m.meta == {"source": "email", "note": "mail:77", "mail_id": 77}
    plain = ix.get("mail:77")
    assert plain is not None and plain.kind == "mail" and len(plain.body) <= 4000


def test_meeting_notes_from_the_last_two_weeks_are_brought_in_once() -> None:
    queries: list[str] = []

    async def run(script: str, *args: str) -> str:
        if script == mail_mac._LIST:
            queries.append(args[2])
            hits = [GEMINI] if args[2] == "gemini-notes@google.com" else []
            return json.dumps({"messages": [*hits, INBOX[1]]})  # Sam's lunch mail matches nothing
        if script == mail_mac._READ_MANY:
            return json.dumps({"messages": [{**GEMINI, "body": GEMINI_BODY}]})
        raise AssertionError("unexpected Mail script")

    ix = index.default()
    assert asyncio.run(mailindex.backfill_meetings(ix, run, days=14)) == 1
    assert queries == list(mailindex.MEETING_QUERIES) and all(q for q in queries)
    assert ix.get("mailmeeting:77") is not None and ix.get("mailmeeting:40") is None
    queries.clear()
    assert asyncio.run(mailindex.backfill_meetings(ix, run, days=14)) == 0 and queries == []  # once


def test_recipients_are_kept_for_the_task_farm() -> None:
    """taskfarm.mail_to_me reads To: mail the user is only Cc'd on isn't theirs to act on."""
    ix = index.default()
    m = {"id": 81, "date": NOW.isoformat(), "sender": "Pat <pat@example.com>", "subject": "Plan", "read": True,
         "to": ["Sam@Example.com"], "cc": ["mike@example.com"]}  # fmt: skip
    mailindex._put(ix, m, "Sam, can you review the plan?")
    h = ix.get("mail:81")
    assert h is not None and h.meta["to"] == ["sam@example.com"] and h.meta["cc"] == ["mike@example.com"]
    mailindex._put(
        ix, {k: v for k, v in m.items() if k not in ("to", "cc")}, None
    )  # listed again, no recipients
    h = ix.get("mail:81")
    assert h is not None and h.meta["to"] == ["sam@example.com"]
