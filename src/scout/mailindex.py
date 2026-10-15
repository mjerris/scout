"""The inbox in Scout's local store, so mail search is local and fast and matches
message text by words and meaning, not just subjects and senders.

A background pass (while Scout is idle) asks Mail.app for the newest messages and
reads the ones not yet stored, a few dozen at a time; old ones age out after
`[index] mail_days`. It uses only the Mail access Scout already has. Searches use the
index when it has mail and fall back to asking Mail.app when it finds nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Any

from . import appdb, index, mail_mac
from .config import load
from .mac import ToolError
from .summary import essential

log = logging.getLogger(__name__)

KIND = "mail"
NEWEST = 100  # messages listed per pass
BODIES = 50  # message texts read per pass
CHUNK = 10  # per call to Mail (25 at a time ran past 30 s live)
MESSAGE_IDS = 20  # already-indexed messages given their Message-ID per pass (no text read)
TIMEOUT_S = 90.0  # background work: Mail can be slow on a big inbox


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return time.time()


async def _slow_jxa(script: str, *args: str) -> str:
    return await mail_mac._jxa(script, *args, timeout=TIMEOUT_S)


def _mail_id(m: dict[str, Any]) -> int:
    """Mail's own id for a message: a message found by subject and date (mail_mac._find)
    carries it as mail_id, its "id" being the index row's, which Mail may not know."""
    return int(m.get("mail_id") or m["id"])


# Meeting notes and transcripts that arrive by email (docs/tasks-design.md): from a
# notes service's own address or domain (the address itself, not the display name; a
# subdomain counts, a look-alike doesn't), with that service's kind of subject. They're
# stored as meetings too, in full.
_SUMMARY = re.compile(r"\b(?:summary|notes|recap|transcript)\b", re.I)
_GEMINI = re.compile(r"^\s*notes\s*:|\bnotes by gemini\b", re.I)
_MEETING_ADDRESSES = {
    "gemini-notes@google.com": _GEMINI,
    "meetings-noreply@google.com": _GEMINI,
    "meet-recordings-noreply@google.com": _GEMINI,
}
_MEETING_DOMAINS = {
    "zoom.us": re.compile(r"\bmeeting summary\b", re.I),
    "otter.ai": _SUMMARY,
    "fireflies.ai": _SUMMARY,
    "fathom.video": _SUMMARY,
    "tldv.io": _SUMMARY,
    "read.ai": _SUMMARY,
    "grain.com": _SUMMARY,
    "email.teams.microsoft.com": re.compile(r"\brecap\b", re.I),
}
MEETING_QUERIES = ("gemini-notes@google.com", "meetings-noreply@google.com", "zoom.us", "otter.ai",
                   "fireflies.ai", "fathom.video", "tldv.io", "teams.microsoft.com")  # fmt: skip
MEETING_CHARS = 60_000


def sender_address(sender: str) -> str:
    """The address in a From line: the one in <...>, or the whole line when it's only an
    address; "" otherwise (a display name alone proves nothing)."""
    if m := re.search(r"<\s*([^<>\s@]+@[^<>\s@]+)\s*>", sender or ""):
        return m.group(1).lower()
    s = (sender or "").strip()
    return s.lower() if re.fullmatch(r"[^\s@<>\"]+@[^\s@<>\"]+", s) else ""


def is_meeting_summary(sender: str, subject: str) -> bool:
    addr = sender_address(sender)
    if not addr:
        return False
    pattern = _MEETING_ADDRESSES.get(addr)
    if pattern is None:
        domain = addr.rsplit("@", 1)[1]
        pattern = next(
            (p for d, p in _MEETING_DOMAINS.items() if domain == d or domain.endswith("." + d)), None
        )
    return pattern is not None and bool(pattern.search(subject or ""))


