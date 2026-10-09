"""Mail's index (the "Envelope Index") through the scout-messages helper, against a
fixture database, and mail_mac's use of it: same answers as Mail scripting, a
fallback when the index can't answer, and the check that Mail's message for an
index id really is that row. Never the real ~/Library/Mail, never Mail itself:
the helper gets --mail-index, and a fake runner stands in for Mail scripting."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scout import mail_mac, messages_mac
from scout.mac import ToolError
from scout.messages_mac import Helper

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None, reason="needs macOS and swiftc"
)


def _short_tmp() -> Path:
    d = ROOT / ".tmp" / "mix" / str(os.getpid())  # Unix socket paths: at most 104 bytes
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="module")
def binary() -> Iterator[Path]:
    home = _short_tmp() / "home"
    r = subprocess.run(  # noqa: S603  our own build script
        [str(ROOT / "scripts" / "build-messages.sh")],
        env={**os.environ, "SCOUT_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "warning:" not in r.stderr + r.stdout, r.stderr
    yield home / "bin" / "scout-messages"
    shutil.rmtree(_short_tmp(), ignore_errors=True)


# The Envelope Index as recent macOS (Mail V10) lays it out, trimmed of tables the
# helper never reads.
SCHEMA = """
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT COLLATE NOCASE, comment TEXT,
    UNIQUE(address, comment));
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT COLLATE RTRIM, UNIQUE(subject));
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT UNIQUE, total_count INTEGER DEFAULT 0,
    unread_count INTEGER DEFAULT 0, deleted_count INTEGER DEFAULT 0, unseen_count INTEGER DEFAULT 0,
    unread_count_adjusted_for_duplicates INTEGER DEFAULT 0, change_identifier TEXT, source INTEGER,
    alleged_change_identifier TEXT);
