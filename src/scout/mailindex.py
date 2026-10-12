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
import time
from typing import Any

from . import index, mail_mac
from .summary import essential

log = logging.getLogger(__name__)

KIND = "mail"
NEWEST = 100  # messages listed per pass
BODIES = 50  # message texts read per pass (in chunks of 25)


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return time.time()


async def sync_pass(
    ix: index.Index, run: mail_mac.Runner = mail_mac._jxa, keep_days: int = 90
) -> dict[str, Any]:
    """Store new inbox messages; returns what was done, with timings."""
    t0 = time.monotonic()
    data = await mail_mac._call(run, mail_mac._LIST, str(NEWEST), "false", "", str(mail_mac.RECENT_DAYS))
    listed = data.get("messages", [])
    t_list = time.monotonic() - t0
    cut = time.time() - keep_days * 86400
    new = [
        m for m in listed
        if isinstance(m.get("id"), int) and _ts(str(m.get("date") or "")) >= cut and ix.get(f"mail:{m['id']}") is None
    ][:BODIES]  # fmt: skip
    stored = 0
    t1 = time.monotonic()
    for i in range(0, len(new), 25):
        chunk = new[i : i + 25]
        got = await mail_mac.bodies([int(m["id"]) for m in chunk], run)
        for row in chunk:
            m = got.get(int(row["id"]))
            if m is None:
                continue
            ix.put(
                f"mail:{row['id']}",
                KIND,
                title=str(m.get("subject") or row.get("subject") or ""),
                body=essential(str(m.get("body") or ""))[:4000],
                who=str(m.get("sender") or row.get("sender") or ""),
                ts=_ts(str(m.get("date") or row.get("date") or "")),
                meta={"mail_id": int(row["id"]), "read": bool(row.get("read")), "date": row.get("date")},
            )
            stored += 1
    aged = ix.sql("SELECT id FROM docs WHERE kind = ? AND ts < ?", (KIND, cut))
    for (old,) in aged:
        ix.delete(old)
    out = {
        "listed": len(listed), "stored": stored, "aged_out": len(aged),
        "list_s": round(t_list, 1), "read_s": round(time.monotonic() - t1, 1), "total": ix.count(KIND),
    }  # fmt: skip
    log.info("mail index: %s", out)
    return out


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


async def loop(idle: Any, every_s: float = 1200.0, keep_days: int = 90) -> None:
    """Keep the index current: a pass every `every_s` while Scout is idle."""
    import asyncio

    await asyncio.sleep(90)  # let startup finish first
    while True:
        try:
            if idle():
                await sync_pass(index.default(), keep_days=keep_days)
        except Exception as exc:
            log.warning("mail index pass failed: %s", exc)
        await asyncio.sleep(every_s)