def _meeting_title(subject: str) -> str:
    """The meeting's name: the quoted part ("Notes: “Budget review” Oct 10"), else the
    subject without its "Meeting summary for" lead-in."""
    if q := re.search(r"[“\"]([^”\"]+)[”\"]", subject):
        return q.group(1).strip()
    t = re.sub(
        r"^\s*(?:notes|meeting summary|summary|transcript|recap)\s*(?:for|of|:)\s*", "", subject, flags=re.I
    )
    return t.strip() or subject


def _put(ix: index.Store, m: dict[str, Any], body: str | None) -> None:
    mid = _mail_id(m)
    meta = {"mail_id": mid, "read": bool(m.get("read")), "date": m.get("date")}
    if body is None:
        meta["partial"] = True  # sender and subject only, until its text is read
    if msgid := str(m.get("message_id") or "").strip():
        meta["message_id"] = msgid  # Mail's message:// link (links.py)
    elif (old := ix.get(f"mail:{mid}")) is not None and old.meta.get("message_id"):
        meta["message_id"] = old.meta["message_id"]  # read again without it: kept
    ix.put(
        f"mail:{mid}",
        KIND,
        title=str(m.get("subject") or ""),
        body=essential(body)[:4000] if body else "",
        who=str(m.get("sender") or ""),
        ts=_ts(str(m.get("date") or "")),
        meta=meta,
    )
    sender, subject = str(m.get("sender") or ""), str(m.get("subject") or "")
    if body and is_meeting_summary(sender, subject):
        ix.put(
            f"mailmeeting:{mid}",
            "meeting",
            title=_meeting_title(subject),
            body=body[:MEETING_CHARS],
            who=sender,
            ts=_ts(str(m.get("date") or "")),
            meta={"source": "email", "note": f"mail:{mid}", "mail_id": mid},
        )


def remember(messages: list[dict[str, Any]], with_body: bool) -> int:
    """Lazily index mail Scout touched for a task: search results (sender and subject,
    their text filled in later) or messages read in full. Never fails the caller."""
    n = 0
    try:
        ix = index.default()
        for m in messages:
            if not isinstance(m.get("id"), int):
                continue
            if with_body:
                _put(ix, m, str(m.get("body") or ""))
            elif ix.get(f"mail:{_mail_id(m)}") is None:
                _put(ix, m, None)
            else:
                continue
            n += 1
    except Exception as exc:  # the memory service is down: the task still works
        log.info("mail not remembered in the index: %s", exc)
    return n


def remember_soon(messages: list[dict[str, Any]], with_body: bool) -> None:
    """remember(), off the request path."""
    import asyncio

    try:
        asyncio.get_running_loop().run_in_executor(None, remember, list(messages), with_body)
    except RuntimeError:
        remember(messages, with_body)


_MARKS = "CREATE TABLE IF NOT EXISTS mail_sync (name TEXT PRIMARY KEY, ts REAL NOT NULL);"


def _mark(ts: float | None = None) -> float | None:
    """The sync watermark: every listed message newer than this has been stored. Set
    with ts; read without."""
    db = appdb.default()
    db.script(_MARKS)
    if ts is not None:
        db.sql("INSERT OR REPLACE INTO mail_sync(name, ts) VALUES ('inbox', ?)", (ts,))
        return ts
    rows = db.sql("SELECT ts FROM mail_sync WHERE name = 'inbox'")
    return float(rows[0][0]) if rows else None


FAST_TIMEOUT_S = 30.0  # the newest-by-range listing; slower than this, the full scan is used
FAST_RETRY_S = 6 * 3600.0  # after the range listing failed, scan in full this long before retrying
# Off against the real Mail: on this Mac's 58k-message Google inbox the range listing ran
# past FAST_TIMEOUT_S on 2026-10-13 even with three properties, and the fallback scan then
# made each pass 59 s instead of 13 s. Tests still drive it with a fake `newest` runner.
USE_NEWEST_RANGES = False
_fast_off_until = 0.0


