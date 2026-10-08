"""Tier 1: a small local model (MLX) that answers read-only lookups itself, the
calendar and the inbox (including what an email says, summarized on this Mac),
and hands everything else to Claude. The same model writes the email summaries
Claude sees instead of the email (summary.py, privacy.py).

The model only chooses between "this read-only lookup answers it" and "Claude".
Safety does not rest on its judgment: plain-code guards send to Claude anything
with an action verb (send, reply, add, delete, order, run...), any judgment or
multi-step request, and anything the model's answer doesn't parse into a known
read-only tool. Measured on 41 labelled requests (scripts/bench_tier1.py).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .mac import ToolError
from .summary import Summaries, sender_name, suspicious

SYSTEM = """You are the fast first stage of a voice assistant called Scout. Choose ONE route for the request and answer with one line of JSON, nothing else.

- {"route": "tool", "tool": NAME, "args": {...}} when ONE read-only lookup fully answers it:
    calendar_events: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "query": "event name"}  (what's on, am I free, when is X)
      Dates as YYYY-MM-DD; add "THH:MM" only when a time of day matters ("tomorrow afternoon").
      "query" is only an event's name ("dentist", "standup"); never a date or a word like "meetings".
    mail_recent: {"count": 1-20, "unread_only": true/false}  (is there new or unread email, the latest email)
    mail_search: {"query": sender name or subject words}  (did someone email me, is there email about something)
    mail_summarize: only when asked what an email SAYS or for a SUMMARY of email:
      {"query": sender name or subject words}  ("what did the Rover email say")
      {"unread_only": true}  ("summarize my unread mail")
- {"route": "claude"} for everything else: knowledge, advice, opinions, the weather or news, anything that sends, replies, writes, adds, changes, deletes, runs, opens or buys something, anything about code or files, anything with more than one step, and anything you are not sure about.

Work out dates from the current date given below. When unsure, choose claude."""

log = logging.getLogger(__name__)

TOOLS = {"calendar_events", "mail_recent", "mail_search", "mail_summarize"}

# Requests that do something, not just look something up: always Claude.
_ACTION = re.compile(
    r"\b(send|sent|reply|respond|answer|forward|email (?:him|her|them|my|the)|write|draft|tell|text|message|call|"
    r"add|put|schedule|book|create|make|move|reschedule|change|update|edit|rename|cancel|delete|remove|"
    r"archive|mark|accept|decline|invite|order|buy|purchase|pay|remind|reminder|run|open|close|install|"
    r"fix|build|deploy|push|commit|merge|start|stop|turn|set|play|share|upload|download|save|print)\b"
)
# Judgment, conditions, or several steps: Claude.
_JUDGMENT = re.compile(
    r"\b(should|whether|if so|and if|then|why|recommend|advice|think|worth|better)\b|,\s*and\b"
)


# Only requests that sound like the calendar or the inbox are worth a model call;
# general questions go straight to Claude (faster, and the model can't misfile them).
_LOOKUP = re.compile(
    r"\b(calendar|schedule|agenda|meetings?|appointments?|events?|standup|busy|free|plans?|"
    r"when(?:'s| is| are| do)|what(?:'s| is) on|do i have|e-?mails?|inbox|mail|messages?|unread|from \w+)\b"
)


def guard(request: str) -> str | None:
    """Why this request must go to Claude no matter what the model says, or None."""
    low = request.lower()
    if not _LOOKUP.search(low):
        return "not a calendar or mail lookup"
    if m := _ACTION.search(low):
        return f"action word {m.group(1)!r}"
    if m := _JUDGMENT.search(low):
        return f"needs judgment ({m.group(0).strip()!r})"
    if len(low.split()) < 2:
        return "too short to tell"
    return None


def parse(output: str, today: str) -> dict[str, Any] | None:
    """The model's decision as {"tool": name, "args": {...}}, or None (= Claude).
    Anything but a well-formed read-only tool call counts as Claude."""
    m = re.search(r"\{.*\}", output, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or d.get("route") != "tool" or d.get("tool") not in TOOLS:
        return None
    raw_args = d.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    if d["tool"] == "calendar_events":
        return _calendar_args(args, today)
    if d["tool"] == "mail_search" and not str(args.get("query", "")).strip():
        return None  # a search with nothing to search for: the model didn't really know
    if d["tool"] == "mail_summarize":
        return _summarize_args(args)
    return {"tool": d["tool"], "args": args}


def _summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    """One message by sender or subject, or the newest few (unread unless said otherwise)."""
    if query := str(args.get("query") or "").strip()[:100]:
        return {"tool": "mail_summarize", "args": {"query": query}}
    return {"tool": "mail_summarize", "args": {"unread_only": args.get("unread_only") is not False}}


# Words the model sometimes puts in the event-name search that are really dates or
# filler; searching titles for them finds nothing.
_NOT_A_NAME = re.compile(
    r"^(?:(?:this|next|the|on|my|any|all)\s+)*(?:today|tomorrow|tonight|morning|afternoon|evening|night|"
    r"week|weekend|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday|meetings?|events?|"
    r"appointments?|anything|plans?|calendar|schedule|free|busy)(?:\s+\w+)?$"
)
_LOOSE_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def _date(value: Any) -> str | None:
    """A model-written date or datetime, zero-padded so the calendar accepts it."""
    m = _LOOSE_DATE.match(str(value or "").strip())
    if not m:
        return None
    y, mo, d, h, mi, sec = m.groups()
    out = f"{y}-{int(mo):02d}-{int(d):02d}"
    return out + (f"T{int(h):02d}:{mi}:{sec or '00'}" if h else "")


def _calendar_args(args: dict[str, Any], today: str) -> dict[str, Any] | None:
    query = str(args.get("query") or "").strip()
    if _NOT_A_NAME.match(query.lower()):
        query = ""
    start, end = _date(args.get("start")), _date(args.get("end"))
    if not start:
        if not query:
            return None  # neither a date nor an event to look for
        start = today  # "when is the dentist": search ahead from today
        if not end:
            end = (dt.date.fromisoformat(today) + dt.timedelta(days=60)).isoformat()
    out: dict[str, Any] = {"start": start}
    if end:
        if "T" not in end and end <= start[:10]:  # an end date means "through that day"
            end = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
        out["end"] = end
    if query:
        out["query"] = query
    return {"tool": "calendar_events", "args": out}


def messages(request: str, context: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM + "\n\n" + context}, {"role": "user", "content": request}]


# --- running it --------------------------------------------------------------------------


class LocalModel:
    """The tier 1 model, loaded once and run on its own thread (MLX, Apple GPU)."""

    def __init__(self, name: str, timeout_s: float = 3.0) -> None:
        self.name, self.timeout_s = name, timeout_s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tier1")
        self._model: Any = None
        self._tok: Any = None
        self.ready = False

    def _load(self) -> None:
        from huggingface_hub import snapshot_download
        from mlx_lm import load

        path = snapshot_download(self.name, local_files_only=True)  # fetched by scripts/fetch-models.sh
        loaded = load(path)  # (model, tokenizer[, config])
        self._model, self._tok = loaded[0], loaded[1]
        self._generate(messages("warm up", "Now: today."), 80)
        self.ready = True

    async def load(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._load)

    def _generate(self, chat: list[dict[str, str]], max_tokens: int) -> str:
        from mlx_lm import generate

        kwargs: dict[str, Any] = {"add_generation_prompt": True, "tokenize": False}
        try:
            prompt = self._tok.apply_chat_template(chat, enable_thinking=False, **kwargs)
        except TypeError:
            prompt = self._tok.apply_chat_template(chat, **kwargs)
        out: str = generate(self._model, self._tok, prompt=prompt, max_tokens=max_tokens, verbose=False)
        return out

    async def complete(self, system: str, user: str, max_tokens: int = 120, timeout_s: float = 20.0) -> str:
        """The model's answer to one system + user prompt: text only, no tools. Used for
        work on private data that stays on this Mac (summarizing an email)."""
        if not self.ready:
            raise RuntimeError("the local model isn't loaded")
        chat = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(self._pool, self._generate, chat, max_tokens), timeout_s
        )

    async def decide(self, request: str, now: dt.datetime) -> dict[str, Any] | None:
        """A read-only lookup that answers `request`, or None for Claude."""
        if not self.ready or guard(request):
            return None
        context = f"Now: {now.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')} (today is {now.date().isoformat()})."
        loop = asyncio.get_running_loop()
        try:
            out = await asyncio.wait_for(
                loop.run_in_executor(self._pool, self._generate, messages(request, context), 80),
                self.timeout_s,
            )
        except TimeoutError:
            log.info("tier 1 took over %.0f s; asking Claude", self.timeout_s)
            return None
        return parse(out, now.date().isoformat())


def _when(start: str, all_day: bool, now: dt.datetime) -> str:
    t = dt.datetime.fromisoformat(start)
    day = t.date() if all_day else t.astimezone(now.tzinfo).date()
    delta = (day - now.date()).days
    name = (
        "today"
        if delta == 0
        else "tomorrow"
        if delta == 1
        else day.strftime("%A")
        if 0 < delta < 7
        else day.strftime("%A, %B %-d")
    )
    if all_day:
        return f"{name}, all day"
    clock = t.astimezone(now.tzinfo).strftime("%-I:%M %p").replace(":00 ", " ")
    return f"{name} at {clock}"


def speak_calendar(data: dict[str, Any], args: dict[str, Any], now: dt.datetime) -> str:
    events = data.get("events", [])
    if q := args.get("query"):
        # "when's my next standup": skip ones that have already started today
        events = [e for e in events if e.get("all_day") or dt.datetime.fromisoformat(e["start"]) >= now]
        if not events:
            return f"I don't see {q} on your calendar in that time."
        e = events[0]
        return f"Your next {e.get('title') or q} is {_when(e['start'], bool(e.get('all_day')), now)}."
    if not events:
        return "Nothing on your calendar then."
    items = [
        f"{e.get('title') or 'an untitled event'} {_when(e['start'], bool(e.get('all_day')), now)}"
        for e in events[:6]
    ]
    more = f" And {len(events) - 6} more." if len(events) > 6 else ""
    if len(items) == 1:
        return f"You have {items[0]}.{more}"
    return f"You have {', '.join(items[:-1])}, and {items[-1]}.{more}"


def _sender(raw: str) -> str:
    """'"Chris @ StubHub" <events@...>' -> 'Chris at StubHub'; a bare address -> its name part."""
    name = raw.split("<", 1)[0].strip().strip('"').strip() or raw.strip()
    if re.fullmatch(r"[^@\s]+@[^@\s]+", name):
        return name.split("@", 1)[0]
    return name.replace(" @ ", " at ")


def _subject(raw: str) -> str:
    words = (raw or "no subject").split()
    return " ".join(words[:9]) + ("..." if len(words) > 9 else "")


def speak_mail(msgs: list[dict[str, Any]], args: dict[str, Any], tool: str) -> str:
    if tool == "mail_search":
        q = args.get("query", "")
        if not msgs:
            return f"I don't see any email from or about {q} lately."
        m = msgs[0]
        return f"The latest from {_sender(m.get('sender', ''))} is about {_subject(m.get('subject', ''))}."
    if not msgs:
        return "No new email." if args.get("unread_only") else "Your inbox is empty."
    top = "; ".join(
        f"from {_sender(m.get('sender', ''))} about {_subject(m.get('subject', ''))}" for m in msgs[:3]
    )
    kind = "unread email" if args.get("unread_only") else "recent email"
    count = f"{len(msgs)} {kind}{'s' * (len(msgs) != 1)}"
    return f"You have {count}. The newest: {top}."


def speak_summaries(
    msgs: list[dict[str, Any]],
    texts: dict[int, str],
    flagged: set[int],
    args: dict[str, Any],
    now: dt.datetime,
) -> str:
    """What the email(s) say, from their local summaries (each led by its sender)."""
    if q := args.get("query"):
        if not msgs:
            return f"I don't see any email from or about {q} lately."
        m = msgs[0]
        text = (
            texts.get(m["id"]) or f"I couldn't summarize the email from {sender_name(m.get('sender', ''))}."
        )
        when = _when(m["date"], False, now) if m.get("date") else "recently"
        lead = "a" + text[1:] if text.startswith("An email") else text
        return f"{when[:1].upper() + when[1:]}, {lead}"
    if not msgs:
        return "No new email." if args.get("unread_only") else "Your inbox is empty."
    kind = "unread email" if args.get("unread_only") else "recent email"
    head = (
        f"Your {len(msgs)} newest {kind}s."
        if len(msgs) >= SUMMARY_COUNT
        else f"You have {len(msgs)} {kind}{'s' * (len(msgs) != 1)}."
    )
    parts = [
        texts.get(m["id"])
        or f"{sender_name(m.get('sender', ''))} wrote about {_subject(m.get('subject', ''))}."
        for m in msgs
        if m["id"] not in flagged
    ]
    odd = [sender_name(m.get("sender", "")) for m in msgs if m["id"] in flagged]
    if len(odd) == 1:
        parts.append(f"And one suspicious email, from {odd[0]}; I didn't act on it.")
    elif odd:
        parts.append(
            f"And {len(odd)} suspicious emails, from {', '.join(odd[:-1])} and {odd[-1]}; I didn't act on them."
        )
    return " ".join([head, *parts])


SUMMARY_COUNT = 5  # messages summarized for "summarize my unread mail"


async def summarize(args: dict[str, Any], now: dt.datetime, summaries: Summaries) -> str:
    """Read the message(s) and summarize them here; nothing goes to Claude."""
    from . import mail_mac

    query = args.get("query")
    msgs = await mail_mac.message_data(1 if query else SUMMARY_COUNT, bool(args.get("unread_only")), query)
    full = await mail_mac.bodies([m["id"] for m in msgs])
    kind = "summary" if query else "gist"
    texts: dict[int, str] = {}
    flagged: set[int] = set()
    for m in msgs:
        if m["id"] not in full:
            continue
        msg = {**m, **full[m["id"]]}
        if suspicious(msg):
            flagged.add(m["id"])
        try:
            texts[m["id"]] = await summaries.of(msg, kind)
        except (RuntimeError, TimeoutError) as exc:
            log.info("no summary of message %s: %s", m["id"], exc)
    return speak_summaries(msgs, texts, flagged, args, now)


async def run(decision: dict[str, Any], now: dt.datetime, summaries: Summaries | None = None) -> str:
    """Do the read-only lookup and say the answer."""
    from . import calendar_mac, mail_mac

    tool, args = decision["tool"], decision["args"]
    if tool == "calendar_events":
        data = await calendar_mac.event_data(args.get("start"), args.get("end"), args.get("query"))
        return speak_calendar(data, args, now)
    if tool == "mail_summarize":
        if summaries is None or not summaries.available:
            raise ToolError("the local model isn't ready to summarize email yet")
        return await summarize(args, now, summaries)
    msgs = await mail_mac.message_data(
        args.get("count") or 5,
        bool(args.get("unread_only")),
        args.get("query") if tool == "mail_search" else None,
    )
    return speak_mail(msgs, args, tool)
