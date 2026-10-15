"""Scout's behavior rules: how the user wants Scout to behave ("from now on, call me
Mike", "always read ticket numbers digit by digit"), learned over time and given to
every session (docs/behavior-rules.md).

Not facts (memory.py: what's true about the user's world, looked up when a request
needs one) and not approvals (rules.py and trust.py: which actions run without a
spoken yes). A behavior rule is an instruction, so all of them go into every session's
prompt: the room (brain.py), the phone agent (phone_service.py), Claude for phone
requests (brain.PhoneBrain) and desk sessions (the MCP server's instructions).

Kept in memory/behavior.md, one rule per line, editable by hand:

    - Call me Mike · told · added 2026-10-15 · confirmed 2026-10-15
    - Read ticket numbers digit by digit · from a fix · added 2026-10-15 · enforced: speech.speak_ids

After the rule, " · " fields: where it came from (told, set by Mike, from a fix, from
corrections), the dates it was added and last confirmed, the journal turns a suggested
rule came from (turns: ...), and `enforced: <code>` for a rule the code itself keeps:
those are listed but never put in a prompt (the code already does it, and the prompt
stays small). At most MAX_INJECTED rules are injected; past that the newest confirmed
are, and the prompt says how many aren't, so the user is asked to review and merge.

A rule's record id is behavior:<hash of its words> (`rule_id`); the store indexes the
file (kind "behavior") and links a told rule to the turn it was told in (told_in),
as it does a fact.

Run as `python -m scout.behavior list | add "<rule>" [--enforced <ref>] [--origin <text>]
| drop <words> --yes` (scoutctl rules ...)."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import logging
import os
import re
import sys
import tempfile
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA
from .mac import ToolError
from .memory import _clean, keywords
from .memory import spoken as spoken

log = logging.getLogger(__name__)

FILE = "behavior.md"  # in the memory folder, beside facts.md and journal/
PATH = DATA / "memory" / FILE
MAX_INJECTED = 40
MAX_CHARS = 200
UNUSED_DAYS = 30  # a rule this old that never came up in this long is offered for dropping

HEADER = (
    "# How Scout should behave\n\n"
    'One rule per line, starting with "- ". Edit freely. After a rule, " · " fields: where it\n'
    "came from, when it was added and last confirmed, and `enforced: <code>` for rules the code\n"
    "keeps itself (listed, not given to Claude).\n\n"
)

# Today's fixes, kept by code (the brief's seed): listed so the user sees them, not injected.
SEED: tuple[tuple[str, str], ...] = (
    (
        "When I ask about meetings today, tell me what's on now, what's left and what ended in the "
        "last hour, not the whole day",
        "tier1.from_now; brain.VOICE_PROMPT; phone_service.PROMPT",
    ),
    ("Read ticket, issue, PR and order numbers digit by digit", "speech.speak_ids"),
    ("Say phone numbers the way people say them", "contacts_mac.spoken_number"),
    ("Find Slack tasks only in conversations that involve me", "taskfarm.slack_involves"),
    ("Remember facts I state on calls", "phone_service.PROMPT; shared_tools remember"),
)


@dataclass(frozen=True)
class Rule:
    text: str
    origin: str = ""  # told | set by Mike | from a fix | from corrections | anything typed
    added: str = ""  # YYYY-MM-DD
    confirmed: str = ""  # YYYY-MM-DD: last said again, kept at a review
    enforced: str = ""  # the code that keeps it, if any
    turns: tuple[str, ...] = ()  # journal turns a suggested rule came from

    @property
    def id(self) -> str:
        return rule_id(self.text)


def normalized(text: str) -> str:
    """A rule as it's kept: one line, no full stop at the end, a capital first letter."""
    t = " ".join(str(text or "").split()).replace(" · ", ", ").strip().rstrip(".!")
    return t[:1].upper() + t[1:]


def rule_id(text: str) -> str:
    return "behavior:" + hashlib.blake2b(normalized(text).lower().encode(), digest_size=6).hexdigest()


_DATE = re.compile(r"^(added|confirmed) (\d{4}-\d\d-\d\d)$")


