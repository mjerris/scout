"""The records Scout's words were about, so "give me a link to that" has an answer.

Everything Scout says out loud carries the ids of the store records it spoke about
(task:15, granola:<note>, mail:<n>, note:3, slack:<channel>:<ts>, journal:...): tier 0
handlers set them (local_intents.Context.refs), the pulse and the briefing attach them
to their sentences (Spoken), tier 1 sets decision["refs"], and a Claude turn collects
them from its tools' results (from_tool). The app keeps a short log of what was said
with its refs (Log); "that" is the last thing said that had refs.

The links themselves are the store's (index.Index.resolve, links-contract): this module
only picks the record, says where it came from, and puts its link on the phone (a
reminder in the "Scout" list with the URL, or the task's own reminder, which already
has it).

A fact (memory:<id>) comes from the turn it was told in (its told_in link): "You told
me on October 12, in the room."; one with no such turn is "a fact you saved".

Privacy (privacy.py): the room and the phone are the user's own, so spoken answers here
use real titles, as tier 0 does. What Claude sees (the open_link and send_link tool
results, the request context) follows the privacy mode: in strict mode a record from
mail, a meeting or Slack (or a task found in one) is named only by its kind ("a task
from a meeting"), never by its title or text. Its link is still given and still sent:
a link is the user's own pointer to their own record, and opening it shows nothing to
Claude. In balanced mode titles are shown (as local_search shows them), never bodies.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import index, privacy, reminders_mac
from .mac import ToolError

log = logging.getLogger(__name__)

LINK_LIST = "Scout"  # the reminder list links are put in (Scout's own: no spoken yes)
RECENT_S = 600.0  # "that" reaches back this far
MAX_REFS = 12  # refs kept for one thing said
_CHUNK = re.compile(r"#\d+$")  # granola:<note>#3, slack:<c>:<ts>#2: chunks of one record
_ID = re.compile(r"^[a-z]+:[^\s]+$")

# How a record is named when its title may not be shown (strict mode), and in speech.
KIND = {
    "task": "a task",
    "meeting": "meeting notes",
    "mail": "an email",
    "slack": "a Slack message",
    "note": "a note",
    "list": "a list",
    "turn": "a past conversation",
    "memory": "something you told me to remember",
    "fact": "something you told me to remember",
    "behavior": "a rule you set for how I behave",
    "reference": "the offline reference",
    "github": "a GitHub item",
}
_SOURCE_RELS = ("found_in", "merged_from", "same_as", "told_in")


def norm(ref: str) -> str:
    """One id per record: a chunk's id is its record's (a GitHub item's #number isn't a chunk)."""
    ref = str(ref).strip()
    return ref if ref.startswith("github:") else _CHUNK.sub("", ref)  # github:owner/repo#117 is whole


def clean(refs: Iterable[str]) -> tuple[str, ...]:
    """Ids, normalized, each once, in order; anything that isn't an id is dropped."""
    out: dict[str, None] = {}
    for r in refs:
        r = norm(r)
        if _ID.match(r):
            out[r] = None
    return tuple(out)[:MAX_REFS]


class Spoken(str):
    """A sentence to say, with the ids of the records it is about (and whether it used
    private text). Compares and prints as the plain string."""

    refs: tuple[str, ...]
    private: bool

    def __new__(cls, text: str, refs: Iterable[str] = (), private: bool = False) -> Spoken:
        s = super().__new__(cls, text)
        s.refs = clean(refs)
        s.private = private
        return s


class Done(Spoken):
    """The short line for an action the user said yes to that went through ("Sent to Pat
    Doe."). In the room the confirm sound says it instead (Assistant._confirmed); `rest`
    is the part of it that's said anyway (news beyond "it's done")."""

    rest: str

    def __new__(cls, text: str, refs: Iterable[str] = (), private: bool = False, rest: str = "") -> Done:
        s = str.__new__(cls, text)
        s.refs, s.private, s.rest = clean(refs), private, rest
        return s


def refs_of(text: Any) -> tuple[str, ...]:
    return tuple(getattr(text, "refs", ()))


def private_of(text: Any) -> bool:
    return bool(getattr(text, "private", False))


def joined(parts: Sequence[Any], sep: str = " ") -> Spoken:
    """Parts joined, keeping every part's refs."""
    return Spoken(
        sep.join(str(p) for p in parts if p),
        [r for p in parts for r in refs_of(p)],
        any(private_of(p) for p in parts),
    )


