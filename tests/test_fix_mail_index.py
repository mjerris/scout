"""Scout's mail index: search keeps working when the memory service is down, the
background pass never skips mail it hasn't stored, and a message found by subject and
date is stored under Mail's own id. Synthetic messages and a fake Mail only."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from typing import Any

import pytest

from scout import index, mac, mail_mac, mailindex, memory_client
from scout.mac import ToolError

NOW = dt.datetime.now().astimezone()


@pytest.fixture(autouse=True)
def _no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may reach osascript: any call is refused."""

    async def run(*argv: str, timeout: float = 10.0) -> str:
        raise AssertionError(f"osascript reached: {argv[:1]}")

    monkeypatch.setattr(mac, "_run", run)


def _msg(i: int, minutes_ago: float, subject: str = "") -> dict[str, Any]:
    return {
        "id": i,
        "date": (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(),
        "sender": f"Person {i} <p{i}@example.com>",
        "subject": subject or f"Message {i}",
        "read": False,
    }


def fake_mail(inbox: list[dict[str, Any]], reads: list[int] | None = None) -> Any:
    async def run(script: str, *args: str) -> str:
        if script == mail_mac._LIST:
            return json.dumps({"messages": inbox})
        if script == mail_mac._READ_MANY:
            ids = [int(i) for i in args[0].split(",")]
            if reads is not None:
                reads.extend(ids)
            return json.dumps(
                {"messages": [{**m, "body": f"text of {m['id']}"} for m in inbox if m["id"] in ids]}
            )
        raise AssertionError("unexpected Mail script")

    return run


class Down:
    """The memory service stopped: every call is the client's ToolError(DOWN)."""

    def __getattr__(self, name: str) -> Any:
        def fail(*a: Any, **k: Any) -> Any:
            raise ToolError(memory_client.DOWN)

        return fail


# --- #26: search with the memory service down -------------------------------------------


def test_mail_search_falls_back_to_mail_when_the_memory_service_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(index, "default", lambda: Down())
    inbox = [_msg(41, 30, "Lunch on Friday")]
    out = asyncio.run(
        mail_mac.listing(5, query="lunch", run=fake_mail(inbox), index=mail_mac.Index(ids_trusted=False))
    )
    assert [m["id"] for m in out["messages"]] == [41] and "source" not in out  # Mail answered


def test_mail_index_search_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[int] = []

    def search(q: str, n: int, days: int) -> dict[str, Any]:
        threads.append(threading.get_ident())
        return {"messages": [], "days": days, "source": "Scout's local index"}

    monkeypatch.setattr(mailindex, "search", search)

    async def go() -> int:
        await mail_mac.listing(5, query="lunch", run=fake_mail([]), index=mail_mac.Index(ids_trusted=False))
        return threading.get_ident()

    loop_thread = asyncio.run(go())
    assert threads and threads[0] != loop_thread


# --- #27: the sync watermark ------------------------------------------------------------


def test_a_pass_with_more_than_it_reads_gets_the_rest_next_time() -> None:
    """60 new messages, 50 read per pass: the 10 older ones come on the next pass."""
    inbox = [_msg(100 + i, i + 1) for i in range(60)]  # newest first
    ix = index.default()
    first = asyncio.run(mailindex.sync_pass(ix, fake_mail(inbox)))
    assert (first["new"], first["stored"]) == (50, 50)
    second = asyncio.run(mailindex.sync_pass(ix, fake_mail(inbox)))
    assert (second["new"], second["stored"]) == (10, 10)
    assert all(ix.get(f"mail:{m['id']}") is not None for m in inbox)
    third = asyncio.run(mailindex.sync_pass(ix, fake_mail(inbox)))
    assert third["new"] == 0


def test_remembered_search_results_do_not_hide_older_new_mail() -> None:
    """30 arrive; a listing remembers the 10 newest (sender and subject). The pass still
    reads the other 20, and fills in the 10."""
    inbox = [_msg(200 + i, i + 1) for i in range(30)]
    ix = index.default()
    assert mailindex.remember(inbox[:10], with_body=False) == 10
    out = asyncio.run(mailindex.sync_pass(ix, fake_mail(inbox)))
    assert (out["new"], out["filled"], out["stored"]) == (20, 10, 30)
    for m in inbox:
        hit = ix.get(f"mail:{m['id']}")
        assert hit is not None and hit.body == f"text of {m['id']}" and not hit.meta.get("partial")


# --- #30: a message found by subject and date --------------------------------------------


def test_a_message_found_by_subject_is_stored_under_mails_own_id() -> None:
    """Mail's message 41 isn't the index row 41; it's found by subject and date as Mail's
    77. Scout's index must say 77, or a later mail_read 41 reads some other email."""
    row = {**_msg(41, 30, "Biscuit on Saturday"), "mailbox": "imap://acct/INBOX"}
    other = {**_msg(41, 600, "Something else"), "body": "not this one"}
    real = {**row, "id": 77, "to": [], "cc": [], "body": "Can you walk Biscuit?", "truncated": False}
    idx = mail_mac.Index()
    idx.remember(row)

    async def run(script: str, *args: str) -> str:
        if script == mail_mac._READ_MANY:
            return json.dumps({"messages": [other], "missing": []})
        if script == mail_mac._FIND:
            return json.dumps(real)
        raise AssertionError("unexpected Mail script")

    async def go() -> dict[int, dict[str, Any]]:
        got = await mail_mac.bodies([41], run, index=idx)
        await asyncio.sleep(0.1)  # remembered off the request path
        return got

    got = asyncio.run(go())
    assert got[41]["mail_id"] == 77
    ix = index.default()
    assert ix.get("mail:41") is None
    hit = ix.get("mail:77")
    assert hit is not None and hit.meta["mail_id"] == 77 and "Biscuit" in hit.body
    found = mailindex.search("Biscuit", 5, 30)
    assert found is not None and [m["id"] for m in found["messages"]] == [77]