def parse_line(line: str) -> Rule | None:
    """The rule on a line, if it is one: "- " at the very start."""
    if not line.startswith("- "):
        return None
    parts = [p.strip() for p in line[2:].split(" · ")]
    text = normalized(parts[0])
    if not text:
        return None
    f: dict[str, Any] = {"origin": "", "added": "", "confirmed": "", "enforced": "", "turns": ()}
    for p in parts[1:]:
        if m := _DATE.match(p):
            f[m.group(1)] = m.group(2)
        elif p.startswith("enforced:"):
            f["enforced"] = p.removeprefix("enforced:").strip()
        elif p.startswith("turns:"):
            f["turns"] = tuple(p.removeprefix("turns:").split())
        elif p and not f["origin"]:
            f["origin"] = p
    return Rule(text, **f)


def render_line(r: Rule) -> str:
    parts = [r.text, r.origin]
    parts += [f"added {r.added}" if r.added else "", f"confirmed {r.confirmed}" if r.confirmed else ""]
    parts += [
        f"turns: {' '.join(r.turns)}" if r.turns else "",
        f"enforced: {r.enforced}" if r.enforced else "",
    ]
    return "- " + " · ".join(p for p in parts if p)


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def parse(text: str) -> list[Rule]:
    return [r for line in _lines(text) if (r := parse_line(line))]


def documents(text: str, mtime: float) -> list[dict[str, Any]]:
    """The rules as index items (kind "behavior"), for the store (index.py)."""
    out, seen = [], set()
    for r in parse(text):
        if r.id in seen:
            continue
        seen.add(r.id)
        ts = mtime
        with contextlib.suppress(ValueError):
            ts = dt.datetime.fromisoformat(r.added).timestamp() if r.added else mtime
        meta: dict[str, Any] = {"origin": r.origin}
        if r.enforced:
            meta["enforced"] = r.enforced
        if r.turns:
            meta["turns"] = list(r.turns)
        body = r.text + (f"\n(enforced in code: {r.enforced})" if r.enforced else "")
        out.append({"id": r.id, "title": r.text, "body": body, "ts": ts, "meta": meta})
    return out


def _today() -> str:
    return dt.date.today().isoformat()