def task_refs(text: str, tasks: Iterable[Any]) -> list[str]:
    """task:<n> for each task whose (spoken) title is in `text`, in the order said."""
    from .tasks import spoken_title

    found = []
    for t in tasks:
        title = spoken_title(t)
        if title and (i := text.find(title)) >= 0:
            found.append((i, f"task:{t.n}"))
    return [r for _, r in sorted(found)]


# --- what was said ------------------------------------------------------------------------


@dataclass
class Said:
    ts: float
    who: str  # "Scout", "pulse", "briefing", a Claude session
    text: str
    refs: tuple[str, ...] = ()
    private: bool = False  # worked out from private text (strict mode withholds it from Claude)
    channel: str = "room"  # "room", or the phone agent that asked
    request: str = ""  # what the user asked, when it was an answer


class Log:
    """The last things said, newest last."""

    def __init__(self, maxlen: int = 30) -> None:
        self.items: deque[Said] = deque(maxlen=maxlen)

    def add(self, said: Said) -> None:
        self.items.append(said)

    def recent(self, seconds: float = RECENT_S, channel: str | None = None) -> list[Said]:
        now = time.time()
        return [s for s in self.items if now - s.ts < seconds and (channel is None or s.channel == channel)]

    def last(self, channel: str | None = "room") -> Said | None:
        """What "that" means: the last thing said, or the one before when the last
        had no records (an "okay" or the time in between)."""
        recent = self.recent(channel=channel)
        for s in reversed(recent[-2:]):
            if s.refs:
                return s
        return None


_log = Log()


def log_of() -> Log:
    return _log


def use(new: Log) -> Log:
    """The app's log (each Assistant makes its own; the shared tools read it)."""
    global _log
    _log = new
    return new


# --- refs from Claude's tool results ------------------------------------------------------

_OUT = {
    "tasks_list": (re.compile(r"^(\d+)\. ", re.M), "task:{}"),
    "task_why": (re.compile(r"^Task (\d+):", re.M), "task:{}"),
    "task_add": (re.compile(r"^Added task (\d+):", re.M), "task:{}"),
    "task_update": (re.compile(r"^Updated task (\d+):", re.M), "task:{}"),
    "notes_find": (re.compile(r"^Note (\d+) \(", re.M), "note:{}"),
    "note_add": (re.compile(r"^Saved note (\d+):", re.M), "note:{}"),
    "mail_recent": (re.compile(r"^id (\d+): ", re.M), "mail:{}"),
    "mail_search": (re.compile(r"^id (\d+): ", re.M), "mail:{}"),
    "local_search": (re.compile(r"\(ref ([^\s)]+)\)"), "{}"),
    "remember": (re.compile(r"\(ref (memory:[0-9a-f]+)\)"), "{}"),
    "recall": (re.compile(r"\(ref (memory:[0-9a-f]+)\)"), "{}"),
    "rule_add": (re.compile(r"\(ref (behavior:[0-9a-f]+)\)"), "{}"),
    "rules_list": (re.compile(r"\(ref (behavior:[0-9a-f]+)\)"), "{}"),
    "open_link": (re.compile(r"^ref: (\S+)", re.M), "{}"),
    "send_link": (re.compile(r"^ref: (\S+)", re.M), "{}"),
    "reference_search": (re.compile(r"\(ref (zim:[^\s)]+)\)"), "{}"),
    "reference_read": (re.compile(r"^ref: (zim:\S+)", re.M), "{}"),
    "github_items": (re.compile(r"\(ref (github:[^\s)]+)\)"), "{}"),
}
_ARG = {
    "task_done": "task:{}",
    "task_update": "task:{}",
    "task_why": "task:{}",
    "mail_read": "mail:{}",
    "mail_read_full": "mail:{}",
}


def from_tool(name: str, args: dict[str, Any], output: str) -> list[str]:
    """The record ids a tool call was about, from its arguments and its output."""
    base = name.rsplit("__", 1)[-1]
    out = []
    if (fmt := _ARG.get(base)) and str((args or {}).get("id") or "").isdigit():
        out.append(fmt.format(int(args["id"])))
    if got := _OUT.get(base):
        pattern, fmt = got
        out += [fmt.format(m) for m in pattern.findall(output or "")]
    return list(clean(out))


# --- resolving and naming -----------------------------------------------------------------


def _store() -> Any:
    return index.default()


