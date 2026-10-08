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
import re
import logging
from pathlib import Path
from typing import Any

from . import calendar_mac, mail_mac
from .mac import ToolError
from .memory import Memory
from . import summary
from .summary import Model, Summaries

log = logging.getLogger(__name__)

MODES = ("strict", "balanced", "open")
GIST_LIMIT = 10  # messages in one listing that get a summary; the rest show subjects only
PREFETCH_BUDGET_S = 2.5  # how long the request context waits for summaries
MEMORIES = {
    "strict": 2,
    "balanced": 5,
    "open": 10,
}  # relevant memories given to Claude (strict: need-to-know)

# Strict mode shows a short local topic label instead of each subject: enough to tell
# messages apart ("the UPS one"), not the subject line itself.
LABEL_SYSTEM = (
    "Give a topic label for an email subject: one to three plain words such as 'delivery notice', "
    "'bill', 'personal note', 'work meeting', 'newsletter', 'appointment', 'receipt'. No names, "
    "numbers, places or other details from the subject. Reply with the label only."
)
_LABEL = re.compile(r"[^a-z ]+")

STRICT_MAIL = (
    "[Strict privacy mode: Claude sees senders and topic labels only. Email text and subjects stay on this Mac; "
    "the user can ask Scout directly, e.g. 'what did the email from Sam say', for a summary "
    "read out by the local model.]"
)
_NO_MODEL = (
    "[No summaries: Scout's local model isn't loaded. Call mail_read_full for a message's text "
    "if the task needs it.]"
)


STRICT_MESSAGES = (
    "[Strict privacy mode: the text of messages isn't shared with Claude. Tell the user what you "
    "can see (who and when); they can ask Scout to read or summarize their texts aloud.]"
)
_NO_MODEL_MESSAGES = "[No summaries: Scout's local model isn't loaded, so the texts are withheld.]"


class Policy:
    def __init__(self, mode: str = "balanced", model: Model | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"privacy.mode must be strict, balanced or open, not {mode!r}")
        self.mode = mode
        self.summaries = Summaries(model)
        self._bg: set[asyncio.Task[Any]] = set()  # summaries still running after a deadline
        self._labels: dict[tuple[Any, ...], str] = {}

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
            return await self._strict_list(data, what)
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
        if self.mode == "strict":
            shown = {**m, "subject": f"[{await self.label(m)}]"}
            return f"{mail_mac._UNTRUSTED}\n{mail_mac.header(shown)}\n\n" + STRICT_MAIL
        head = f"{mail_mac._UNTRUSTED}\n{mail_mac.header(m)}\n\n"
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

    # --- messages (iMessage, SMS) -------------------------------------------------

    async def messages_view(self, data: dict[str, Any], what: str, budget_s: float = 3.0) -> str:
        """Claude's view of a messages lookup: who and when always; the texts only in
        open mode; in balanced mode one local, attributed summary per person (texts
        that look like scams get the fixed warning, as email does)."""
        from . import messages_mac

        msgs = data.get("messages") or []
        if self.mode == "open" or not msgs:
            return messages_mac.format_messages(data, what)
        bare = {
            **data,
            "messages": [{**m, "text": "", "attachment": False, "truncated": False} for m in msgs],
        }
        listing = messages_mac.format_messages(bare, what).replace("(no text)", "(text withheld)")
        if self.mode == "strict":
            return STRICT_MESSAGES + "\n" + listing
        if not self.summaries.available:
            return _NO_MODEL_MESSAGES + "\n" + listing
        people: dict[str, list[dict[str, Any]]] = {}
        if data.get("conversation"):  # one thread: summarize both sides together
            people[f"the conversation with {data['conversation']}"] = [m for m in msgs if m.get("text")]
        for m in msgs:
            if not data.get("conversation") and not m.get("from_me") and m.get("text"):
                people.setdefault(messages_mac._person(m), []).append(m)
        lines = []
        for who, theirs in list(people.items())[:5]:
            ids = ",".join(str(m.get("id")) for m in theirs)
            pseudo = {
                "id": f"sms:{ids}",
                "sender": who,
                "subject": "text messages",
                "date": theirs[0].get("date", ""),
                "body": "\n".join(
                    f"[{m.get('date', '')}] {'me' if m.get('from_me') else messages_mac._person(m)}: {m.get('text', '')}"
                    for m in reversed(theirs)
                ),
            }
            try:
                gist = await asyncio.wait_for(self.summaries.of(pseudo), budget_s)
            except (RuntimeError, TimeoutError) as exc:
                log.info("no summary of texts from %s: %s", who, exc)
                gist = f"{len(theirs)} message(s); no summary in time"
            lines.append(f"  {who} ({len(theirs)} message{'s' * (len(theirs) != 1)}): {gist}")
        if not lines:
            return listing
        return (
            listing + "\nSummaries by Scout's local model (the texts stay on this Mac):\n" + "\n".join(lines)
        )

    async def label(self, m: dict[str, Any]) -> str:
        """A one-to-three-word local topic label for an email (strict mode)."""
        if summary.suspicious(m):
            return "possible scam"
        model = self.summaries.model
        if model is None or not getattr(model, "ready", False):
            return "subject withheld"
        key = ("label", m.get("id"), m.get("subject"))
        if key not in self._labels:
            try:
                out = await asyncio.wait_for(
                    model.complete(LABEL_SYSTEM, str(m.get("subject") or ""), 8), 3.0
                )
            except (RuntimeError, TimeoutError):
                return "subject withheld"
            words = _LABEL.sub(" ", out.lower()).split()[:3]
            self._labels[key] = " ".join(words) or "subject withheld"
        return self._labels[key]

    async def _strict_list(self, data: dict[str, Any], what: str) -> str:
        msgs = [{**m, "subject": f"[{await self.label(m)}]"} for m in data.get("messages", [])]
        return STRICT_MAIL + "\n" + mail_mac.format_list({**data, "messages": msgs}, what)

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
        if self.mode == "strict" and not about.strip():
            raise ToolError(
                "Strict privacy mode: only memories about something specific are shared; say what about."
            )
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