class Behavior:
    """The rules in behavior.md, read fresh when the file changed (an edit by hand)."""

    def __init__(self, path: Path = PATH, seed: bool = True) -> None:
        self.path = path
        self.seed = seed
        self._lock = threading.RLock()
        self._sig: tuple[int, int] | None = None
        self._rules: list[Rule] = []

    def signature(self) -> tuple[int, int] | None:
        """(mtime, size) of the file, None when there is none: changes when it does."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _read(self) -> str:
        try:
            return self.path.read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""

    def rules(self) -> list[Rule]:
        with self._lock:
            if self.seed and not self.path.exists():
                try:
                    seeded = [render_line(Rule(t, "from a fix", _today(), _today(), ref)) for t, ref in SEED]
                    self._write(_lines(HEADER) + seeded)
                except OSError as exc:
                    log.warning("behavior rules not seeded (%s)", exc)
            sig = self.signature()
            if sig != self._sig:
                try:
                    self._rules = parse(self._read())
                except OSError as exc:
                    log.warning("behavior rules unreadable (%s)", exc)
                    self._rules = []
                self._sig = sig
            return list(self._rules)

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(("\n".join(lines) + "\n") if lines else "")
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _change(self, update: dict[str, Rule | None], new: Sequence[Rule] = ()) -> None:
        """Rewrite the file: rules by id in `update` replaced (None: dropped), `new` ones
        added at the end, every other line as it was."""
        text = self._read()
        lines = _lines(text) if text else _lines(HEADER)
        out = []
        for line in lines:
            r = parse_line(line)
            if r is None or r.id not in update:
                out.append(line)
            elif (u := update[r.id]) is not None:
                out.append(render_line(u))
        out += [render_line(r) for r in new]
        self._write(out)
        self._sig = None

    def add(
        self, text: str, origin: str = "told", enforced: str = "", turns: Iterable[str] = ()
    ) -> tuple[Rule, bool]:
        """Keep a rule; the same rule again only confirms it. (rule, whether it's new)."""
        rule = normalized(text)
        if not rule:
            raise ToolError("no rule to keep")
        if len(rule) > MAX_CHARS:
            raise ToolError(f"that rule is too long (max {MAX_CHARS} characters)")
        if any(ord(c) < 32 for c in rule):
            raise ToolError("that rule has control characters")
        with self._lock:
            have = {r.id: r for r in self.rules()}
            rid = rule_id(rule)
            if rid in have:
                old = have[rid]
                kept = dataclasses.replace(old, confirmed=_today(), enforced=enforced or old.enforced)
                self._change({rid: kept})
                return kept, False
            r = Rule(rule, origin, _today(), _today(), enforced, tuple(turns))
            self._change({}, [r])
            return r, True

    def matching(self, about: str) -> list[Rule]:
        """Rules with every content word of `about`, or the one with that id."""
        about = about.strip()
        rules = self.rules()
        if about.startswith("behavior:"):
            return [r for r in rules if r.id == about]
        want = keywords(about)
        return [r for r in rules if want and want <= keywords(r.text)]

    def drop(self, about: str) -> list[Rule]:
        """Drop the rules `about` names, except ones code enforces (they document it)."""
        with self._lock:
            gone = [r for r in self.matching(about) if not r.enforced]
            if gone:
                self._change(dict.fromkeys((r.id for r in gone), None))
            return gone

    def confirm(self, ids: Iterable[str]) -> None:
        with self._lock:
            want = set(ids)
            update: dict[str, Rule | None] = {
                r.id: dataclasses.replace(r, confirmed=_today()) for r in self.rules() if r.id in want
            }
            if update:
                self._change(update)

    def merge(self, keep: str, drop: str) -> None:
        """Two rules that say the same thing: one stays (confirmed today), one goes."""
        with self._lock:
            have = {r.id: r for r in self.rules()}
            if keep in have and drop in have and keep != drop:
                self._change({keep: dataclasses.replace(have[keep], confirmed=_today()), drop: None})

    def injected(self) -> tuple[list[Rule], int]:
        """The rules put in prompts (not enforced ones), at most MAX_INJECTED: the most
        recently confirmed. Returns them in file order, and how many were left out."""
        live = [r for r in self.rules() if not r.enforced]
        if len(live) <= MAX_INJECTED:
            return live, 0
        newest = sorted(range(len(live)), key=lambda i: (live[i].confirmed or live[i].added, i), reverse=True)
        keep = set(newest[:MAX_INJECTED])
        return [r for i, r in enumerate(live) if i in keep], len(live) - MAX_INJECTED

    def over_cap_note(self) -> str:
        """What to tell the user when there are more rules than are used."""
        live = sum(not r.enforced for r in self.rules())
        if live <= MAX_INJECTED:
            return ""
        return (
            f" That makes {live} rules; I use the {MAX_INJECTED} most recent. "
            "Say review my rules to merge some."
        )

    def prompt_block(self, heading: bool = True) -> str:
        """The rules for a session's prompt, or "" when there are none. Never fails.
        heading=False: without the markdown heading (the phone agent's section has its own)."""
        try:
            shown, hidden = self.injected()
        except Exception:
            log.exception("behavior rules not read")
            return ""
        if not shown:
            return ""
        lines = [
            *(["# The user's rules for how you behave"] if heading else []),
            'In their words ("me" is the user). Follow them unless the user asks otherwise now:',
            *(f"- {r.text}" for r in shown),
        ]
        if hidden:
            lines.append(
                f"({hidden} more rules aren't shown: suggest the user say review my rules to merge some.)"
            )
        return "\n".join(lines)


_default: Behavior | None = None  # tests put their own here


def default() -> Behavior:
    global _default
    if _default is None:
        _default = Behavior(PATH)
    return _default


def prompt_block(heading: bool = True) -> str:
    """The current rules for a prompt (default store), or ""."""
    try:
        return default().prompt_block(heading)
    except Exception:
        log.exception("behavior rules not read")
        return ""


def signature() -> tuple[int, int] | None:
    return default().signature()


# --- tier 0: "from now on ..." ----------------------------------------------------------

_FUTURE = r"(?:from now on|going forward|in (?:the )?future|from here on(?: out)?|for the future)"
_FUTURE_FIRST = re.compile(rf"^{_FUTURE}[,:]?\s+(.+)$", re.I)
_FUTURE_LAST = re.compile(rf"^(.+?),?\s+{_FUTURE}$", re.I)
_LEAD = re.compile(r"^(?:and |also |ok(?:ay)?,? (?:so )?|so,? )+", re.I)
_QUESTION = re.compile(
    r"^(?:what|why|how|when|where|who|which|do(?!\s+not\b)|does|did|is|are|am|was|were|will|would|should|could"
    r"|can|have|has)\b",
    re.I,
)
# Approvals are trust.py's ("always allow adding reminders", "stop asking me before ...").
_TRUST = re.compile(r"\b(?:allow|ask(?:ing)?(?: me)?(?: again)? (?:about|before)|go ahead with)\b", re.I)
_ALWAYS = re.compile(
    r"^(?:remember to |make sure (?:to |you )?|be sure to )?(always|never)\s+(\S+)(.*)$", re.I
)
_NOT_TO = re.compile(r"^(?:remember|make sure|be sure)(?: to)? not to\s+(.+)$", re.I)
_DONT = re.compile(r"^(?:don'?t|do not)\s+(.+)$", re.I)
_STOP = re.compile(r"^stop\s+([a-z]+ing)\b\s*(.*)$", re.I)
_STANDING = re.compile(r"\b(?:ever|anymore|any more|again|every time|each time|whenever)\b", re.I)
_TRAILING = re.compile(r"\s+(?:anymore|any more|ever again|again)$", re.I)
# Words after always/never that make it no instruction ("never mind", "always the same").
_NOT_VERB = {
    "mind", "been", "heard", "seen", "had", "knew", "know", "thought", "said", "got", "did", "was",
    "were", "is", "are", "the", "a", "an", "on", "off", "so", "there", "here", "same", "my", "your",
    "that", "this", "it", "ever", "again", "too", "late", "early", "right", "wrong", "more", "less",
}  # fmt: skip
# Things to do once, not ways to behave ("don't forget the milk again" is a reminder).
_ONE_OFF = {"forget", "remind", "worry", "bother", "mind", "go", "stop", "be", "do", "let"}
# "stop <doing something>": how Scout talks or acts; "stop playing music" is a command.
_STOP_VERBS = {
    "saying": "say", "reading": "read", "telling": "tell", "asking": "ask", "calling": "call",
    "interrupting": "interrupt", "repeating": "repeat", "using": "use", "giving": "give",
    "apologizing": "apologize", "apologising": "apologise", "spelling": "spell",
    "explaining": "explain", "mentioning": "mention", "adding": "add", "offering": "offer",
    "announcing": "announce", "summarizing": "summarize", "summarising": "summarise",
    "thanking": "thank", "confirming": "confirm", "suggesting": "suggest", "talking": "talk",
    "describing": "describe", "listing": "list", "guessing": "guess", "rounding": "round",
}  # fmt: skip
_VAGUE = {"that", "it", "this", "so", "those", "these"}


def _instruction(body: str, standing: bool) -> str | None:
    """A rule from a sentence that tells Scout what to do; `standing` when the words
    already said it's for good ("from now on ..."). None for anything else."""
    body = _LEAD.sub("", _clean(body)).strip()
    if not body or _QUESTION.match(body) or _TRUST.search(body):
        return None
    if m := _ALWAYS.match(body):
        word, nxt = m.group(1).lower(), m.group(2).lower().strip(",.")
        if nxt in _NOT_VERB or (nxt.endswith("ed") and nxt != "need") or nxt in _ONE_OFF - {"be", "do"}:
            return None
        return f"{word.title()} {m.group(2)}{m.group(3)}"
    if m := _NOT_TO.match(body):
        rest = m.group(1)
        return None if rest.split()[0].lower() in _ONE_OFF else f"Don't {rest}"
    if m := _DONT.match(body):
        rest = m.group(1)
        if not (standing or _STANDING.search(rest)):
            return None
        rest = _TRAILING.sub("", rest).strip()
        words = rest.lower().split()
        verb = words[1] if words[:1] == ["ever"] and len(words) > 1 else (words[0] if words else "")
        if not words or verb in _ONE_OFF or set(words[1:]) <= _VAGUE:
            return None
        return f"Don't {rest}"
    if m := _STOP.match(body):
        base = _STOP_VERBS.get(m.group(1).lower())
        if base is None:
            return None
        return f"Don't {base} {m.group(2)}".strip()
    if standing and len(body.split()) >= 2 and body.split()[0].lower() not in _ONE_OFF:
        return body
    return None


def told(text: str) -> str | None:
    """The rule a request asks Scout to keep ("from now on, call me Mike" -> "Call me
    Mike"), normalized; None for a question, a one-off ("don't forget the milk") or an
    approval ("always allow adding reminders", trust.py's)."""
    t = _LEAD.sub("", _clean(text)).strip()
    if not t or _QUESTION.match(t):
        return None
    m = _FUTURE_FIRST.match(t) or _FUTURE_LAST.match(t)
    rule = _instruction(m.group(1), standing=True) if m else _instruction(t, standing=False)
    if rule is None or len(rule.split()) < 3 or len(rule) > MAX_CHARS:
        return None
    return normalized(rule)


def says_rule(question: str) -> bool:
    """Whether a turn's request set a rule (for the told_in link: links.py, index.py)."""
    q = _clean(question)
    return told(q) is not None or (bool(re.search(r"\brules?\b", q, re.I)) and not _QUESTION.match(q))


_REVIEW = re.compile(
    r"^(?:let'?s |can we |could we |i want to |i'd like to )?(?:review|go over|go through|clean up|tidy up)"
    r" (?:my |the |your |all (?:my |the )?)?(?:behavior )?rules$",
    re.I,
)
_LISTING = re.compile(
    r"^(?:what are|list|tell me|read me|what're) (?:my |the |your |all (?:my |the |your )?)?(?:behavior )?rules"
    r"|^what rules (?:do you have|have i (?:set|given you))$",
    re.I,
)


def is_review(text: str) -> bool:
    return bool(_REVIEW.match(_clean(text)))


def is_listing(text: str) -> bool:
    return bool(_LISTING.match(_clean(text)))


# --- upkeep -----------------------------------------------------------------------------

# Words that say how, not what: they don't make a rule relevant or two rules the same.
_HOW = {"always", "never", "don't", "dont", "do", "not", "stop", "read", "tell", "say", "give", "mention",
        "use", "aloud", "out", "loud", "ever", "anymore"}  # fmt: skip


def _topic(text: str) -> set[str]:
    return keywords(text) - _HOW


def _negative(text: str) -> bool:
    return bool(re.match(r"^(?:never|don'?t|do not|stop)\b", text, re.I))


@dataclass
class Review:
    duplicates: list[tuple[Rule, Rule]]  # (the one to keep, one that says the same)
    unused: list[Rule]  # older than UNUSED_DAYS and never came up in that time
    enforced: list[Rule]


def review(rules: Sequence[Rule], talk: Iterable[str], today: dt.date | None = None) -> Review:
    """Upkeep for the rules: which say the same thing (same topic words, same yes or no),
    which never came up in the last UNUSED_DAYS of conversation (`talk`: what was said),
    and which code enforces. Nothing is changed here."""
    today = today or dt.date.today()
    live = [r for r in rules if not r.enforced]
    dupes: list[tuple[Rule, Rule]] = []
    gone: set[str] = set()  # the second of a pair (merged into the first)
    for i, a in enumerate(live):
        if a.id in gone:
            continue
        for b in live[i + 1 :]:
            if b.id in gone or _negative(a.text) != _negative(b.text):
                continue
            ta, tb = _topic(a.text), _topic(b.text)
            if ta and tb and len(ta & tb) / len(ta | tb) >= 0.75:
                dupes.append((a, b))
                gone.add(b.id)
    paired = gone | {a.id for a, _ in dupes}
    said: set[str] = set()
    for t in talk:
        said |= keywords(t)
    cutoff = (today - dt.timedelta(days=UNUSED_DAYS)).isoformat()
    unused = [
        r for r in live
        if r.id not in paired and (r.added or "9999") < cutoff and (r.confirmed or r.added) < cutoff
        and not (_topic(r.text) & said)
    ]  # fmt: skip
    return Review(dupes, unused, [r for r in rules if r.enforced])


# --- what's said and listed -------------------------------------------------------------


def listing_text(b: Behavior | None = None) -> str:
    """The rules for Claude (rules_list): each with where it came from and its ref."""
    b = b or default()
    rules = b.rules()
    if not rules:
        return "No behavior rules yet (memory/behavior.md)."
    out = []
    for r in rules:
        when = f"added {r.added}" if r.added else "added by hand"
        how = f"; enforced in code: {r.enforced}, not in prompts" if r.enforced else ""
        out.append(f"- {r.text} ({r.origin or 'set by hand'}, {when}{how}) (ref {r.id})")
    _, hidden = b.injected()
    tail = (
        f"\n{hidden} rules past the {MAX_INJECTED} used aren't in prompts: offer to merge some."
        if hidden
        else ""
    )
    return "\n".join(out) + tail


def _said(rule: str) -> str:
    s = spoken(rule)
    return s[:1].lower() + s[1:]


def spoken_listing(b: Behavior | None = None, limit: int = 5) -> str:
    """The rules said back ("You have 2 rules: call you Mike. ...")."""
    b = b or default()
    rules = b.rules()
    if not rules:
        return "You haven't set any rules for me yet."
    live = [r for r in rules if not r.enforced]
    enforced = len(rules) - len(live)
    n = len(rules)
    out = f"You have {n} rule{'s' * (n != 1)}"
    if live:
        out += ": " + "; ".join(_said(r.text) for r in live[-limit:][::-1]) + "."
        if len(live) > limit:
            out += f" And {len(live) - limit} more in behavior.md."
    else:
        out += "."
    if enforced:
        out += f" {enforced} {'is' if enforced == 1 else 'are'} kept by my code"
        out += ": " + _said(next(r.text for r in rules if r.enforced)) + "." if enforced == 1 else "."
    return out


def read_back(rule: str) -> str:
    """The question that keeps a rule: "Rule: always call you Mike. Keep it?"."""
    return f"Rule: {_said(rule)}. Keep it?"


# --- scoutctl rules ...  ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="scoutctl rules", description="Scout's behavior rules (memory/behavior.md)"
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="the rules, with where each came from")
    a = sub.add_parser("add", help="keep a rule")
    a.add_argument("rule")
    a.add_argument("--enforced", default="", help="the code that keeps it: listed, not put in prompts")
    a.add_argument(
        "--origin",
        default="",
        help='where it came from (default "from a fix" with --enforced, else "set by Mike")',
    )
    d = sub.add_parser("drop", help="drop rules with every one of these words")
    d.add_argument("about")
    d.add_argument("--yes", action="store_true", help="really drop them (without it, only show which)")
    sub.add_parser("prompt", help="what sessions are given")
    args = p.parse_args(argv)
    b = default()
    if args.cmd == "add":
        origin = args.origin or ("from a fix" if args.enforced else "set by Mike")
        try:
            r, new = b.add(args.rule, origin=origin, enforced=args.enforced)
        except ToolError as exc:
            print(f"not kept: {exc}", file=sys.stderr)
            return 1
        print(("added: " if new else "already there (confirmed): ") + render_line(r) + b.over_cap_note())
        return 0
    if args.cmd == "drop":
        which = [r for r in b.matching(args.about) if not r.enforced]
        if not which:
            print("no rule matches (rules enforced in code aren't dropped here)")
            return 1
        if not args.yes:
            print("would drop (add --yes):\n" + "\n".join(render_line(r) for r in which))
            return 0
        print("dropped:\n" + "\n".join(render_line(r) for r in b.drop(args.about)))
        return 0
    if args.cmd == "prompt":
        print(b.prompt_block() or "(no rules are put in prompts)")
        return 0
    print(listing_text(b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