CREATE TABLE messages (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL DEFAULT 0,
    global_message_id INTEGER NOT NULL DEFAULT 0, remote_id INTEGER, document_id TEXT,
    sender INTEGER, subject_prefix TEXT, subject INTEGER NOT NULL, summary INTEGER,
    date_sent INTEGER, date_received INTEGER, mailbox INTEGER NOT NULL, remote_mailbox INTEGER,
    flags INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0,
    flagged INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0, conversation_id INTEGER NOT NULL DEFAULT 0,
    date_last_viewed INTEGER, list_id_hash INTEGER, unsubscribe_type INTEGER,
    searchable_message INTEGER, brand_indicator INTEGER, display_date INTEGER, color TEXT,
    type INTEGER, fuzzy_ancestor INTEGER, automated_conversation INTEGER DEFAULT 0,
    root_status INTEGER DEFAULT -1, flag_color INTEGER, is_urgent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE labels (message_id INTEGER NOT NULL, mailbox_id INTEGER NOT NULL,
    PRIMARY KEY(message_id, mailbox_id));
CREATE INDEX messages_mailbox_date_received_index ON messages(mailbox, date_received);
CREATE INDEX messages_date_received_index ON messages(date_received);
"""

GOOGLE, ICLOUD, EXCHANGE = (
    "7A1C2B3D-0000-4000-8000-000000000001",
    "7A1C2B3D-0000-4000-8000-000000000002",
    "7A1C2B3D-0000-4000-8000-000000000003",
)
MAILBOXES = [
    (1, f"imap://{GOOGLE}/INBOX"),
    (2, f"imap://{GOOGLE}/%5BGmail%5D/All%20Mail"),
    (3, f"imap://{ICLOUD}/INBOX"),
    (4, f"imap://{ICLOUD}/Sent%20Messages"),
    (5, f"ews://{EXCHANGE}/Inbox"),
    (6, f"imap://{GOOGLE}/Inbox%20Old"),  # not an inbox: the path doesn't end in /INBOX
]


@dataclass
class Msg:
    rowid: int
    mailbox: int
    address: str
    comment: str
    subject: str
    ago: dt.timedelta
    read: int = 0
    deleted: int = 0
    prefix: str = ""
    label: int | None = None  # a labels row (Gmail) putting it in this mailbox


H, D = dt.timedelta(hours=1), dt.timedelta(days=1)
FIXTURE = [
    Msg(1, 1, "pat@acme.com", "Pat Lee", "Moving our 1:1", 2 * H),
    Msg(2, 3, "pat@work.example", "Lee, Pat", "Q4 planning", 3 * H, read=1, prefix="Re: "),
    Msg(3, 5, "office@smile.example", 'Dr. "Smile" Office', "Café ☕ résumé — 日本語", D),
    Msg(4, 4, "me@icloud.example", "Me", "Lunch?", H / 6),  # Sent: not an inbox
    Msg(5, 1, "pat@acme.com", "Pat Lee", "Deleted thing", H / 12, deleted=1),
    Msg(6, 2, "eva@gmail.example", "Eva", "In the inbox by label", H / 2, label=1),
    Msg(7, 2, "eva@gmail.example", "Eva", "Archived", H / 3),  # All Mail, no inbox label
    Msg(8, 1, "pat@acme.com", "Pat Lee", "Ancient", 400 * D),
    Msg(9, 1, "billing@example.com", "", "Old invoice", 60 * D, read=1),
    Msg(10, 6, "pat@acme.com", "Pat Lee", "Not an inbox either", H / 4),
    Msg(11, 3, "Sam@Example.com", "sam@example.com", "Name is the address", 5 * H),
]


def build_index(path: Path, now: dt.datetime, msgs: list[Msg], *, drop: str = "") -> None:
    """A fixture Envelope Index; `drop` names a messages column to leave out (an
    unexpected schema)."""
    schema = SCHEMA
    if drop:
        schema = schema.replace(f" {drop} INTEGER NOT NULL DEFAULT 0,", "", 1)
    db = sqlite3.connect(path)
    db.executescript(schema)
    db.execute("PRAGMA journal_mode=WAL")
    db.executemany("INSERT INTO mailboxes (ROWID, url) VALUES (?, ?)", MAILBOXES)
    addresses: dict[tuple[str, str], int] = {}
    subjects: dict[str, int] = {}
    cols = "ROWID, sender, subject_prefix, subject, date_received, mailbox, read, deleted"
    if drop:
        cols = cols.replace(f", {drop}", "")
    for m in msgs:
        a = addresses.setdefault((m.address, m.comment), len(addresses) + 1)
        s = subjects.setdefault(m.subject, len(subjects) + 1)
        values: dict[str, Any] = {
            "ROWID": m.rowid,
            "sender": a,
            "subject_prefix": m.prefix or None,
            "subject": s,
            "date_received": int((now - m.ago).timestamp()),
            "mailbox": m.mailbox,
            "read": m.read,
            "deleted": m.deleted,
        }
        names = [c.strip() for c in cols.split(",")]
        db.execute(
            f"INSERT INTO messages ({cols}) VALUES ({','.join('?' * len(names))})",  # noqa: S608  test fixture
            [values[c] for c in names],
        )
        if m.label is not None:
            db.execute("INSERT INTO labels VALUES (?, ?)", (m.rowid, m.label))
    db.executemany("INSERT INTO addresses VALUES (?, ?, ?)", [(i, a, c) for (a, c), i in addresses.items()])
    db.executemany("INSERT INTO subjects VALUES (?, ?)", [(i, s) for s, i in subjects.items()])
    db.commit()
    db.close()


@dataclass
class Running:
    helper: Helper
    index: Path
    log: Path
    proc: subprocess.Popen[bytes]


def _start(binary: Path, name: str, *extra: str, index: Path | None = None, drop: str = "") -> Running:
    d = _short_tmp() / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    if index is None:
        index = d / "Envelope Index"
        build_index(index, dt.datetime.now().astimezone(), FIXTURE, drop=drop)
    sock, token, log = d / "m.sock", d / "token", d / "access.log"
    proc = subprocess.Popen(  # noqa: S603
        [
            str(binary),
            "serve",
            "--db",
            str(d / "no-chat.db"),  # Messages isn't under test here
            "--mail-index",
            str(index),
            "--socket",
            str(sock),
            "--token",
            str(token),
            "--log",
            str(log),
            "--contacts",
            "none",
            *extra,
        ],
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if token.exists() and sock.exists():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        raise AssertionError("helper didn't start")
    return Running(Helper(socket=sock, token=token, binary=binary), index, log, proc)


def _stop(r: Running) -> None:
    r.proc.terminate()
    try:
        r.proc.wait(5)
    except subprocess.TimeoutExpired:
        r.proc.kill()
        r.proc.wait()
    r.helper.socket.unlink(missing_ok=True)


@pytest.fixture
def running(binary: Path) -> Iterator[Running]:
    r = _start(binary, "main", "--rate", "1000")
    yield r
    _stop(r)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def mail(r: Running, op: str, **args: Any) -> dict[str, Any]:
    return dict(run(messages_mac.mail(op, args, r.helper)))


def _raw(h: Helper, req: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(h.socket))
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return dict(json.loads(line))

    return dict(run(go()))


# --- the helper ---------------------------------------------------------------------------------


def test_status_checks_the_schema_and_counts_the_inbox(running: Running) -> None:
    st = mail(running, "mail_status")
    assert st["access"] == "granted" and st["path"] == str(running.index)
    assert st["usable"] is True and st["schema_ok"] is True and st["missing"] == []
    assert st["inbox_mailboxes"] == 3  # Google and iCloud INBOX, Exchange Inbox
    assert st["inbox_messages"] == 7  # 1 2 3 6(label) 8 9 11; not Sent, deleted, All Mail or Inbox Old
    assert any("labels" in n for n in st["notes"]) and any("subject_prefix" in n for n in st["notes"])


def test_recent_is_the_inbox_newest_first_with_display_senders(running: Running) -> None:
    msgs = mail(running, "mail_recent", limit=50)["messages"]
    assert [m["id"] for m in msgs] == [6, 1, 2, 11, 3]  # 30 days: not 9 (60 days) or 8 (400)
    by = {m["id"]: m for m in msgs}
    assert by[1]["sender"] == "Pat Lee <pat@acme.com>" and by[1]["read"] is False
    assert by[2]["sender"] == '"Lee, Pat" <pat@work.example>'
    assert by[2]["subject"] == "Re: Q4 planning" and by[2]["read"] is True  # subject_prefix kept
    assert by[3]["sender"] == '"Dr. \\"Smile\\" Office" <office@smile.example>'
    assert by[3]["subject"] == "Café ☕ résumé — 日本語"
    assert by[11]["sender"] == "Sam@Example.com"  # the name is just the address
    assert by[6]["mailbox"] == f"imap://{GOOGLE}/INBOX"  # stored in All Mail, inbox by label
    assert by[3]["mailbox"] == f"ews://{EXCHANGE}/Inbox"
    for m in msgs:
        assert set(m) == {"id", "date", "sender", "subject", "read", "mailbox"}  # envelopes only
        assert dt.datetime.fromisoformat(m["date"]).tzinfo is not None
    t = dt.datetime.fromisoformat(by[1]["date"])
    assert abs((dt.datetime.now().astimezone() - t) - 2 * H) < dt.timedelta(minutes=1)


def test_unread_only_and_days(running: Running) -> None:
    assert [m["id"] for m in mail(running, "mail_recent", unread_only=True)["messages"]] == [6, 1, 11, 3]
    assert [m["id"] for m in mail(running, "mail_recent", limit=2)["messages"]] == [6, 1]
    assert [m["id"] for m in mail(running, "mail_recent", since_days=365, limit=50)["messages"]][-1] == 9


def test_search_matches_sender_or_subject_ignoring_case_and_accents(running: Running) -> None:
    def ids(text: str, **kw: Any) -> list[int]:
        return [m["id"] for m in mail(running, "mail_search", text=text, **kw)["messages"]]

    assert ids("PAT LEE") == [1]  # a name ("Lee, Pat" doesn't contain "pat lee")
    assert ids("lee") == [1, 2]
    assert ids("acme.com") == [1]  # an address
    assert ids("CAFE") == [3] and ids("resume") == [3]  # accents and case ignored
    assert ids("日本語") == [3]
    assert ids("q4 PLANNING") == [2]
    assert ids("invoice") == [9]  # 60 days: within the default 180
    assert ids("invoice", since_days=30) == []
    assert ids("ancient") == []  # 400 days
    assert ids("deleted thing") == [] and ids("lunch") == [] and ids("archived") == []
    assert ids("by label") == [6]
    r = mail(running, "mail_search", text="pat")
    assert r["scanned"] >= 3 and r["scan_capped"] is False and r["since_days"] == 180


def test_requests_are_checked(running: Running) -> None:
    h = running.helper
    token = h.token.read_text().strip()
    bad = [
        ("mail_recent", {"limit": 51}),
        ("mail_recent", {"limit": 0}),
        ("mail_recent", {"since_days": 366}),
        ("mail_recent", {"unread_only": "yes"}),
        ("mail_recent", {"unread_only": 1}),
        ("mail_recent", {"text": "x"}),
        ("mail_search", {}),
        ("mail_search", {"text": "x" * 101}),
        ("mail_search", {"text": "a\nb"}),
        ("mail_status", {"limit": 1}),
        ("mail_sql", {}),
    ]
    for op, args in bad:
        assert _raw(h, {"token": token, "op": op, "args": args})["code"] == "bad_request", (op, args)
    assert _raw(h, {"token": "0" * 64, "op": "mail_recent"})["code"] == "bad_token"
    assert _raw(h, {"op": "mail_status"})["code"] == "bad_token"


def test_it_never_writes_the_index_and_sees_the_wal(running: Running) -> None:
    before = hashlib.sha256(running.index.read_bytes()).hexdigest()
    for op, args in (("mail_status", {}), ("mail_recent", {}), ("mail_search", {"text": "pat"})):
        mail(running, op, **args)
    assert hashlib.sha256(running.index.read_bytes()).hexdigest() == before
    writer = sqlite3.connect(running.index)  # Mail writes new mail to the -wal first
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO messages (ROWID, sender, subject, date_received, mailbox) VALUES (99, 1, 1, ?, 1)",
        (int(time.time()),),
    )
    writer.commit()
    try:
        assert mail(running, "mail_recent", limit=1)["messages"][0]["id"] == 99
    finally:
        writer.close()


def test_access_log_has_numbers_never_search_words(running: Running) -> None:
    mail(running, "mail_search", text="cafe resume", limit=5)
    mail(running, "mail_recent", limit=3)
    text = running.log.read_text()
    assert "op=mail_search limit=5" in text and "op=mail_recent limit=3" in text and "pid=" in text
    for secret in ("cafe", "resume", "Pat", "acme"):
        assert secret not in text


def test_rate_limit_covers_lookups_not_status(binary: Path) -> None:
    r = _start(binary, "rate", "--rate", "2")
    try:
        mail(r, "mail_recent")
        mail(r, "mail_search", text="pat")
        with pytest.raises(ToolError, match="rate_limited"):
            mail(r, "mail_recent")
        assert mail(r, "mail_status")["usable"] is True
    finally:
        _stop(r)


def test_an_unexpected_schema_is_reported_not_used(binary: Path) -> None:
    r = _start(binary, "schema", drop="read")
    try:
        st = mail(r, "mail_status")
        assert st["usable"] is False and st["schema_ok"] is False and st["missing"] == ["messages.read"]
        with pytest.raises(ToolError, match=r"schema: .*messages\.read"):
            mail(r, "mail_recent")
    finally:
        _stop(r)


def test_without_deleted_column_it_still_answers(binary: Path) -> None:
    r = _start(binary, "nodel", drop="deleted")
    try:
        st = mail(r, "mail_status")
        assert st["usable"] is True and any("deleted" in n for n in st["notes"])
        assert 5 in [m["id"] for m in mail(r, "mail_recent", limit=50)["messages"]]
    finally:
        _stop(r)


def test_missing_and_unreadable_index(binary: Path) -> None:
    d = _short_tmp() / "noidx"
    d.mkdir(exist_ok=True)
    r = _start(binary, "noidx", index=d / "absent")
    try:
        st = mail(r, "mail_status")
        assert st["access"] == "missing" and st["usable"] is False
        with pytest.raises(ToolError, match="missing_index"):
            mail(r, "mail_recent")
    finally:
        _stop(r)
    r = _start(binary, "denied")
    try:
        r.index.chmod(0)  # stands in for no Full Disk Access
        if os.access(r.index, os.R_OK):
            pytest.skip("running as root")
        st = mail(r, "mail_status")
        assert st["access"] == "denied" and st["usable"] is False
        with pytest.raises(ToolError, match="no_access"):
            mail(r, "mail_recent")
        r.index.chmod(0o644)
        assert mail(r, "mail_status")["usable"] is True
    finally:
        r.index.chmod(0o644)
        _stop(r)


def test_sixty_thousand_messages_answer_fast(binary: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The real inbox that took Mail scripting 12-15 s per sender lookup had 58,732
    messages. Same order of rows here, with the indexes the fixture schema declares."""
    d = _short_tmp() / "big"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    now = dt.datetime.now().astimezone()
    msgs = [
        Msg(
            i,
            (1, 1, 1, 3, 5, 2)[i % 6] if i <= 6000 else (1, 1, 1, 3, 5)[i % 5],  # 1,000 not in an inbox
            f"sender{i % 900}@list{i % 37}.example",
            f"Sender {i % 900}",
            f"Newsletter {i % 5000} about things",
            dt.timedelta(minutes=8 * i),  # 60,000 rows over 333 days
            read=int(i % 11 != 0),
        )
        for i in range(1, 60_001)
    ]
    msgs.append(Msg(60_001, 1, "rare.person@unique.example", "Rare Person", "One of a kind", 200 * D))
    build_index(d / "Envelope Index", now, msgs)
    r = _start(binary, "bigrun", "--rate", "1000", index=d / "Envelope Index")
    try:
        times: dict[str, float] = {}
        for name, op, args in (
            ("status", "mail_status", {}),
            ("recent 10", "mail_recent", {"limit": 10}),
            ("unread 20", "mail_recent", {"limit": 20, "unread_only": True}),
            ("search, rare sender (scans all)", "mail_search", {"text": "rare person", "since_days": 365}),
            ("search, common", "mail_search", {"text": "sender 12", "limit": 10, "since_days": 365}),
            ("search, nothing (scans all)", "mail_search", {"text": "zzz nothing", "since_days": 365}),
        ):
            t0 = time.monotonic()
            data = mail(r, op, **args)
            times[name] = (time.monotonic() - t0) * 1000
            if name.startswith("search, rare"):
                assert [m["id"] for m in data["messages"]] == [60_001]
                assert data["scanned"] == 59_001  # every inbox row in the year
        assert mail(r, "mail_status")["inbox_messages"] == 59_001
        with capsys.disabled():
            print(
                "\n60k-row Envelope Index, round trip ms: "
                + json.dumps({k: round(v) for k, v in times.items()})
            )
        assert max(times.values()) < 3000
    finally:
        _stop(r)


# --- mail_mac on top -----------------------------------------------------------------------------


class FakeMail:
    """Mail scripting: answers each script from a table; records which ran."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.ran: list[str] = []

    async def __call__(self, script: str, *args: str) -> str:
        name = next(k for k in ("_LIST", "_READ_MANY", "_READ", "_FIND") if getattr(mail_mac, k) is script)
        self.ran.append(name)
        reply = self.replies.get(name)
        if callable(reply):
            reply = reply(*args)
        return json.dumps(reply if reply is not None else {"error": f"no inbox message with id {args[0]}"})


def _index(r: Running) -> mail_mac.Index:
    return mail_mac.Index(helper=r.helper)


def test_listing_comes_from_the_index_in_the_same_shape(running: Running) -> None:
    fake = FakeMail({})
    idx = _index(running)
    data = run(mail_mac.listing(3, False, None, fake, idx))
    assert fake.ran == []  # Mail scripting not asked
    assert [m["id"] for m in data["messages"]] == [6, 1, 2]
    assert all(set(m) == {"id", "date", "sender", "subject", "read"} for m in data["messages"])
    assert data["days"] == mail_mac.RECENT_DAYS and data["considered"] == 3
    found = run(mail_mac.message_data(5, False, "invoice", fake, idx))
    assert [m["id"] for m in found] == [9]
    out = mail_mac.format_list(data, "messages")
    assert "id 6: unread, " in out and "from Eva <eva@gmail.example>: In the inbox by label" in out


def test_falls_back_to_mail_scripting_when_the_index_cant_answer(tmp_path: Path) -> None:
    listed = {
        "messages": [
            {"id": 41, "date": "2026-10-01T14:00:00Z", "sender": "a@b.c", "subject": "x", "read": True}
        ]
    }
    fake = FakeMail({"_LIST": listed})
    idx = mail_mac.Index(helper=Helper(socket=tmp_path / "none.sock", token=tmp_path / "none"))
    assert run(mail_mac.listing(1, False, None, fake, idx))["messages"][0]["id"] == 41
    assert fake.ran == ["_LIST"]


def test_falls_back_when_the_schema_is_unexpected(binary: Path) -> None:
    r = _start(binary, "pyschema", drop="read")
    try:
        fake = FakeMail({"_LIST": {"messages": []}})
        run(mail_mac.listing(1, False, None, fake, _index(r)))
        assert fake.ran == ["_LIST"]
    finally:
        _stop(r)


def _as_mail(row: dict[str, Any], **change: Any) -> dict[str, Any]:
    """What Mail scripting returns for an index row (UTC date, maybe changed)."""
    utc = dt.datetime.fromisoformat(row["date"]).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": row["id"],
        "date": utc,
        "sender": row["sender"],
        "subject": row["subject"],
        "to": ["me@example.com"],
        "cc": [],
        "body": "the text",
        "truncated": False,
        **change,
    }


def test_reading_an_index_id_checks_it_is_that_message(running: Running) -> None:
    idx = _index(running)
    rows = {m["id"]: m for m in run(mail_mac.listing(5, False, None, FakeMail({}), idx))["messages"]}
    row2 = {**rows[2], "subject": "RE:  q4   planning"}  # Mail may spell the prefix and spaces differently
    fake = FakeMail(
        {"_READ": _as_mail(row2), "_READ_MANY": {"messages": [_as_mail(rows[1]), _as_mail(rows[2])]}}
    )
    assert run(mail_mac.message(2, fake, idx))["body"] == "the text"
    got = run(mail_mac.bodies([1, 2], fake, idx))
    assert set(got) == {1, 2} and idx.ids_trusted is True
    assert "_FIND" not in fake.ran


def test_an_id_mismatch_is_loud_and_finds_the_right_message(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    idx = _index(running)
    rows = {m["id"]: m for m in run(mail_mac.listing(5, False, None, FakeMail({}), idx))["messages"]}
    right = _as_mail(rows[1], id=777, body="Pat's real text")
    fake = FakeMail({"_READ": _as_mail(rows[2], id=1, body="someone else's text"), "_FIND": right})
    with caplog.at_level(logging.WARNING, logger="scout.mail_mac"):
        m = run(mail_mac.message(1, fake, idx))
    assert m["body"] == "Pat's real text" and m["id"] == 1 and m["mail_id"] == 777
    assert fake.ran == ["_READ", "_FIND"]
    assert "MAIL INDEX ID MISMATCH" in caplog.text and "different subject, sender, date" in caplog.text
    assert "Moving our" not in caplog.text and "Q4" not in caplog.text  # field names, not their text
    assert idx.ids_trusted is False
    fake2 = FakeMail({"_LIST": {"messages": []}})  # from now on, listings come from Mail scripting
    run(mail_mac.listing(3, False, None, fake2, idx))
    assert fake2.ran == ["_LIST"]


def test_a_missing_or_wrong_id_in_bodies_is_found_or_left_out(running: Running) -> None:
    idx = _index(running)
    rows = {m["id"]: m for m in run(mail_mac.listing(5, False, None, FakeMail({}), idx))["messages"]}

    def find(subject: str, date: str, mailbox: str, limit: str) -> dict[str, Any]:
        assert mailbox == f"imap://{GOOGLE}/INBOX" and limit == str(mail_mac._MAX_BODY)
        return (
            _as_mail(rows[1], id=555) if "1:1" in subject else {"error": "couldn't find that message in Mail"}
        )

    fake = FakeMail(
        {"_READ_MANY": {"messages": [_as_mail(rows[2]), _as_mail(rows[3], id=6)]}, "_FIND": find}
    )  # 1 missing, 6 is someone else's message
    got = run(mail_mac.bodies([1, 2, 6], fake, idx))
    assert set(got) == {1, 2} and got[1]["mail_id"] == 555
    assert fake.ran.count("_FIND") == 2 and idx.ids_trusted is False


def test_scripting_ids_are_never_checked_against_index_rows(running: Running) -> None:
    idx = _index(running)
    run(mail_mac.listing(5, False, None, FakeMail({}), idx))
    idx.ids_trusted = False  # the index is off; Mail scripting lists, and its id 1 is its own
    listed = {
        "messages": [
            {"id": 1, "date": "2026-10-01T14:00:00Z", "sender": "x@y.z", "subject": "s", "read": True}
        ]
    }
    fake = FakeMail(
        {
            "_LIST": listed,
            "_READ": {"id": 1, "date": "2026-10-01T14:00:00Z", "sender": "x@y.z", "subject": "s"},
        }
    )
    run(mail_mac.listing(1, False, None, fake, idx))
    assert run(mail_mac.message(1, fake, idx))["subject"] == "s"
    assert fake.ran == ["_LIST", "_READ"]


def test_verify_index_ids_reports_without_message_text(running: Running) -> None:
    idx = _index(running)
    rows = mail(running, "mail_recent", limit=3)["messages"]
    same = FakeMail(
        {
            "_LIST": {"messages": [_as_mail(m) for m in rows]},
            "_READ_MANY": {"messages": [_as_mail(m, body="") for m in rows]},
        }
    )
    report = run(mail_mac.verify_index_ids(3, same, idx))
    assert "verdict: index ids are Mail's ids" in report and "same ids in both listings: 3/3" in report
    assert "Mail's message for each index id is that row: 3/3" in report
    for m in rows:
        assert m["subject"] not in report and m["sender"] not in report
    swapped = [_as_mail(rows[1], id=rows[0]["id"]), _as_mail(rows[0], id=rows[1]["id"]), _as_mail(rows[2])]
    off = FakeMail({"_LIST": {"messages": swapped}, "_READ_MANY": {"messages": swapped}})
    report = run(mail_mac.verify_index_ids(3, off, idx))
    assert "MISMATCH" in report and "that row: 1/3" in report and "'subject'" in report
    gone = mail_mac.Index(
        helper=Helper(socket=running.helper.socket.with_name("x.sock"), token=running.helper.token)
    )
    assert run(mail_mac.verify_index_ids(3, same, gone)).startswith("index: not reachable")
