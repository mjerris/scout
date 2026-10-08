"""What of the user's private data Claude gets to see: one policy, enforced here in
code (not by asking Claude nicely), for the request context, Claude's mail and
calendar tools (room and other sessions alike) and Scout's memory.

privacy.mode in config.toml:
- "strict": email text, summaries of it, calendar notes and memories never go to
  Claude. It sees senders, subjects and event titles and times, and is told
  plainly when something can't be done without more.
- "balanced" (default): Claude gets a summary of each email written by the local
  model on this Mac (sender, subject, one-line gist; a few sentences for one
  message), never the email itself unless it calls mail_read_full for a task that
  needs the exact words. Calendar: titles, times and places, no notes or attendees.
  Memories: only those relevant to the request.
- "open": everything as the tools return it (Scout's behaviour before privacy modes).

Local tiers (plain code and the local model) see everything: nothing leaves the Mac."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import calendar_mac, mail_mac
from .mac import ToolError
from .memory import Memory
from .summary import Model, Summaries

log = logging.getLogger(__name__)

MODES = ("strict", "balanced", "open")
GIST_LIMIT = 10  # messages in one listing that get a summary; the rest show subjects only
PREFETCH_BUDGET_S = 2.5  # how long the request context waits for summaries
MEMORIES = {"strict": 0, "balanced": 5, "open": 10}  # relevant memories given to Claude

STRICT_MAIL = (
    "[Strict privacy mode: Claude sees senders and subjects only. Email text stays on this Mac; "
    "the user can ask Scout directly, e.g. 'what did the email from Sam say', for a summary "
    "read out by the local model.]"
)
_NO_MODEL = (
    "[No summaries: Scout's local model isn't loaded. Call mail_read_full for a message's text "
    "if the task needs it.]"
)


class Policy:
    def __init__(self, mode: str = "balanced", model: Model | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"privacy.mode must be strict, balanced or open, not {mode!r}")
        self.mode = mode
        self.summaries = Summaries(model)
        self._bg: set[asyncio.Task[Any]] = set()  # summaries still running after a deadline

    @property
    def shares_private_text(self) -> bool:
        """May Claude see what Scout read or worked out locally from private data
        (a spoken email summary, a memory)?"""
        return self.mode != "strict"

    # --- mail ----------------------------------------------------------------------

    async def mail_list(
        self,
        count: Any = 10,
        unread_only: Any = False,
        query: Any = None,
        run: mail_mac.Runner = mail_mac._jxa,
        budget_s: float | None = None,
    ) -> str:
        """Claude's view of mail_recent / mail_search (query set). `budget_s`: give up
        waiting for summaries after this long (they finish in the background)."""
        q = mail_mac.search_query(query) if query is not None else ""
        data = await mail_mac.listing(count, unread_only, q or None, run)
        what = (
            f"recent inbox messages matching {q!r}"
            if q
            else "unread messages in the inbox"
            if unread_only
            else "messages in the inbox"
        )
        if self.mode == "open" or not data.get("messages"):
            return mail_mac.format_list(data, what)
        if self.mode == "strict":
            return STRICT_MAIL + "\n" + mail_mac.format_list(data, what)
        if not self.summaries.available:
            return _NO_MODEL + "\n" + mail_mac.format_list(data, what)
        gists = await self._gists(data["messages"][:GIST_LIMIT], run, budget_s)
        notes = {i: f"gist (Scout's local summary): {g}" for i, g in gists.items()}
        missing = len(data["messages"]) - len(gists)
        tail = (
            f"\n[{missing} without a summary yet: ask for fewer, or mail_read one by id.]" if missing else ""
        )
        return mail_mac.format_list(data, what, notes) + tail

    async def _gists(
        self, msgs: list[dict[str, Any]], run: mail_mac.Runner, budget_s: float | None
    ) -> dict[int, str]:
        out = {m["id"]: g for m in msgs if (g := self.summaries.cached(m)) is not None}
        todo = [m for m in msgs if m["id"] not in out]
        if not todo:
            return out
        try:
            texts = await mail_mac.bodies([m["id"] for m in todo], run)
        except ToolError as exc:
            log.info("no message text for summaries: %s", exc)
            return out
        tasks = {
            m["id"]: asyncio.ensure_future(self.summaries.of({**m, **texts[m["id"]]}))
            for m in todo
            if m["id"] in texts
        }
        if not tasks:
            return out
        done, pending = await asyncio.wait(tasks.values(), timeout=budget_s)
        for t in pending:  # keeps filling the cache for the next request
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            t.add_done_callback(_quiet)
        for mid, t in tasks.items():
            if t in done and not t.cancelled() and t.exception() is None:
                out[mid] = t.result()
            elif t in done:
                log.info("no summary of message %s: %s", mid, t.exception())
        return out

    async def mail_read(self, message_id: Any, run: mail_mac.Runner = mail_mac._jxa) -> str:
        """Claude's view of one message."""
        if self.mode == "open":
            return await mail_mac.read(message_id, run)
        m = await mail_mac.message(message_id, run)
        head = f"{mail_mac._UNTRUSTED}\n{mail_mac.header(m)}\n\n"
        if self.mode == "strict":
            return head + STRICT_MAIL
        if not self.summaries.available:
            return head + _NO_MODEL
        try:
            text = await self.summaries.of(m, "summary")
        except (RuntimeError, TimeoutError) as exc:
            log.info("no summary of message %s: %s", message_id, exc)
            return head + _NO_MODEL.replace("isn't loaded", "couldn't summarize this one")
        return (
            head
            + f"Summary by Scout's local model (the text stays on this Mac): {text}\n"
            + "[Call mail_read_full only if the task needs the exact wording, e.g. quoting it in a reply.]"
        )

    async def mail_read_full(self, message_id: Any, run: mail_mac.Runner = mail_mac._jxa) -> str:
        if self.mode == "strict":
            raise ToolError(
                "Strict privacy mode: the text of emails isn't shared with Claude. Tell the user this "
                "needs the email's text; they can ask Scout to summarize it aloud, or change privacy.mode."
            )
        return await mail_mac.read(message_id, run)

    # --- calendar ------------------------------------------------------------------

    async def calendar_events(
        self,
        start: Any = None,
        end: Any = None,
        query: Any = None,
        calendar: Any = None,
        helper: Path | None = None,
    ) -> str:
        """Claude's view of calendar_events: titles and times always; notes and
        attendees only in open mode."""
        data = await calendar_mac.event_data(start, end, query, calendar, helper)
        if self.mode == "open":
            return calendar_mac.format_events(data)
        events = data.get("events", [])
        dropped = any(e.get("notes") or e.get("attendees") for e in events)
        data = {**data, "events": [{**e, "notes": "", "attendees": 0} for e in events]}
        out = calendar_mac.format_events(data)
        return out + (
            f"\n[Event notes and attendees are left out ({self.mode} privacy mode).]" if dropped else ""
        )

    # --- memory --------------------------------------------------------------------

    def memories(self, memory: Memory | None, request: str) -> list[str]:
        """Remembered facts Claude may see with this request."""
        n = MEMORIES[self.mode]
        return memory.relevant(request, n) if memory is not None and n else []

    def recall(self, memory: Memory, about: str) -> str:
        if self.mode == "strict":
            raise ToolError("Strict privacy mode: Scout's memories aren't shared with Claude.")
        facts = memory.relevant(about, MEMORIES[self.mode]) if about.strip() else memory.newest(10)
        if not facts:
            return f"Nothing remembered about {about}." if about.strip() else "Nothing remembered yet."
        return "The user told Scout:\n" + "\n".join(f"- {f}" for f in facts)


def _quiet(t: asyncio.Task[Any]) -> None:
    if not t.cancelled() and t.exception() is not None:
        log.info("background summary failed: %s", t.exception())


_current = Policy()


def configure(mode: str, model: Model | None = None) -> Policy:
    """Set the policy everything uses (the app does this at startup)."""
    global _current
    _current = Policy(mode, model)
    return _current


def current() -> Policy:
    return _current