async def resolve(ref: str, store: Any = None) -> dict[str, Any] | None:
    """The store's record of `ref` with its links (index.Index.resolve), or None. An
    offline reference article (zim:...) isn't in the store: reference.py has it."""
    if norm(ref).startswith("zim:"):
        from . import reference

        return await asyncio.to_thread(reference.resolve, norm(ref))
    store = _store() if store is None else store
    fn = getattr(store, "resolve", None)
    if fn is None:
        log.info("links: this store can't resolve records yet")
        return None
    try:
        r = await asyncio.to_thread(fn, norm(ref))
    except (ToolError, OSError) as exc:
        log.warning("links: resolving %s failed: %s", ref, exc)
        return None
    return r if isinstance(r, dict) else None


async def _hit(ref: str, store: Any) -> index.Hit | None:
    try:
        return await asyncio.to_thread(store.get, ref)
    except (ToolError, OSError) as exc:
        log.info("links: no record %s: %s", ref, exc)
        return None


def source_of(r: dict[str, Any]) -> dict[str, Any] | None:
    """Where a record came from: its first found_in (or merged_from, same_as) record; a
    fact's, the turn it was told in (told_in)."""
    related = [x for x in r.get("related") or [] if isinstance(x, dict)]
    for rel in _SOURCE_RELS:
        for x in related:
            if x.get("rel") == rel and (x.get("dir") or "out") == "out":
                return x
    return None


def best_url(r: dict[str, Any]) -> str | None:
    """The link to give for a record: its source's (a task found in a meeting opens the
    meeting), else its own. A task's own is already the right one (the store's resolve:
    the GitHub item it's about, else its source's)."""
    if r.get("kind") == "task" and r.get("url"):
        return str(r["url"])
    src = source_of(r)
    if src is not None and src.get("url"):
        return str(src["url"])
    return str(r["url"]) if r.get("url") else None


def kind_word(kind: str) -> str:
    return KIND.get(kind, "a record")


def shown_title(r: dict[str, Any], mode: str | None = None) -> str:
    """The record's title as Claude may see it in privacy mode `mode`."""
    mode = mode or privacy.current().mode
    title = str(r.get("title") or "").strip()
    if mode == "strict" and r.get("private"):
        if r.get("kind") == "task" and (src := source_of(r)) is not None:
            return f"a task from {kind_word(str(src.get('kind') or ''))}"
        return kind_word(str(r.get("kind") or ""))
    return title or kind_word(str(r.get("kind") or ""))


def describe(r: dict[str, Any], mode: str | None = None) -> str:
    """The open_link result: the record, its link and where it came from, for Claude."""
    lines = [f"ref: {r.get('id')}", f"{kind_word(str(r.get('kind') or ''))}: {shown_title(r, mode)}"]
    if url := best_url(r):
        lines.append(f"link: {url}")
    if (page := r.get("page")) and page != url:
        lines.append(f"Scout's page for it: {page}")
    if (src := source_of(r)) is not None and src.get("rel") == "told_in":  # the turn's words: the fact's own
        lines.append(f"from: {src.get('id')} ({kind_word('turn')})")
    elif src is not None:
        lines.append(f"from: {src.get('id')} ({shown_title({**src, 'private': r.get('private')}, mode)})")
    if r.get("private") and (mode or privacy.current().mode) == "strict":
        lines.append("(Its text stays on this Mac in strict privacy mode; the link is the user's own.)")
    return "\n".join(lines)


def _day(ts: float) -> str:
    d = dt.datetime.fromtimestamp(ts)
    return f"{d:%B} {d.day}"


_CHANNEL = {"room": "in the room", "phone": "on the phone"}


async def _fact_from(r: dict[str, Any], src: dict[str, Any] | None, store: Any) -> str:
    """ "You told me on October 12, in the room." (the turn it was told in), or "It's a
    fact you saved on October 12." when no turn is known (one written into facts.md)."""
    if src is not None and src.get("rel") == "told_in":
        h = await _hit(str(src.get("id") or ""), store)
        when = f" on {_day(h.ts)}" if h and h.ts else ""
        where = _CHANNEL.get(h.who.strip()) if h else None
        return f"You told me{when}" + (f", {where}" if where else "") + "."
    h = await _hit(str(r.get("id") or ""), store)
    return "It's a fact you saved" + (f" on {_day(h.ts)}" if h and h.ts else "") + "."


