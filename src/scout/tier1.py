"""Tier 1: a small local model (MLX) that answers read-only lookups itself: the
calendar and the inbox (including what an email says, summarized on this Mac), the
weather, the task list, and (`recall`) what's in Scout's memory ("what did Pat say
about the budget", recall.py); it hands everything else to Claude. The same model
writes the email summaries Claude sees instead of the email (summary.py, privacy.py).

The model only chooses between "this read-only lookup answers it" and "Claude".
Safety does not rest on its judgment: plain-code guards send to Claude anything
with an action verb (send, reply, add, delete, order, run...), any judgment or
multi-step request, and anything the model's answer doesn't parse into a known
read-only tool. A verb asked about in the past ("when did I last change the oil",
"what did Pat tell me") is a question about the record, not an action, so it passes.
A recall answer is checked in plain code against the record it came from, and goes
to Claude when it isn't grounded (recall.py). Measured on labelled requests:
scripts/bench_tier1.py (routing) and scripts/bench_recall.py (recall, on a synthetic
store).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
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
<EXTRA>- {"route": "claude"} for everything else: knowledge, advice, opinions, <NEWS>, anything that sends, replies, writes, adds, changes, deletes, runs, opens or buys something, anything about code or files, anything with more than one step, and anything you are not sure about.

Work out dates from the current date given below. When unsure, choose claude."""

# Routes offered with `extra` (LocalModel(extra=True)): the weather, the task list, and
# what's in the user's own records (recall.py).
EXTRA = """    weather: {"place": "town, state" or "" for home, "when": "now"|"today"|"tonight"|"morning"|"tomorrow"|"wind"|"rain"|"rain-tomorrow"}
      (the weather, temperature, wind, will it rain or snow)
    tasks: {"which": "next"|"list"|"waiting", "who": "a person's name" or ""}
      only when the request is about TASKS or a TO-DO list: what should I work on next, what's on my task list,
      what am I waiting on (from someone). Never for the calendar ("do I have anything Tuesday" is calendar_events).
    recall: {"query": "the subject to look for, in a few words"}
      when the answer is in the user's own records: what someone said, asked for or decided (in a meeting, a Slack
      message, an email or a past conversation), what a note or a saved fact says (a password, a code, a number, a
      name), when the user last did something or what it cost. Not for upcoming events (calendar_events), and not for
      whether someone emailed ("anything from Sam", "did Rover email me" are mail_search).
"""


def system(extra: bool = False) -> str:
    """The routing prompt; with `extra`, the weather, tasks and recall routes too."""
    return SYSTEM.replace("<EXTRA>", EXTRA if extra else "").replace(
        "<NEWS>", "the news" if extra else "the weather or news"
    )


log = logging.getLogger(__name__)

TOOLS = {"calendar_events", "mail_recent", "mail_search", "mail_summarize"}
EXTRA_TOOLS = {"weather", "tasks", "recall"}  # offered with LocalModel(extra=True)

# Requests that do something, not just look something up: always Claude.
_ACTION = re.compile(
    r"\b(send|sent|reply|respond|answer|forward|email (?:him|her|them|my|the)|write|draft|tell|text|message|call|"
    r"add|put|schedule|book|create|make|move|reschedule|change|update|edit|rename|cancel|delete|remove|"
    r"archive|mark|accept|decline|invite|order|buy|purchase|pay|remind|reminder|run|open|close|install|"
    r"fix|build|deploy|push|commit|merge|start|stop|turn|set|play|share|upload|download|save|print|"
    r"sending|forwarding|forwarded|replying|replied|deleting|cancell?ing|cancell?ed|unsubscribe|rsvp|"
    r"clear (?:my|the|out|off)|block (?:off|out|my|the|some|time)|let \w+(?: \w+)? know|"
    r"do (?:it|that|this|them|so|what)|go ahead|take care of|handle (?:it|that|this|them)|act on)\b"
)
# "Email Bob that...", "text Sarah": a message to someone, said first. ("Email from Bob" is a lookup.)
_ADDRESS = re.compile(
    r"^(?:(?:please|can you|could you|would you|will you|hey|scout)[ ,]+)*(?:e-?mail|text|message) "
    r"(?!(?:from|about|in|on|today|yesterday|this|lately|recently)\b)\w"
)
# Judgment, conditions, or several steps: Claude.
_JUDGMENT = re.compile(
    r"\b(should|whether|if so|and if|then|why|recommend|advice|think|worth|better)\b|,\s*and\b"
)
# With the extra routes, also several questions or clauses in one ("...and did you reply",
# "what did it say - do it"): Claude.
_SEVERAL = re.compile(
    r"\band (?:did|do|does|have|has|had|can|could|will|would|is|are|was|were|what|when|where|who|how|also|please)\b"
    r"|[?!;]\s*\S|(?<![dmsj]r)(?<!mrs)(?<!st)\.\s+\S|\s[-\u2013\u2014]+\s|\u2014"
    r"|\bif (?:not|it|they|so|there)\b|\bso (?:that )?(?:i|you|we) can\b"
)
# A verb asked about in the past is a question about the record, not an action: "when did
# I last CHANGE the oil", "what did Pat TELL me", "have I PAID the plumber".
_AUX = {"did", "didn't", "have", "haven't", "has", "hasn't", "had", "hadn't", "is", "are", "was", "were"}
_QUESTION = re.compile(
    r"^(?:(?:hey|scout|ok|okay|so|and)[ ,]+)*(?:what|what's|when|when's|where|where's|who|who's|which|how|"
    r"did|didn't|have|has|had|was|were|is|are|do you (?:remember|know))\b"
)


