"""The Messages helper (native/messages) against a fixture chat.db, and its Python
client. Never the real ~/Library/Messages, never a permission prompt: the helper
gets --db/--socket/--token/--log paths in .tmp and a contacts file instead of
Contacts, and System Settings / Finder openers are fakes."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
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

from scout import messages_mac
from scout.mac import ToolError
from scout.messages_mac import Helper

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data" / "messages"
APPLE_EPOCH = 978_307_200

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None, reason="needs macOS and swiftc"
)


def _short_tmp() -> Path:
    # Unix socket paths are limited to 104 bytes: keep the directory short.
    d = ROOT / ".tmp" / "msg" / str(os.getpid())
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
    exe = home / "bin" / "scout-messages"
    sig = subprocess.run(  # noqa: S603
        ["/usr/bin/codesign", "-dv", str(exe)], capture_output=True, text=True, check=False
    ).stderr
    assert "Identifier=com.local.scout.messages" in sig
    assert "runtime" in sig  # hardened runtime: no DYLD_* injection into the FDA holder
    yield exe
    shutil.rmtree(_short_tmp(), ignore_errors=True)


def _apple(t: dt.datetime) -> int:
    return int((t.timestamp() - APPLE_EPOCH) * 1e9)


SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL,
    country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT, person_centric_id TEXT DEFAULT NULL);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
    state INTEGER, account_id TEXT, properties BLOB, chat_identifier TEXT, service_name TEXT,
    room_name TEXT, account_login TEXT, is_archived INTEGER DEFAULT 0, last_addressed_handle TEXT,
    display_name TEXT, group_id TEXT, is_filtered INTEGER DEFAULT 0, successful_query INTEGER);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
    replace INTEGER DEFAULT 0, service_center TEXT, handle_id INTEGER DEFAULT 0, subject TEXT,
    country TEXT, attributedBody BLOB, version INTEGER DEFAULT 0, type INTEGER DEFAULT 0,
    service TEXT, account TEXT, account_guid TEXT, error INTEGER DEFAULT 0, date INTEGER,
    date_read INTEGER, date_delivered INTEGER, is_delivered INTEGER DEFAULT 0,
    is_finished INTEGER DEFAULT 0, is_emote INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0,
    is_empty INTEGER DEFAULT 0, is_delayed INTEGER DEFAULT 0, is_auto_reply INTEGER DEFAULT 0,
    is_prepared INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0, is_system_message INTEGER DEFAULT 0,
    is_sent INTEGER DEFAULT 0, has_dd_results INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
    item_type INTEGER DEFAULT 0, other_handle INTEGER DEFAULT 0, group_title TEXT,
    group_action_type INTEGER DEFAULT 0, associated_message_guid TEXT,
    associated_message_type INTEGER DEFAULT 0, thread_originator_guid TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE, message_date INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id));
CREATE TABLE chat_handle_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE, UNIQUE(chat_id, handle_id));
CREATE INDEX message_idx_date ON message(date);
"""

EVA, SAM, STRANGER = "+15550102000", "sam@example.com", "+15550109999"
CONTACTS = {"Eva Rossi": ["+1 (555) 010-2000"], "Sam Lee": ["Sam@Example.com"]}