def _mention(x: dict[str, Any], h: index.Hit | None) -> str:
    """ "a Slack message from Pat on October 14", "your meeting Vendor sync on October 13"."""
    from .briefing import sender_name

    kind, title = str(x.get("kind") or ""), str(x.get("title") or "").strip().rstrip(".")
    who = sender_name(h.who.split(",")[0]) if h and h.who.strip() else ""
    when = f" on {_day(h.ts)}" if h and h.ts else ""
    if kind == "meeting":
        return f"your meeting {title}{when}" if title else f"a meeting{when}"
    if kind == "mail":
        return f"an email from {who}{when}" if who else f"an email{when}"
    if kind == "slack":
        return f"a Slack message from {who}{when}" if who else f"a Slack message{when}"
    if kind == "task":
        return f"task {str(x.get('id', '')).split(':', 1)[-1]}, {title}"
    return f"{kind_word(kind)}{when}"


async def _github_from(r: dict[str, Any], store: Any) -> str:
    """ "It's PR 117 in api, Fix the jitter buffer. It was merged on October 14. It came up in
    a Slack message from Pat on October 14." (the first record that named it)."""
    from .github import name, state_words

    h = await _hit(str(r.get("id") or ""), store)
    if h is not None:
        title = " ".join(h.title.split()).rstrip(".")
        said = f"It's {name(h)}" + (f", {title}" if title else "") + f". It {state_words(h)}."
    else:
        said = f"It's {shown_title(r, 'open')}, on GitHub."
    named = [x for x in r.get("related") or [] if x.get("rel") == "mentions" and x.get("dir") == "in"]
    if not named:
        return said
    first = named[0]
    return f"{said} It came up in {_mention(first, await _hit(str(first.get('id') or ''), store))}."


async def where_from(r: dict[str, Any], store: Any = None) -> str:
    """ "It's from your meeting Vendor sync, on October 13." Spoken on the Mac or the
    phone (the user's own), so with the real title."""
    store = _store() if store is None else store
    src = source_of(r)
    if r.get("kind") == "memory":
        return await _fact_from(r, src, store)
    if r.get("kind") == "github":
        return await _github_from(r, store)
    thing = src or r
    h = await _hit(str(thing.get("id") or ""), store)
    kind = str(thing.get("kind") or (h.kind if h else ""))
    title = str(thing.get("title") or (h.title if h else "")).strip().rstrip(".")
    from .briefing import sender_name

    who = sender_name(h.who) if h and h.who.strip() else ""
    when = f", on {_day(h.ts)}" if h and h.ts else ""
    if kind == "meeting":
        what = f"your meeting {title}" if title else "a meeting"
    elif kind == "mail":
        what = f"an email from {who}" + (f" about {title}" if title else "") if who else f"the email {title}"
    elif kind == "slack":
        what = f"a Slack message from {who}" if who else "a Slack message"
    elif kind == "note":
        what = f"your note {title}" if title else "one of your notes"
    elif kind == "turn":
        what = "something you asked me"
    elif kind == "task" and src is None:
        return f"That's task {str(r.get('id', '')).split(':', 1)[-1]}, {title}; you added it yourself."
    else:
        what = f"{kind_word(kind)}" + (f", {title}" if title else "")
    return f"It's from {what}{when}."


# --- what Claude is told -------------------------------------------------------------------

CONTEXT_ITEMS = 4  # things said with refs, shown in the request context
CONTEXT_TIMEOUT_S = 1.5
_WHO = {"pulse": "the pulse check-in", "briefing": "the briefing", "Scout": "Scout"}


def _hit_private(h: index.Hit) -> bool:
    from .recall import is_private

    return is_private(h)


async def label(ref: str, mode: str, store: Any = None) -> str:
    """'task:15 (a task: Vendor quote)', the title as privacy mode `mode` allows."""
    h = await _hit(ref, _store() if store is None else store)
    if h is None:
        return ref
    if mode == "strict" and _hit_private(h):
        if h.kind == "task":
            from .tasks import FROM, found_from

            src = found_from(h.meta)
            return f"{ref} (a task from {FROM[src]})" if src else f"{ref} (a task)"
        return f"{ref} ({kind_word(h.kind)})"
    title = " ".join(h.title.split())[:100]
    return f"{ref} ({kind_word(h.kind)}: {title})" if title else f"{ref} ({kind_word(h.kind)})"


