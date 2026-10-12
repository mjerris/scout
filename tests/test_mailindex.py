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


def test_a_pass_stores_new_mail_once_and_ages_out_old_mail() -> None:
    calls: list[str] = []
    ix = index.default()
    first = asyncio.run(mailindex.sync_pass(ix, fake_mail(calls), keep_days=90))
    assert (first["listed"], first["stored"], first["total"]) == (3, 2, 2)  # the 200-day-old one skipped
    again = asyncio.run(mailindex.sync_pass(ix, fake_mail(calls), keep_days=90))
    assert (again["stored"], again["total"]) == (0, 2)
    assert calls == ["list", "read 2", "list"]  # nothing read twice (2 fit in one chunk)
    ix.put("mail:7", "mail", title="Stale", ts=0)
    assert asyncio.run(mailindex.sync_pass(ix, fake_mail(calls), keep_days=90))["aged_out"] == 1
    rover = ix.get("mail:41")
    assert rover is not None and rover.title == "New message from Catherine" and rover.who.startswith("Rover")
    assert "unsubscribe" not in rover.body  # footers dropped, like spoken summaries


def test_mail_search_uses_the_index_and_matches_message_text() -> None:
    asyncio.run(mailindex.sync_pass(index.default(), fake_mail([]), keep_days=90))

    async def mail_must_not_be_asked(script: str, *args: str) -> str:
        raise AssertionError("searched Mail instead of the index")

    found = asyncio.run(mail_mac.listing(5, query="walk Biscuit", run=mail_must_not_be_asked))
    assert [m["id"] for m in found["messages"]] == [41] and found["source"] == "Scout's local index"


def test_mail_search_falls_back_to_mail_when_the_index_is_empty() -> None:
    assert mailindex.search("anything", 5, 180) is None
