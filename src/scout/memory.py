"""Scout's memory: facts the user tells it ("remember that my dentist is Dr. Lee",
"Pat is my manager"), kept on this Mac in memory/facts.md: one "- fact · date" line
each (at the start of the line; an indented bullet isn't a fact), which the user can edit;
an edit is picked up on the next read, and a change keeps the file's other lines as they are.

Plain code answers "remember...", "what do you know about...", "who is my
dentist" and "forget..." (tier 0, `answer`). Claude sees a memory only when it
shares words with the request, and only as the privacy mode allows (privacy.py);
the room agent can add or drop one with its remember / forget tools.

A fact's record id is memory:<hash of its words> (`fact_id`). Answers from facts carry
those ids (refs.Spoken), and the store links a fact to the journal turn it was told in
(told_in, docs/links.md), so "where did that come from" has an answer."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .mac import ToolError

log = logging.getLogger(__name__)

MAX_FACTS = 500
_CURLY = "\u2019"  # right single quote, as some keyboards type an apostrophe
MAX_CHARS = 300

_STOP = {
    'a', 'an', 'the', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'at', 'for', 'with', 'about', 'from', 'by', 'is', 'are', 'was', 'were', 'be', 'been', 'am', 'do', 'does', 'did', 'what', 'whats', 'who', 'whos', 'where', 'when', 'how', 'which', 'that', 'this', 'these', 'those', 'it', 'its', 'my', 'me', 'mine', 'i', 'im', "i'm", 'you', 'your', 'yours', 'we', 'our', 'they', 'their', 'them', 'he', 'she', 'his', 'her', 'him', 'there', 'here', 'have', 'has', 'had', 'not', 'no', 'yes', 'so', 'just', 'know', 'remember', 'tell', 'say', 'said', 'please', 'can', 'could', 'would', 'will', 'scout', 'anything', 'something', 'everything', 'thing', 'things',
}  # fmt: skip


def keywords(text: str) -> set[str]:
    """Content words, lowercased, with a plural 's' dropped: "Pat's managers" -> {pat, manager}."""
    out = set()
    for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace(_CURLY, "'")):
        w = re.sub(r"'s$|'$", "", w)
        if w in _STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def fact_id(text: str) -> str:
    """A fact's record id in the store: memory:<hash of its words>."""
    return "memory:" + hashlib.blake2b(text.encode(), digest_size=6).hexdigest()


def normalized(text: str) -> str:
    """A fact as it's kept: one line, no full stop at the end."""
    return " ".join(str(text or "").split()).strip().rstrip(".")


FACTS_HEADER = (
    "# What Scout remembers\n\n"
    'One fact per line, starting with "- ". Edit freely; the date after a fact is when it was said.\n\n'
)
_FACT_LINE = re.compile(r"^- (.+?)(?: · (\d{4}-\d\d-\d\d)(?: (\d\d:\d\d))?)?\s*$")


def _parse_line(line: str, default_ts: float) -> dict[str, Any] | None:
    """The fact on a line, if it is one: "- " at the very start."""
    m = _FACT_LINE.match(line)
    if not m or not m.group(1).strip():
        return None
    ts = default_ts
    if m.group(2):
        with contextlib.suppress(ValueError, OverflowError):
            ts = time.mktime(time.strptime(f"{m.group(2)} {m.group(3) or '00:00'}", "%Y-%m-%d %H:%M"))
    return {"text": m.group(1).strip(), "ts": ts}


def _lines(text: str) -> list[str]:
    lines = text.split("\n")  # not splitlines(): U+2028 and the like aren't line breaks here
    return lines[:-1] if lines and lines[-1] == "" else lines


def parse_facts(text: str, default_ts: float = 0.0) -> list[dict[str, Any]]:
    return [f for line in _lines(text) if (f := _parse_line(line, default_ts))]


def _render_line(f: dict[str, Any]) -> str:
    when = f" · {time.strftime('%Y-%m-%d %H:%M', time.localtime(f['ts']))}" if f.get("ts") else ""
    return f"- {f['text']}{when}"


def render_facts(facts: list[dict[str, Any]]) -> str:
    lines = [_render_line(f) for f in facts]
    return FACTS_HEADER + "\n".join(lines) + ("\n" if lines else "")


def _ts(value: Any) -> float:
    """A fact's time from JSON: seconds, or 0 if it isn't a usable time."""
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return ts if 0 <= ts < 1e11 else 0.0  # also NaN and times localtime() can't show


def _from_json(data: Any) -> list[dict[str, Any]]:
    facts = data.get("facts", []) if isinstance(data, dict) else []
    return [
        {"text": str(f["text"]), "ts": _ts(f.get("ts"))}
        for f in facts if isinstance(f, dict) and f.get("text")
    ]  # fmt: skip


