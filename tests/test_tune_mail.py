"""Mail path speed: the index pass lists the newest messages by range (not every inbox
id and date), message text comes from Scout's index when it has it, and an index search
is one store call. Synthetic messages only; Mail is always faked."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import pytest

from scout import index, mail_mac, mailindex
from scout.mac import ToolError

NOW = dt.datetime.now().astimezone()
US, RS = "\x1f", "\x1e"


@pytest.fixture(autouse=True)
def _fast_path_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mailindex, "_fast_off_until", 0.0)


def _secs(t: dt.datetime) -> str:
    """AppleScript's encoding: wall-clock seconds since 2020-01-01 local."""
    return str(int((t.astimezone().replace(tzinfo=None) - dt.datetime(2020, 1, 1)).total_seconds()))


def _msg(i: int, minutes_ago: float, read: bool = False) -> dict[str, Any]:
    return {
        "id": i,
        "date": (NOW - dt.timedelta(minutes=minutes_ago)).isoformat(),
        "sender": f"Person {i} <p{i}@example.com>",
        "subject": f"Message {i}",
        "read": read,
    }


def _ranges(inbox: list[dict[str, Any]]) -> str:
    return RS.join(
        US.join([str(m["id"]), _secs(dt.datetime.fromisoformat(m["date"])), "true" if m["read"] else "false"])
        for m in inbox
    )


class FakeMail:
    """Mail's JXA scripts and the AppleScript range listing, counted."""

    def __init__(self, inbox: list[dict[str, Any]], ranges_fail: bool = False) -> None:
        self.inbox, self.ranges_fail = inbox, ranges_fail
        self.ran: list[str] = []

    async def jxa(self, script: str, *args: str) -> str:
        if script == mail_mac._LIST:
            self.ran.append("list")
            return json.dumps({"messages": self.inbox})
        if script == mail_mac._READ_MANY:
            ids = [int(i) for i in args[0].split(",")]
            self.ran.append(f"read {sorted(ids)}")
            return json.dumps(
                {
                    "messages": [
                        {k: v for k, v in m.items() if k != "read"} | {"body": f"text of {m['id']}"}
                        for m in self.inbox
                        if m["id"] in ids
                    ]
                }
            )
        raise AssertionError("unexpected Mail script")

    async def newest(self, script: str, *args: str) -> str:
        assert script == mail_mac._NEWEST_AS and args == (str(mailindex.NEWEST),)
        self.ran.append("ranges")
        if self.ranges_fail:
            raise ToolError("osascript timed out after 30 s")
        return _ranges(self.inbox)


def test_the_index_pass_lists_newest_by_range_not_the_whole_inbox() -> None:
    inbox = [_msg(3, 5), _msg(2, 30, read=True), _msg(1, 60 * 24 * 5)]  # the last is before the first window
    mail = FakeMail(inbox)
    ix = index.default()
    first = asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))
    assert first["how"] == "ranges" and (first["listed"], first["new"], first["stored"]) == (3, 2, 2)
    assert mail.ran == ["ranges", "read [2, 3]"]  # never the full _LIST scan
    two = ix.get("mail:2")
    assert two is not None and two.meta["read"] is True and two.body == "text of 2"
    three = ix.get("mail:3")
    assert three is not None and three.meta["read"] is False  # read status from the range listing
    again = asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))
    assert (again["new"], again["stored"]) == (0, 0) and mail.ran[-1] == "ranges"
    mail.inbox = [_msg(4, 1), *inbox]
    third = asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))
    assert third["stored"] == 1 and mail.ran[-1] == "read [4]"


def test_the_watermark_only_moves_past_what_was_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mailindex, "BODIES", 2)
    inbox = [_msg(i, 10 * (6 - i)) for i in range(1, 6)]  # 5 new, 2 read per pass
    mail = FakeMail(inbox)
    ix = index.default()
    stored = [asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))["stored"] for _ in range(4)]
    assert stored == [2, 2, 1, 0] and ix.count("mail") == 5


def test_a_failed_range_listing_falls_back_to_the_full_scan_and_backs_off() -> None:
    mail = FakeMail([_msg(7, 5)], ranges_fail=True)
    ix = index.default()
    out = asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))
    assert out["how"] == "scan" and out["stored"] == 1 and mail.ran == ["ranges", "list", "read [7]"]
    mail.ran.clear()
    asyncio.run(mailindex.sync_pass(ix, mail.jxa, newest=mail.newest))
    assert mail.ran == ["list"]  # not retried (and timed out again) every pass