def build_fixture(path: Path, now: dt.datetime) -> None:
    # attributed_*.bin: NSArchiver.archivedData(withRootObject:) of an NSAttributedString
    # ("Running 10 min late 🙂 see you at Café Rio"; "Long message about the trip. " x 12 +
    # "End.", which needs the 2-byte length) and an NSMutableAttributedString with
    # attributes ("Dinner at 7? " + U+FFFC, an attachment placeholder), as Messages stores them.
    blob = {n: (DATA / f"attributed_{n}.bin").read_bytes() for n in ("short", "long", "mutable")}
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    db.executemany(
        "INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
        [(1, EVA, "iMessage"), (2, SAM, "iMessage"), (3, STRANGER, "SMS")],
    )
    chats = [
        (1, f"iMessage;-;{EVA}", 45, EVA, ""),
        (2, "iMessage;+;chat100", 43, "chat100", "Family"),
        (3, "iMessage;+;chat200", 43, "chat200", ""),
        (4, f"SMS;-;{STRANGER}", 45, STRANGER, ""),
    ]
    db.executemany(
        "INSERT INTO chat (ROWID, guid, style, chat_identifier, display_name) VALUES (?, ?, ?, ?, ?)", chats
    )
    db.executemany(
        "INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 1), (2, 2), (3, 1), (3, 2), (3, 3), (4, 3)]
    )

    def ago(**kw: float) -> int:
        return _apple(now - dt.timedelta(**kw))

    # (rowid, chat, handle, from_me, read, text, blob, date, service, reaction, attachment)
    rows: list[tuple[Any, ...]] = [
        (1, 1, 1, 0, 1, "Running late", None, ago(hours=2), "iMessage", 0, 0),
        (2, 1, 1, 0, 0, None, blob["short"], ago(hours=1), "iMessage", 0, 0),
        (3, 1, 0, 1, 1, "ok no rush", None, ago(minutes=50), "iMessage", 0, 0),
        (4, 2, 2, 0, 0, None, blob["long"], ago(minutes=40), "iMessage", 0, 0),
        (5, 2, 2, 0, 0, "Loved “ok no rush”", None, ago(minutes=30), "iMessage", 2000, 0),
        (6, 4, 3, 0, 0, "Your code is 123456", None, ago(minutes=20), "SMS", 0, 0),
        (7, 3, 1, 0, 1, None, blob["mutable"], ago(minutes=10), "iMessage", 0, 1),
        (8, 1, 1, 0, 1, "ancient history", None, ago(days=400), "iMessage", 0, 0),
    ]
    for rid, chat, handle, me, read, text, body, date, service, reaction, att in rows:
        db.execute(
            "INSERT INTO message (ROWID, guid, text, attributedBody, handle_id, date, is_from_me, is_read,"
            " service, associated_message_type, cache_has_attachments) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, f"guid-{rid}", text, body, handle, date, me, read, service, reaction, att),
        )
        db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rid, date))
    db.commit()
    db.close()


@dataclass
class Running:
    helper: Helper
    db: Path
    log: Path
    proc: subprocess.Popen[bytes]
    opened: list[list[str]]
    said: list[str]