async def _list_newest(
    run: mail_mac.Runner, newest: mail_mac.Runner | None
) -> tuple[list[dict[str, Any]], str]:
    """The newest inbox messages as {id, date, read} (more from the full scan), and how
    they were listed. The newest NEWEST of each inbox by range when Mail is the real one
    (or `newest` fakes it); the full id-and-date scan (_LIST) when that fails or is off."""
    global _fast_off_until
    real = USE_NEWEST_RANGES and newest is None and run is _slow_jxa
    if (real or newest is not None) and time.monotonic() >= _fast_off_until:
        try:
            return await mail_mac.newest_ids(NEWEST, newest, timeout=FAST_TIMEOUT_S), "ranges"
        except ToolError as exc:
            _fast_off_until = time.monotonic() + FAST_RETRY_S
            log.warning("mail index: the newest-by-range listing failed (%s); scanning everything", exc)
    data = await mail_mac._call(run, mail_mac._LIST, str(NEWEST), "false", "", str(mail_mac.RECENT_DAYS))
    listed: list[dict[str, Any]] = data.get("messages", [])
    return listed, "scan"


async def sync_pass(
    ix: index.Store,
    run: mail_mac.Runner = _slow_jxa,
    first_days: float = 2.0,
    newest: mail_mac.Runner | None = None,
) -> dict[str, Any]:
    """Index mail that arrived since the last pass stored everything (the first time, the
    last `first_days`), then fill in the text of a few remembered search results.

    The watermark only moves past messages actually stored: not the newest indexed one,
    which may be a search result remembered ahead of older mail this pass never got to.
    `newest` fakes the AppleScript range listing in tests (see _list_newest)."""
    t0 = time.monotonic()
    mark = _mark()
    since = mark - 60 if mark is not None else time.time() - first_days * 86400
    listed, how = await _list_newest(run, newest)
    t_list = time.monotonic() - t0
    due = [
        m for m in listed
        if isinstance(m.get("id"), int) and _ts(str(m.get("date") or "")) > since and ix.get(f"mail:{m['id']}") is None
    ]  # fmt: skip
    new = due[:BODIES]
    partial = [h for h in ix.recent([KIND], 300) if h.meta.get("partial")][:CHUNK]
    todo = [
        *new,
        *(
            {"id": int(h.meta["mail_id"]), "date": h.meta.get("date"), "read": h.meta.get("read")}
            for h in partial
        ),
    ]
    stored = 0
    done: set[int] = set()
    t1 = time.monotonic()
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i : i + CHUNK]
        got = await mail_mac.bodies([int(m["id"]) for m in chunk], run, remember=False, stored=False)
        for row in chunk:
            m = got.get(int(row["id"]))
            if m is not None:
                _put(ix, {**row, **m}, str(m.get("body") or ""))
                if _mail_id({**row, **m}) != int(row["id"]):  # stored under Mail's own id
                    ix.delete(f"mail:{row['id']}")
                done.add(int(row["id"]))
                stored += 1
    t2 = time.monotonic()
    ids_added = await _add_message_ids(ix, run)
    t_ids = time.monotonic() - t2
    left = [_ts(str(m.get("date") or "")) for m in due if int(m["id"]) not in done]
    if left:  # next pass starts at the oldest message not stored yet
        _mark(min(left))
    elif listed:
        _mark(max([_ts(str(m.get("date") or "")) for m in listed] + ([mark] if mark is not None else [])))
    out = {
        "listed": len(listed), "how": how, "new": len(new), "filled": len(partial), "stored": stored,
        "list_s": round(t_list, 1), "read_s": round(t2 - t1, 1), "message_ids": ids_added,
        "ids_s": round(t_ids, 1), "total": ix.count(KIND),
    }  # fmt: skip
    log.info("mail index: %s", out)
    return out