class Memory:
    """Facts in a markdown file (facts.md), or JSON for an older path; `legacy`, a JSON
    file from before facts.md, is brought over once."""

    def __init__(self, path: Path, legacy: Path | None = None) -> None:
        self.path = path
        self.facts: list[dict[str, Any]] = []
        self.on_change: Callable[[], None] | None = None  # e.g. re-index (index.py)
        # The memory service runs requests on several threads: one change at a time.
        self._lock = threading.RLock()
        self._mtime = 0.0  # of the file as last read or written; 0 if there was none
        # facts.md as last read or written, each line with its fact (None for other text),
        # so a change rewrites only the lines of facts that came or went.
        self._lines: list[tuple[str, dict[str, Any] | None]] = []
        if legacy is not None and legacy.exists() and not path.exists():
            try:
                self.facts = _from_json(json.loads(legacy.read_text()))
                self._save()
                legacy.rename(legacy.with_suffix(legacy.suffix + ".moved"))
                log.info("moved %d facts to %s", len(self.facts), path)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("facts in %s not moved (%s)", legacy, exc)
        self._load()

    def _load(self) -> None:
        try:
            st = self.path.stat()
            text = self.path.read_text()
            if self.path.suffix == ".md":
                self._lines = [(line, _parse_line(line, st.st_mtime)) for line in _lines(text)]
                self.facts = [f for _, f in self._lines if f is not None]
            else:
                self.facts = _from_json(json.loads(text))
            self._mtime = st.st_mtime
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("memory file %s unreadable (%s); starting empty", self.path, exc)

    def _catch_up(self) -> bool:
        """Re-read the file if it changed since it was last read or written (an edit by
        hand), or forget the facts if it was deleted. Whether anything changed."""
        with self._lock:
            try:
                m = self.path.stat().st_mtime
            except FileNotFoundError:
                if not self._mtime:
                    return False
                self.facts, self._lines, self._mtime = [], [], 0.0
                return True
            except OSError:
                return False
            if m == self._mtime:
                return False
            self._load()
            return True

    def refresh(self) -> None:
        """Re-read the file if it was edited (or deleted) since, by the user, by hand."""
        if self._catch_up() and self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("memory change hook failed")

    def _merged(self) -> str:
        """facts.md with the current facts: the lines of facts that are gone dropped, new
        facts added at the end, every other line as it was."""
        keep = {id(f) for f in self.facts}
        lines = self._lines or [(line, None) for line in _lines(FACTS_HEADER)]
        lines = [(line, f) for line, f in lines if f is None or id(f) in keep]
        placed = {id(f) for _, f in lines if f is not None}
        lines += [(_render_line(f), f) for f in self.facts if id(f) not in placed]
        self._lines = lines
        self.facts = [f for _, f in lines if f is not None]
        return "\n".join(line for line, _ in lines) + "\n"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(
                    self._merged()
                    if self.path.suffix == ".md"
                    else json.dumps({"facts": self.facts}, indent=1)
                )
            Path(tmp).replace(self.path)
            self._mtime = self.path.stat().st_mtime
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("memory change hook failed")

    def as_documents(self) -> list[dict[str, Any]]:
        """The facts as index items (index.Index.sync)."""
        return [
            {"id": fact_id(f["text"]), "title": f["text"], "body": f["text"], "ts": float(f.get("ts") or 0)}
            for f in self.facts
        ]  # fmt: skip

    def add(self, text: str) -> str:
        fact = normalized(text)
        if not fact:
            raise ToolError("nothing to remember")
        if len(fact) > MAX_CHARS:
            raise ToolError(f"that's too long to remember (max {MAX_CHARS} characters)")
        if any(ord(c) < 32 for c in fact):
            raise ToolError("that has control characters")
        # The same fact said again (in other small words) replaces the old copy.
        new = keywords(fact)
        self.refresh()
        with self._lock:
            self._catch_up()  # an edit saved since the refresh above
            self.facts = [
                f for f in self.facts if f["text"].lower() != fact.lower() and keywords(f["text"]) != new
            ]
            self.facts.append({"text": fact, "ts": time.time()})
            if len(self.facts) > MAX_FACTS:  # too many: the oldest go, wherever they are in the file
                oldest = sorted(self.facts, key=lambda f: float(f.get("ts") or 0))[
                    : len(self.facts) - MAX_FACTS
                ]
                gone = {id(f) for f in oldest}
                self.facts = [f for f in self.facts if id(f) not in gone]
            self._save()
        return fact

    def matching(self, about: str) -> list[str]:
        """Facts containing every content word of `about` ("dentist", "Pat")."""
        want = keywords(about)
        if not want:
            return []
        self.refresh()
        return [f["text"] for f in reversed(self.facts) if want <= keywords(f["text"])]

    def forget(self, about: str) -> list[str]:
        with self._lock:
            gone = self.matching(about)  # re-reads an edited file, under the lock
            if gone:
                self.facts = [f for f in self.facts if f["text"] not in gone]
                self._save()
        return gone

    def relevant(self, request: str, limit: int = 5) -> list[str]:
        """Facts sharing a content word with the request, most overlap (then newest) first."""
        want = keywords(request)
        self.refresh()
        scored = [(len(want & keywords(f["text"])), i, f["text"]) for i, f in enumerate(self.facts)]
        hits = sorted((s for s in scored if s[0]), reverse=True)
        return [t for _, _, t in hits[:limit]]

    def newest(self, limit: int = 10) -> list[str]:
        self.refresh()
        return [f["text"] for f in reversed(self.facts[-limit:])]