# Only requests that sound like the calendar or the inbox are worth a model call;
# general questions go straight to Claude (faster, and the model can't misfile them).
_LOOKUP = re.compile(
    r"\b(calendar|schedule|agenda|meetings?|appointments?|events?|standup|busy|free|plans?|"
    r"when(?:'s| is| are| do)|what(?:'s| is) on|do i have|e-?mails?|inbox|mail|messages?|unread|from \w+)\b"
)
# ...and with the extra routes, the weather, the task list and Scout's records.
_EXTRA_LOOKUP = re.compile(
    r"\b(weather|forecast|temperature|rain(?:ing|y)?|snow(?:ing|y)?|wind(?:y)?|cold|hot|warm|umbrella|degrees|"
    r"tasks?|to-?dos?|to do list|owes?|waiting on|work on|priorit\w*|"
    r"said|say|says|told|tell me|mention(?:ed)?|decided?|decision|agreed?|talk(?:ed)? about|discuss(?:ed)?|"
    r"notes?|noted|wrote|written|remember|recall|last time|did (?:i|we)|when did|what did|what was|"
    r"what were|where did|who did|password|code|combination|serial|model number|account number|journal|"
    r"minutes|conversation)\b"
)


# ...and one just after an article or a possessive is a thing: "the pricing CHANGE", "my ORDER".
_DET = {"the", "a", "an", "my", "our", "your", "his", "her", "their", "this", "that", "its"}


def _acting(low: str, extra: bool) -> re.Match[str] | None:
    """The first action word that makes this a request to do something, or None."""
    asked = extra and _QUESTION.match(low) is not None
    for m in _ACTION.finditer(low):
        before = low[: m.start()].split()
        recent = before[-3:]
        about_past = any(w in _AUX for w in recent) and "you" not in recent  # "did you send it": Claude
        if asked and (about_past or any(w in _DET for w in before[-2:])):
            continue  # "when did I last change...", "the pricing change": not an action
        return m
    return None


NOT_A_LOOKUP = "not a lookup"


def is_question(request: str) -> bool:
    return _QUESTION.match(" ".join(request.lower().replace("\u2019", "'").split())) is not None


def guard(request: str, extra: bool = False, on_record: bool = False) -> str | None:
    """Why this request must go to Claude no matter what the model says, or None.
    `extra`: the weather, tasks and recall routes are offered too. `on_record`: a
    record in the store shares the question's words (recall.on_record), so it counts
    as a lookup even without a lookup word."""
    low = " ".join(request.lower().replace("\u2019", "'").split())
    if not (_LOOKUP.search(low) or (extra and (on_record or _EXTRA_LOOKUP.search(low)))):
        return NOT_A_LOOKUP
    if m := _acting(low, extra):
        return f"action word {m.group(1)!r}"
    if _ADDRESS.search(low):
        return "a message to someone"
    if m := _JUDGMENT.search(low):
        return f"needs judgment ({m.group(0).strip()!r})"
    if extra and (m := _SEVERAL.search(low.rstrip("?.! "))):
        return f"more than one thing ({m.group(0).strip()!r})"
    if len(low.split()) < 2:
        return "too short to tell"
    return None