async def context_lines(log_: Log, mode: str, store: Any = None) -> list[str]:
    """The request context's lines on what was said lately and the records it was about,
    so "that" resolves to an id. Private text is withheld in strict mode."""
    items = [s for s in log_.recent() if s.refs][-CONTEXT_ITEMS:]
    if not items:
        return []
    store = _store() if store is None else store

    async def one(s: Said) -> str:
        when = dt.datetime.fromtimestamp(s.ts).strftime("%-I:%M %p")
        who = f"answering {s.request!r}" if s.request else _WHO.get(s.who, s.who)
        if s.channel != "room":
            who += f", on the {s.channel}"
        if s.private and mode == "strict":
            text = "(said from private data; withheld in strict privacy mode)"
        else:
            text = " ".join(s.text.split())
            text = repr(text if len(text) <= 200 else text[:200].rsplit(" ", 1)[0] + " ...")
        try:
            names = await asyncio.wait_for(
                asyncio.gather(*(label(r, mode, store) for r in s.refs[:6])), CONTEXT_TIMEOUT_S
            )
        except (TimeoutError, ToolError, OSError):
            names = list(s.refs[:6])
        return f"  [{when}, {who}] {text} -> refs: {'; '.join(names)}"

    head = (
        'Records Scout spoke about recently, newest last ("that" usually means the newest; '
        "open_link and send_link take a ref):"
    )
    return [head, *[await one(s) for s in items]]


# --- sending a link to the phone ---------------------------------------------------------

_sent: dict[str, float] = {}  # url -> when its reminder was added (no duplicates within the hour)
SENT_KEEP_S = 3600.0


async def send(r: dict[str, Any], store: Any = None) -> tuple[str, str]:
    """Put the record's link on the phone. Returns (where, spoken): where is "task" (its
    reminder already has the link), "reminder" (a new one in the Scout list) or "again"
    (sent within the hour)."""
    url = best_url(r)
    if not url:
        raise ToolError("that record has no link")
    if str(r.get("id", "")).startswith("task:"):
        h = await _hit(str(r["id"]), _store() if store is None else store)
        # The task mirror set its reminder's URL to this link (reminder_url): nothing to add.
        if h is not None and h.meta.get("reminder") and h.meta.get("reminder_url"):
            return "task", "It's in that task's reminder, on your phone."
    now = time.time()
    for u in [u for u, t in _sent.items() if now - t > SENT_KEEP_S]:
        del _sent[u]
    if url in _sent:
        return "again", "The link is already in your Scout reminders."
    src = source_of(r)
    named = src if src is not None and src.get("rel") != "told_in" else r  # a fact by its own words
    title = str(named.get("title") or r.get("title") or "a record").strip()
    await reminders_mac.add_data(
        {"title": f"Link: {title}"[:200], "list": LINK_LIST, "url": url}, mirror=True
    )
    _sent[url] = now
    return "reminder", "I put the link in your Scout reminders."


# --- the tools (room, phone and other sessions: shared_tools) ------------------------------


def _about_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


async def pick(ref: str = "", about: str = "", store: Any = None) -> list[str]:
    """The ref(s) meant: the one given, else "that" (the last thing said with refs), else
    the recently said refs whose titles share words with `about`."""
    if ref.strip():
        return [norm(ref)]
    about = about.strip()
    if not about or about.lower() in ("that", "it", "this", "that one", "the last one"):
        said = log_of().last(channel=None)
        return list(said.refs) if said else []
    want = _about_words(about)
    store = _store() if store is None else store
    scored: dict[str, int] = {}
    for s in reversed(log_of().recent(channel=None)):
        for r in s.refs:
            if r in scored:
                continue
            h = await _hit(r, store)
            have = _about_words(" ".join((h.title, h.who)) if h else "") | _about_words(s.text)
            scored[r] = len(want & have)
    best = max(scored.values(), default=0)
    return [r for r, n in scored.items() if n == best] if best else []


async def open_link(a: dict[str, Any]) -> str:
    refs = await pick(str(a.get("ref") or ""), str(a.get("about") or ""))
    if not refs:
        return (
            "Nothing said recently has a record to link to. Give a ref (from the request "
            "context or a tool result), or find the record with local_search."
        )
    out = []
    for ref in refs[:5]:
        r = await resolve(ref)
        out.append(describe(r) if r is not None else f"ref: {ref}\n(no such record in Scout's store)")
    head = "" if len(refs) == 1 else f"{len(refs)} records match; ask which, or pass one ref:\n"
    return head + "\n\n".join(out)


async def send_link(a: dict[str, Any]) -> str:
    refs = await pick(str(a.get("ref") or ""), str(a.get("about") or ""))
    if not refs:
        raise ToolError("no record to send: give a ref from the request context or a tool result")
    if len(refs) > 1:
        raise ToolError("more than one record matches: " + ", ".join(refs[:5]) + "; pass one ref")
    r = await resolve(refs[0])
    if r is None:
        raise ToolError(f"there's no record {refs[0]}")
    where, spoken = await send(r)
    return f"ref: {r.get('id')}\n{spoken}" + ("" if where != "task" else " (no new reminder was added)")