def test_a_faked_jxa_runner_alone_keeps_the_scan() -> None:
    mail = FakeMail([_msg(8, 5)])
    out = asyncio.run(mailindex.sync_pass(index.default(), mail.jxa))
    assert out["how"] == "scan" and mail.ran == ["list", "read [8]"]


def test_newest_parses_sorts_and_keeps_its_own_offset() -> None:
    winter = dt.datetime(2026, 1, 15, 9, 30).astimezone()
    summer = dt.datetime(2026, 7, 15, 9, 30).astimezone()
    text = RS.join([US.join(["5", _secs(winter), "true"]), "garbage", US.join(["6", _secs(summer), "false"])])
    rows = mail_mac._parse_newest(text)
    assert [r["id"] for r in rows] == [6, 5] and [r["read"] for r in rows] == [False, True]
    got = [dt.datetime.fromisoformat(r["date"]) for r in rows]
    assert got == [summer, winter]  # wall-clock 9:30 both times, whatever the DST offset
    assert asyncio.run(mail_mac.newest_ids(10, _const(""))) == []  # no inboxes: nothing, not an error
    with pytest.raises(ToolError):
        asyncio.run(mail_mac.newest_ids(10, _const("execution error: Mail got an error")))


def _const(text: str) -> mail_mac.Runner:
    async def run(script: str, *args: str) -> str:
        return text

    return run


# --- message text from Scout's index ----------------------------------------------------------


def _store(m: dict[str, Any], body: str | None) -> None:
    mailindex._put(index.default(), m, body)


def test_bodies_come_from_the_index_without_asking_mail() -> None:
    m = _msg(11, 5)
    _store(m, "Can you walk Biscuit on Saturday?\n\nTo unsubscribe from these emails, click here.")

    async def mail_must_not_be_asked(script: str, *args: str) -> str:
        raise AssertionError("asked Mail for text Scout already has")

    got = asyncio.run(mail_mac.bodies([11], mail_must_not_be_asked))
    assert got[11]["body"] == "Can you walk Biscuit on Saturday?"  # footer already dropped
    assert (got[11]["sender"], got[11]["subject"], got[11]["date"]) == (m["sender"], m["subject"], m["date"])


def test_partial_or_missing_rows_are_read_from_mail_and_only_those() -> None:
    full, partial, absent = _msg(21, 5), _msg(22, 6), _msg(23, 7)
    _store(full, "Stored text.")
    _store(partial, None)  # a search result: sender and subject only
    mail = FakeMail([full, partial, absent])
    got = asyncio.run(mail_mac.bodies([21, 22, 23], mail.jxa, remember=False))
    assert mail.ran == ["read [22, 23]"]
    assert list(got) == [21, 22, 23] and got[21]["body"] == "Stored text." and got[22]["body"] == "text of 22"


def test_bodies_ask_mail_when_the_memory_service_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> Any:
        raise ToolError("Scout's memory service isn't running")

    monkeypatch.setattr(index, "default", down)
    mail = FakeMail([_msg(31, 5)])
    got = asyncio.run(mail_mac.bodies([31], mail.jxa, remember=False))
    assert got[31]["body"] == "text of 31" and mail.ran == ["read [31]"]


def test_a_stored_row_that_isnt_mails_index_row_is_read_from_mail() -> None:
    m = _msg(41, 5)
    _store(m, "Stored text.")
    idx = mail_mac.Index()
    idx.remember({**m, "subject": "Something else entirely", "mailbox": ""})  # Mail's index says otherwise
    mail = FakeMail([{**m, "subject": "Something else entirely"}])
    got = asyncio.run(mail_mac.bodies([41], mail.jxa, idx, remember=False))
    assert mail.ran == ["read [41]"] and got[41]["body"] == "text of 41"


def test_the_index_pass_never_takes_text_from_the_index() -> None:
    """Meeting notes are stored in full: the backfill must read Mail, not the 4000-character copy."""
    m = _msg(51, 5)
    _store(m, "Stored text.")
    mail = FakeMail([m])
    got = asyncio.run(mail_mac.bodies([51], mail.jxa, remember=False, stored=False))
    assert mail.ran == ["read [51]"] and got[51]["body"] == "text of 51"


def test_index_search_is_one_store_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _store(_msg(61, 5), "Lunch at the taco place on Thursday?")
    real = index.default()
    calls: list[str] = []

    class Counting:
        def __getattr__(self, name: str) -> Any:
            calls.append(name)
            return getattr(real, name)

    monkeypatch.setattr(index, "default", lambda: Counting())
    found = mailindex.search("taco", 5, 180)
    assert found is not None and [m["id"] for m in found["messages"]] == [61] and calls == ["search"]
    calls.clear()
    assert mailindex.search("nothing like this", 5, 180) is None and calls == ["search"]