def _start(binary: Path, name: str, *extra: str, db: Path | None = None) -> Running:
    d = _short_tmp() / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    if db is None:
        db = d / "chat.db"
        build_fixture(db, dt.datetime.now().astimezone())
    contacts = d / "contacts.json"
    contacts.write_text(json.dumps(CONTACTS))
    sock, token, log = d / "m.sock", d / "token", d / "access.log"
    proc = subprocess.Popen(  # noqa: S603
        [
            str(binary),
            "serve",
            "--db",
            str(db),
            "--socket",
            str(sock),
            "--token",
            str(token),
            "--log",
            str(log),
            "--contacts",
            str(contacts),
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
    opened: list[list[str]] = []
    said: list[str] = []
    helper = Helper(
        socket=sock,
        token=token,
        binary=binary,
        opener=opened.append,
        announce=said.append,
        watch_every_s=0.1,
        watch_for_s=10,
    )
    return Running(helper, db, log, proc, opened, said)


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


def _raw(h: Helper, req: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(h.socket))
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return dict(json.loads(line))

    return dict(run(go()))


def _token(h: Helper) -> str:
    return h.token.read_text().strip()


# --- the helper ---------------------------------------------------------------------------------


def test_recent_is_newest_first_and_decodes_attributed_body(running: Running) -> None:
    data = run(messages_mac.request("recent", {"limit": 50}, running.helper))
    msgs = data["messages"]
    assert [m["id"] for m in msgs] == [7, 6, 4, 3, 2, 1]  # no reaction (5), nothing > 365 days (8)
    by_id = {m["id"]: m for m in msgs}
    assert by_id[2]["text"] == "Running 10 min late 🙂 see you at Café Rio"  # attributedBody only
    assert by_id[4]["text"].startswith("Long message about the trip.")  # 2-byte length form
    assert by_id[7]["text"] == "Dinner at 7?"  # NSMutableString, attachment placeholder dropped
    assert by_id[7]["attachment"] is True
    assert by_id[2]["name"] == "Eva Rossi" and by_id[2]["handle"] == EVA
    assert by_id[4]["name"] == "Sam Lee" and by_id[4]["chat"] == "Family" and by_id[4]["group"]
    assert by_id[7]["chat"] == "Eva Rossi, Sam Lee, +15550109999"  # unnamed group: its people
    assert by_id[3]["from_me"] and by_id[3]["chat"] == "Eva Rossi" and "read" not in by_id[3]
    assert by_id[6]["service"] == "SMS" and "name" not in by_id[6]
    assert dt.datetime.fromisoformat(by_id[1]["date"]).tzinfo is not None


def test_long_text_is_cut_at_500_characters(running: Running) -> None:
    m = run(messages_mac.request("recent", {"limit": 3}, running.helper))["messages"]
    long = next(x for x in m if x["id"] == 4)
    assert len("Long message about the trip. " * 12 + "End.") < 500
    assert "truncated" not in long and long["text"].endswith("End.")


def test_from_matches_a_name_a_number_in_any_format_or_an_email(running: Running) -> None:
    h = running.helper
    by_name = run(messages_mac.request("from", {"contact": "eva"}, h))
    assert [m["id"] for m in by_name["messages"]] == [7, 2, 1]  # hers only, groups too, not mine
    assert by_name["matched"] == ["Eva Rossi"]
    by_number = run(messages_mac.request("from", {"contact": "(555) 010-2000"}, h))
    assert [m["id"] for m in by_number["messages"]] == [7, 2, 1]
    by_email = run(messages_mac.request("from", {"contact": "SAM@example.com"}, h))
    assert [m["id"] for m in by_email["messages"]] == [4]
    with pytest.raises(ToolError, match="no conversations with Zed"):
        run(messages_mac.request("from", {"contact": "Zed"}, h))


def test_unread_search_and_chat(running: Running) -> None:
    h = running.helper
    unread = run(messages_mac.request("unread", {}, h))
    assert [m["id"] for m in unread["messages"]] == [6, 4, 2]
    found = run(messages_mac.request("search", {"text": "cafe rio", "since_days": 1}, h))
    assert [m["id"] for m in found["messages"]] == [2]  # case- and accent-blind, in attributedBody
    assert found["scanned"] >= 1 and found["scan_capped"] is False
    assert run(messages_mac.request("search", {"text": "nothing like this"}, h))["messages"] == []
    fam = run(messages_mac.request("chat", {"chat": "family"}, h))
    assert fam["chat"] == "Family" and [m["id"] for m in fam["messages"]] == [4]
    eva = run(messages_mac.request("chat", {"chat": "Eva"}, h))  # her one-to-one chat
    assert eva["chat"] == "Eva Rossi" and [m["id"] for m in eva["messages"]] == [3, 2, 1]


def test_new_messages_in_the_wal_are_seen(running: Running) -> None:
    """Messages writes to chat.db-wal first; an `immutable` open would miss these."""
    writer = sqlite3.connect(running.db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    now = _apple(dt.datetime.now().astimezone())
    writer.execute(
        "INSERT INTO message (ROWID, guid, text, handle_id, date, service) VALUES (9, 'g9', 'fresh', 2, ?, 'iMessage')",
        (now,),
    )
    writer.execute("INSERT INTO chat_message_join VALUES (2, 9, ?)", (now,))
    writer.commit()
    try:
        msgs = run(messages_mac.request("recent", {"limit": 1}, running.helper))["messages"]
        assert msgs[0]["text"] == "fresh"
    finally:
        writer.close()


def test_it_never_writes_the_database(running: Running) -> None:
    before = hashlib.sha256(running.db.read_bytes()).hexdigest()
    mtime = running.db.stat().st_mtime_ns
    for op, args in (("recent", {}), ("unread", {}), ("search", {"text": "x"}), ("from", {"contact": "Sam"})):
        run(messages_mac.request(op, args, running.helper))
    assert hashlib.sha256(running.db.read_bytes()).hexdigest() == before
    assert running.db.stat().st_mtime_ns == mtime


def test_requests_are_checked(running: Running) -> None:
    h = running.helper
    token = _token(h)
    assert _raw(h, {"token": "0" * 64, "op": "recent"})["code"] == "bad_token"
    assert _raw(h, {"op": "status"})["code"] == "bad_token"
    assert _raw(h, {"token": token, "op": "sql", "args": {"q": "select 1"}})["code"] == "bad_request"
    for args in ({"limit": 51}, {"limit": 0}, {"limit": "5"}, {"limit": True}, {"query": "x"}):
        r = _raw(h, {"token": token, "op": "recent", "args": args})
        assert r["code"] == "bad_request", args
    assert _raw(h, {"token": token, "op": "unread", "args": {"since_days": 366}})["code"] == "bad_request"
    assert _raw(h, {"token": token, "op": "recent", "args": [1]})["code"] == "bad_request"
    assert _raw(h, {"token": token, "op": "search", "args": {"text": "x" * 101}})["code"] == "bad_request"
    assert _raw(h, {"token": token, "op": "from", "args": {"contact": "a\nb"}})["code"] == "bad_request"
    # Not JSON at all.
    reply = run(_send_bytes(h, b"hello\n"))
    assert reply["code"] == "bad_request"
    # Still serving after all that.
    assert _raw(h, {"token": token, "op": "status"})["access"] == "granted"


async def _send_bytes(h: Helper, data: bytes) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(h.socket))
    writer.write(data)
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return dict(json.loads(line))


def test_status_and_file_permissions(running: Running) -> None:
    st = run(messages_mac.status(running.helper))
    assert st["ok"] and st["access"] == "granted" and st["contacts"] == "file"
    assert st["db"] == str(running.db)
    assert running.helper.socket.stat().st_mode & 0o777 == 0o600
    assert running.helper.token.stat().st_mode & 0o777 == 0o600
    assert len(_token(running.helper)) == 64


def test_access_log_names_the_caller_but_never_the_content(running: Running) -> None:
    h = running.helper
    run(messages_mac.request("search", {"text": "cafe rio", "limit": 5}, h))
    run(messages_mac.request("from", {"contact": "Eva Rossi"}, h))
    _raw(h, {"token": "nope", "op": "recent"})
    text = running.log.read_text()
    assert "op=search limit=5" in text and "op=from" in text and "bad_token" in text
    assert "pid=" in text and "exe=/" in text
    for secret in ("cafe", "Café", "Eva", "Running", "123456"):
        assert secret not in text
    assert running.log.stat().st_mode & 0o777 == 0o600


def test_rate_limit(binary: Path) -> None:
    r = _start(binary, "rate", "--rate", "3")
    try:
        for _ in range(3):
            run(messages_mac.request("recent", {"limit": 1}, r.helper))
        with pytest.raises(ToolError, match="Too many"):
            run(messages_mac.request("recent", {"limit": 1}, r.helper))
        assert run(messages_mac.status(r.helper))["ok"]  # status is never limited
    finally:
        _stop(r)


def test_a_second_copy_leaves_the_running_one_alone(running: Running, binary: Path) -> None:
    h = running.helper
    p = subprocess.run(  # noqa: S603
        [
            str(binary),
            "serve",
            "--db",
            str(running.db),
            "--socket",
            str(h.socket),
            "--token",
            str(h.token.with_name("token2")),
            "--log",
            str(running.log),
            "--contacts",
            "none",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert p.returncode == 1 and "already serving" in p.stderr
    assert run(messages_mac.status(h))["access"] == "granted"


def test_missing_access_guides_setup_once_and_says_got_it(binary: Path) -> None:
    """No Full Disk Access looks like a refused open; chmod 000 stands in for it."""
    r = _start(binary, "fda")
    clock = [1000.0]
    r.helper.clock = lambda: clock[0]
    try:
        r.db.chmod(0)
        if os.access(r.db, os.R_OK):
            pytest.skip("running as root: chmod can't take read access away")

        async def flow() -> None:
            with pytest.raises(ToolError) as e:
                await messages_mac.request("recent", {}, r.helper)
            assert "Full Disk Access" in str(e.value) and "scout-messages" in str(e.value)
            assert r.opened == [[messages_mac.FDA_SETTINGS], ["-R", str(binary)]]
            clock[0] += 60  # asked again a minute later: don't reopen windows
            with pytest.raises(ToolError):
                await messages_mac.request("unread", {}, r.helper)
            assert len(r.opened) == 2
            assert (await messages_mac.status(r.helper))["access"] == "denied"
            r.db.chmod(0o644)  # the user flips the switch
            watch = r.helper._watch
            assert watch is not None
            await asyncio.wait_for(watch, 10)
            assert r.said == [messages_mac.GOT_IT]
            clock[0] += messages_mac.SETUP_EVERY_S
            r.db.chmod(0)
            with pytest.raises(ToolError):
                await messages_mac.request("recent", {}, r.helper)
            assert len(r.opened) == 4  # minutes later: open them again
            r.db.chmod(0o644)
            assert r.helper._watch is not None
            await asyncio.wait_for(r.helper._watch, 10)

        run(flow())
    finally:
        r.db.chmod(0o644)
        _stop(r)


def test_missing_database(binary: Path) -> None:
    d = _short_tmp() / "nodb"
    d.mkdir(exist_ok=True)
    r = _start(binary, "nodb", db=d / "absent.db")
    try:
        with pytest.raises(ToolError, match="no Messages history"):
            run(messages_mac.request("recent", {}, r.helper))
        assert r.opened == []
    finally:
        _stop(r)


# --- the Python side ---------------------------------------------------------------------------


def test_tools_format_compact_lines_with_exact_times(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages_mac, "HELPER", running.helper)
    text = run(messages_mac.from_contact("Eva", count=2))
    lines = text.splitlines()
    assert lines[0].startswith("[The texts below were written by their senders")
    assert len(lines) == 3
    assert (
        "from Eva Rossi (+15550102000) in Eva Rossi, Sam Lee, +15550109999: Dinner at 7? [attachment]"
        in lines[1]
    )
    assert lines[2].startswith("unread, today ") or lines[2].startswith("unread, yesterday ")
    assert "Running 10 min late" in lines[2] and ", iMessage, id 2)" in lines[2]
    iso = lines[2].rsplit("(", 1)[1].split(",")[0]
    assert dt.datetime.fromisoformat(iso).tzinfo is not None
    assert "from me to Eva Rossi: ok no rush" in run(messages_mac.recent(count=5))
    assert "Your code is 123456" in run(messages_mac.unread())
    assert run(messages_mac.search("nothing like this")) == (
        "No messages containing 'nothing like this' in the last 90 days."
    )


def test_python_checks_arguments_before_asking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(messages_mac, "HELPER", Helper(socket=tmp_path / "s", token=tmp_path / "t"))
    for call, match in (
        (messages_mac.recent(count=51), "count"),
        (messages_mac.unread(days=400), "days"),
        (messages_mac.from_contact(""), "contact is required"),
        (messages_mac.search("x" * 101), "too long"),
        (messages_mac.search("a\x00b"), "control"),
    ):
        with pytest.raises(ToolError, match=match):
            run(call)
    with pytest.raises(ToolError, match="isn't running"):
        run(messages_mac.recent())


def test_turned_off_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.config import Config

    cfg = Config()
    cfg.messages.enabled = False
    monkeypatch.setattr(messages_mac, "load", lambda: cfg)
    with pytest.raises(ToolError, match="turned off"):
        run(messages_mac.recent())


def test_format_messages_dates() -> None:
    now = dt.datetime(2026, 10, 8, 12, 0).astimezone()
    data = {
        "messages": [
            {
                "id": 1,
                "date": dt.datetime(2026, 10, 7, 21, 5).astimezone().isoformat(),
                "from_me": False,
                "handle": "+1555",
                "text": "hi",
                "read": True,
                "service": "SMS",
                "group": False,
            },
        ],
        "scan_capped": True,
    }
    out = messages_mac.format_messages(data, "x", now)
    assert "yesterday 9:05 PM, from +1555: hi (" in out
    assert out.endswith("narrow it with fewer days)")
    assert messages_mac.format_messages({"messages": []}, "texts") == "No texts."


def test_the_default_opener_refuses_under_pytest() -> None:
    with pytest.raises(RuntimeError):
        messages_mac._open_system([messages_mac.FDA_SETTINGS])