# --- tier 0: what the user says to it ------------------------------------------------------

_SWAP = {
    "i am": "you are", "i was": "you were", "you are": "I am", "you were": "I was",
    "my": "your", "mine": "yours", "me": "you", "i": "you", "i'm": "you're", "i've": "you've",
    "myself": "yourself", "your": "my", "yours": "mine", "you": "I", "you're": "I'm",
}  # fmt: skip
_SWAP_WORDS = re.compile(
    r"\b(?:" + "|".join(sorted(map(re.escape, _SWAP), key=len, reverse=True)).replace(r"\ ", r"\s+") + r")\b",
    re.I,
)


def spoken(fact: str) -> str:
    """The user's words said back to them: "my dentist is Dr. Lee" -> "your dentist is Dr. Lee"."""
    fact = fact.replace(_CURLY, "'")
    return _SWAP_WORDS.sub(lambda m: _SWAP[" ".join(m.group(0).lower().split())], fact)


def _clean(text: str) -> str:
    t = text.strip().replace(_CURLY, "'")
    t = re.sub(
        r"^(?:(?:hey |ok |okay )?scout[, ]+)?(?:please |can you |could you |would you )*", "", t, flags=re.I
    )
    return t.strip().rstrip(".!?").strip()


_REMEMBER = re.compile(r"^(?:remember|note|make a note|keep in mind)(?: that|:)?\s+(.+)$", re.I)
_RECALL = re.compile(
    r"^(?:what do you (?:know|remember) about|what have i told you about|tell me what you (?:know|remember) about)"
    r"\s+(.+)$",
    re.I,
)
_RECALL_ALL = re.compile(
    r"^(?:what do you (?:know|remember)(?: about me)?|what have i (?:told you|asked you to remember))$", re.I
)
_FORGET = re.compile(r"^forget(?: about| that| what i (?:said|told you) about)?\s+(.+)$", re.I)
# "who's my dentist", "who is Pat", "what's my wifi name": only answered if a memory fits.
_WHO = re.compile(r"^(?:who(?:'s| is| are)\s+|what(?:'s| is| are) my\s+)(.+)$", re.I)
# Not facts to keep: requests that start like one ("remember to call Sam" is a reminder).
_NOT_A_FACT = re.compile(r"^(?:to |me to |when |where |what |how |why |if |the time)", re.I)
_TOO_VAGUE = {"it", "that", "this", "everything", "all", "all of it", "all that"}


def told(text: str) -> str | None:
    """The fact a request tells Scout to keep ("remember that my dentist is Dr. Lee"), as
    answer() would keep it; None when it isn't one ("remember to call Sam")."""
    m = _REMEMBER.match(_clean(text))
    if m is None or _NOT_A_FACT.match(m.group(1)):
        return None
    return normalized(m.group(1))


def answer(text: str, memory: Memory) -> str | None:
    """The spoken answer if this is about Scout's memory, else None. It carries the ids of
    the facts it says (refs.Spoken), so "where did that come from" can be answered."""
    from .refs import Spoken

    def said(words: str, facts: list[str]) -> Spoken:
        return Spoken(words, [fact_id(f) for f in facts])

    t = _clean(text)
    if m := _REMEMBER.match(t):
        fact = m.group(1)
        if _NOT_A_FACT.match(fact):
            return None  # a reminder or a question: Claude
        try:
            kept = memory.add(fact)
        except ToolError as exc:
            return f"Sorry, {exc}."
        return said(f"Okay, I'll remember that {spoken(kept)}.", [kept])
    if _RECALL_ALL.match(t):
        facts = memory.newest(5)
        if not facts:
            return "You haven't asked me to remember anything yet."
        more = f" And {len(memory.facts) - 5} more." if len(memory.facts) > 5 else ""
        return said("You told me " + "; ".join(spoken(f) for f in facts) + "." + more, facts)
    if m := _RECALL.match(t):
        facts = memory.matching(m.group(1))
        if not facts:
            return f"You haven't told me anything about {spoken(m.group(1))}."
        return said("You told me " + "; ".join(spoken(f) for f in facts[:3]) + ".", facts[:3])
    if m := _FORGET.match(t):
        about = m.group(1)
        if about.lower() in _TOO_VAGUE:
            return None  # "forget it", "forget that": not about memory
        gone = memory.forget(about)
        if not gone:
            return f"I don't have anything remembered about {spoken(about)}."
        return f"Okay, I forgot that {spoken(gone[0])}." + (
            f" And {len(gone) - 1} more about it." if len(gone) > 1 else ""
        )
    if m := _WHO.match(t):
        facts = memory.matching(m.group(1))
        if facts:
            return said("You told me " + spoken(facts[0]) + ".", facts[:1])
    return None