async def _add_message_ids(ix: index.Store, run: mail_mac.Runner, n: int = MESSAGE_IDS) -> int:
    """Mail indexed before Scout kept the Message-ID gets it, the newest n a pass (one
    Apple event each, no text read). A message Mail no longer has keeps none and is
    asked for again only while it's among the newest without one. Never fails the pass."""
    try:
        todo = [h for h in ix.missing(KIND, "message_id", n) if h.meta.get("mail_id")]
        if not todo:
            return 0
        got = await mail_mac.message_ids([int(h.meta["mail_id"]) for h in todo], run)
    except Exception as exc:  # Mail can't answer this pass: the mail itself was indexed
        log.info("mail index: Message-IDs not read: %s", exc)
        return 0
    added = 0
    for h in todo:
        if msgid := got.get(int(h.meta["mail_id"])):
            ix.put(h.id, h.kind, h.title, h.body, h.who, h.ts, {**h.meta, "message_id": msgid})
            added += 1
        else:  # Mail hasn't got it (moved, deleted): "" stops it being asked for every pass
            ix.put(h.id, h.kind, h.title, h.body, h.who, h.ts, {**h.meta, "message_id": ""})
    return added


async def backfill_meetings(ix: index.Store, run: mail_mac.Runner = _slow_jxa, days: float = 14.0) -> int:
    """Once: meeting notes and transcripts that came by email in the last `days`, found
    by a Mail search per sender, stored in full (as mail and as meetings)."""
    db = appdb.default()
    db.script(_MARKS)
    if db.sql("SELECT ts FROM mail_sync WHERE name = 'meetings'"):
        return 0
    found: dict[int, dict[str, Any]] = {}
    for q in MEETING_QUERIES:
        data = await mail_mac._call(run, mail_mac._LIST, "50", "false", q, str(int(days)))
        for m in data.get("messages", []):
            if isinstance(m.get("id"), int) and is_meeting_summary(
                str(m.get("sender")), str(m.get("subject"))
            ):
                found[int(m["id"])] = m
    ids = [i for i in found if ix.get(f"mailmeeting:{i}") is None]
    stored: set[int] = set()
    for i in range(0, len(ids), CHUNK):
        got = await mail_mac.bodies(ids[i : i + CHUNK], run, remember=False, stored=False)
        for mid, m in got.items():
            if m.get("body"):
                _put(ix, {**found.get(mid, {}), **m}, str(m["body"]))
                stored.add(mid)
    if stored >= set(ids):  # done only when every one was read; else the next pass tries again
        db.sql("INSERT OR REPLACE INTO mail_sync(name, ts) VALUES ('meetings', ?)", (time.time(),))
    log.info("mail index: %d meeting notes from email brought in (%d found)", len(stored), len(found))
    return len(stored)


def search(query: str, count: int, days: int) -> dict[str, Any] | None:
    """Matching messages from the index, in mail_mac.listing's shape, or None when it
    has none (no mail indexed yet, or no match: the caller asks Mail.app instead). One
    call to the store."""
    t0 = time.monotonic()
    hits = index.default().search(
        query, kinds=[KIND], since=time.time() - days * 86400, limit=count, semantic=None
    )
    msgs = [
        {"id": int(h.meta.get("mail_id") or h.id.split(":", 1)[1]), "date": h.meta.get("date") or "",
         "sender": h.who, "subject": h.title, "read": bool(h.meta.get("read"))}
        for h in hits
    ]  # fmt: skip
    log.info("mail: searched the local index in %d ms (%d matches, %s)", (time.monotonic() - t0) * 1000,
             len(msgs), "by meaning" if any(h.how != "text" for h in hits) else "by words")  # fmt: skip
    if not msgs:
        return None
    return {"messages": msgs, "days": days, "source": "Scout's local index"}


async def loop(idle: Any, every_s: float = 1200.0) -> None:
    """Keep the index current: a pass every `every_s` while Scout is idle."""
    import asyncio

    await asyncio.sleep(90)  # let startup finish first
    while True:
        try:
            if idle():
                await sync_pass(index.default())
                await backfill_meetings(index.default(), days=load().tasks.backfill_days)
        except Exception as exc:
            log.warning("mail index pass failed: %s", exc)
        await asyncio.sleep(every_s)