def parse(output: str, today: str, extra: bool = False) -> dict[str, Any] | None:
    """The model's decision as {"tool": name, "args": {...}}, or None (= Claude).
    Anything but a well-formed read-only tool call counts as Claude."""
    m = re.search(r"\{.*\}", output, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    allowed = TOOLS | EXTRA_TOOLS if extra else TOOLS
    if extra and isinstance(d, dict) and d.get("route") in allowed:
        # {"route": "recall", "query": ...}: the tool named as the route, its args inline.
        inline = {k: v for k, v in d.items() if k not in ("route", "tool", "args")}
        d = {"route": "tool", "tool": d["route"], "args": d.get("args") or inline}
    if not isinstance(d, dict) or d.get("route") != "tool" or d.get("tool") not in allowed:
        return None
    raw_args = d.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    if d["tool"] == "calendar_events":
        return _calendar_args(args, today)
    if d["tool"] == "mail_search" and not str(args.get("query", "")).strip():
        return None  # a search with nothing to search for: the model didn't really know
    if d["tool"] == "mail_summarize":
        return _summarize_args(args)
    if d["tool"] in EXTRA_TOOLS:
        return _extra_args(d["tool"], args)
    return {"tool": d["tool"], "args": args}


WEATHER_WHEN = ("now", "today", "tonight", "morning", "tomorrow", "wind", "rain", "rain-tomorrow")


def _extra_args(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if tool == "weather":
        when = str(args.get("when") or "now").strip().lower()
        place = str(args.get("place") or "").strip()[:60]
        if place.lower() in ("home", "here", "my location"):
            place = ""
        return {"tool": "weather", "args": {"place": place, "when": when if when in WEATHER_WHEN else "now"}}
    if tool == "tasks":
        which = str(args.get("which") or "").strip().lower()
        if which not in ("next", "list", "waiting"):
            return None
        out: dict[str, Any] = {"which": which}
        if which == "waiting" and (who := str(args.get("who") or "").strip()[:40]):
            out["who"] = who
        return {"tool": "tasks", "args": out}
    query = str(args.get("query") or "").strip()[:200]  # recall
    return {"tool": "recall", "args": {"query": query} if query else {}}


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


def messages(request: str, context: str, extra: bool = False) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system(extra) + "\n\n" + context},
        {"role": "user", "content": request},
    ]


# --- running it --------------------------------------------------------------------------


class LocalModel:
    """The tier 1 model, loaded once and run on its own thread (MLX, Apple GPU)."""

    def __init__(self, name: str, timeout_s: float = 3.0, extra: bool = False) -> None:
        self.name, self.timeout_s = name, timeout_s
        self.extra = extra  # also the weather, tasks and recall routes (claude.local_recall)
        self.store: Any = None  # recall's store: index.default() unless set (the bench's own)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tier1")
        self._model: Any = None
        self._tok: Any = None
        self.ready = False

    def _load(self) -> None:
        from huggingface_hub import snapshot_download
        from mlx_lm import load

        from .asr import limit_mlx_cache

        limit_mlx_cache()
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
        if not self.ready:
            return None
        why = guard(request, self.extra)
        if why == NOT_A_LOOKUP and self.extra and is_question(request):
            # No lookup word, but a question: it may be about something in the records
            # ("who is the furnace technician"). A quick search by words tells.
            from . import index, recall

            if await asyncio.to_thread(recall.on_record, request, self.store or index.default()):
                why = guard(request, self.extra, on_record=True)
        if why:
            return None
        context = f"Now: {now.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')} (today is {now.date().isoformat()})."
        loop = asyncio.get_running_loop()
        try:
            out = await asyncio.wait_for(
                loop.run_in_executor(self._pool, self._generate, messages(request, context, self.extra), 80),
                self.timeout_s,
            )
        except TimeoutError:
            log.info("tier 1 took over %.0f s; asking Claude", self.timeout_s)
            return None
        decision = parse(out, now.date().isoformat(), self.extra)
        if decision is not None and decision["tool"] == "recall":
            decision["args"]["question"] = request  # answered from the records, then checked against it
        return decision


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
        return (
            f"The latest from {sender_name(m.get('sender', ''))} is about {_subject(m.get('subject', ''))}."
        )
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


