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
from .summary import essential

log = logging.getLogger(__name__)

KIND = "mail"
NEWEST = 100  # messages listed per pass
BODIES = 50  # message texts read per pass
CHUNK = 10  # per call to Mail (25 at a time ran past 30 s live)
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


# Meeting notes and transcripts that arrive by email (docs/tasks-design.md): the sender's
# domain, and a subject that says so. They're stored as meetings too, in full.
_MEETING_SENDERS = re.compile(
    r"gemini-notes@google\.com|meetings-noreply@google\.com|meet-recordings-noreply@google\.com|"
    r"@zoom\.us|@otter\.ai|@fireflies\.ai|@fathom\.video|@tldv\.io|@email\.teams\.microsoft\.com|"
    r"@read\.ai|@grain\.com",
    re.I,
)
_MEETING_SUBJECT = re.compile(r"notes|summary|transcript|recap|action items|meeting assets", re.I)
MEETING_QUERIES = ("gemini-notes@google.com", "meetings-noreply@google.com", "zoom.us", "otter.ai",
                   "fireflies.ai", "fathom.video", "tldv.io", "teams.microsoft.com")  # fmt: skip
MEETING_CHARS = 60_000


def is_meeting_summary(sender: str, subject: str) -> bool:
    return bool(_MEETING_SENDERS.search(sender or "")) and bool(_MEETING_SUBJECT.search(subject or ""))


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


async def sync_pass(
    ix: index.Store, run: mail_mac.Runner = _slow_jxa, first_days: float = 2.0
) -> dict[str, Any]:
    """Index mail that arrived since the last pass stored everything (the first time, the
    last `first_days`), then fill in the text of a few remembered search results.

    The watermark only moves past messages actually stored: not the newest indexed one,
    which may be a search result remembered ahead of older mail this pass never got to."""
    t0 = time.monotonic()
    mark = _mark()
    since = mark - 60 if mark is not None else time.time() - first_days * 86400
    data = await mail_mac._call(run, mail_mac._LIST, str(NEWEST), "false", "", str(mail_mac.RECENT_DAYS))
    listed = data.get("messages", [])
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
        got = await mail_mac.bodies([int(m["id"]) for m in chunk], run, remember=False)
        for row in chunk:
            m = got.get(int(row["id"]))
            if m is not None:
                _put(ix, {**row, **m}, str(m.get("body") or ""))
                if _mail_id({**row, **m}) != int(row["id"]):  # stored under Mail's own id
                    ix.delete(f"mail:{row['id']}")
                done.add(int(row["id"]))
                stored += 1
    left = [_ts(str(m.get("date") or "")) for m in due if int(m["id"]) not in done]
    if left:  # next pass starts at the oldest message not stored yet
        _mark(min(left))
    elif listed:
        _mark(max([_ts(str(m.get("date") or "")) for m in listed] + ([mark] if mark is not None else [])))
    out = {
        "listed": len(listed), "new": len(new), "filled": len(partial), "stored": stored,
        "list_s": round(t_list, 1), "read_s": round(time.monotonic() - t1, 1), "total": ix.count(KIND),
    }  # fmt: skip
    log.info("mail index: %s", out)
    return out


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
    for i in range(0, len(ids), CHUNK):
        got = await mail_mac.bodies(ids[i : i + CHUNK], run, remember=False)
        for mid, m in got.items():
            _put(ix, {**found.get(mid, {}), **m}, str(m.get("body") or ""))
    db.sql("INSERT OR REPLACE INTO mail_sync(name, ts) VALUES ('meetings', ?)", (time.time(),))
    log.info("mail index: %d meeting notes from email brought in (%d found)", len(ids), len(found))
    return len(ids)


def search(query: str, count: int, days: int) -> dict[str, Any] | None:
    """Matching messages from the index, in mail_mac.listing's shape, or None when the
    index has no mail yet (the caller asks Mail.app instead)."""
    ix = index.default()
    if not ix.count(KIND):
        return None
    t0 = time.monotonic()
    hits = ix.search(query, kinds=[KIND], since=time.time() - days * 86400, limit=count, semantic=None)
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