async def summarize(
    args: dict[str, Any], now: dt.datetime, summaries: Summaries, out: dict[str, Any] | None = None
) -> str:
    """Read the message(s) and summarize them here; nothing goes to Claude. `out`
    gets "refs": the messages spoken about."""
    from . import mail_mac

    query = args.get("query")
    t0 = time.perf_counter()
    msgs = await mail_mac.message_data(1 if query else SUMMARY_COUNT, bool(args.get("unread_only")), query)
    if out is not None:
        out["refs"] = [f"mail:{m['id']}" for m in msgs if m.get("id") is not None]
    t1 = time.perf_counter()
    full = await mail_mac.bodies([m["id"] for m in msgs])
    t2 = time.perf_counter()
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
    log.info(
        "mail summary timings: find %.1f s, read %.1f s, summarize %.1f s (%d message%s)",
        t1 - t0, t2 - t1, time.perf_counter() - t2, len(msgs), "s" * (len(msgs) != 1),
    )  # fmt: skip
    return speak_summaries(msgs, texts, flagged, args, now)


def is_private(decision: dict[str, Any]) -> bool:
    """Whether the answer `run` gave for this decision came from private data (an email's
    text, a meeting, Slack, a task found in mail): withheld from Claude in strict mode."""
    return decision["tool"] == "mail_summarize" or bool(decision.get("private"))


async def run(
    decision: dict[str, Any],
    now: dt.datetime,
    summaries: Summaries | None = None,
    store: Any = None,
) -> str | None:
    """Do the read-only lookup and say the answer; None when it turns out the lookup
    can't answer it (Claude then takes the request). Sets decision["private"] when the
    answer came from private data (is_private)."""
    from . import calendar_mac, mail_mac

    tool, args = decision["tool"], decision["args"]
    if tool in EXTRA_TOOLS:
        return await _run_extra(decision, now, summaries, store)
    if tool == "calendar_events":
        data = await calendar_mac.event_data(args.get("start"), args.get("end"), args.get("query"))
        return speak_calendar(data, args, now)
    if tool == "mail_summarize":
        if summaries is None or not summaries.available:
            raise ToolError("the local model isn't ready to summarize email yet")
        return await summarize(args, now, summaries, out=decision)
    if tool in ("mail_search", "mail_recent") and summaries is not None and summaries.available:
        # "Anything from Rover?" wants what it says, not its subject line (heard live: the
        # subject alone sounded cut off). A local gist costs ~1 s and stays on the Mac.
        q = args.get("query") if tool == "mail_search" else None
        args = {"query": q} if q else {"unread_only": args.get("unread_only", True)}
        # Now a summary of what the email says: the caller marks the answer private.
        decision.update(tool="mail_summarize", args=args)
        return await summarize(args, now, summaries, out=decision)
    msgs = await mail_mac.message_data(
        args.get("count") or 5,
        bool(args.get("unread_only")),
        args.get("query") if tool == "mail_search" else None,
    )
    spoken = msgs[:1] if tool == "mail_search" else msgs[:3]
    decision["refs"] = [f"mail:{m['id']}" for m in spoken if m.get("id") is not None]
    return speak_mail(msgs, args, tool)


async def _run_extra(
    decision: dict[str, Any], now: dt.datetime, summaries: Summaries | None, store: Any
) -> str | None:
    from . import index, recall, refs, tasks, weather

    tool, args = decision["tool"], decision["args"]
    if tool == "weather":
        return weather.speak(await weather.forecast(args.get("place") or None), args.get("when") or "now")
    if tool == "tasks":
        rows = await asyncio.to_thread(tasks.all_tasks)
        people = await asyncio.to_thread(tasks._people)
        if args["which"] == "next":
            text, private = tasks.speak_next(rows, now.date(), people)
        elif args["which"] == "list":
            text, private = tasks.speak_list(rows, now.date(), people)
        else:
            got = tasks.speak_waiting(rows, now.date(), args.get("who") or "")
            if got is None:
                return None  # nothing from them on the list (maybe not a person): Claude
            text, private = got
        decision["private"] = private
        decision["refs"] = refs.task_refs(text, rows)
        return text
    # recall: from Scout's records, checked against them (recall.py)
    model = summaries.model if summaries is not None else None
    if model is None or not model.ready:
        return None
    question = str(args.get("question") or args.get("query") or "")
    try:
        found = await recall.answer(
            question, str(args.get("query") or ""), model, store or index.default(), now
        )
    except (ToolError, OSError, TimeoutError) as exc:  # the memory service is down, or the model slow
        log.info("recall unavailable (%s); asking Claude", exc)
        return None
    if found is None:
        return None
    decision["private"] = found.private
    decision["refs"] = [refs.norm(found.source.id)]
    return found.text
