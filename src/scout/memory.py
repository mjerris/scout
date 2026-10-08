"""Scout's memory: facts the user tells it ("remember that my dentist is Dr. Lee",
"Pat is my manager"), kept on this Mac in state/memory.json.

Plain code answers "remember...", "what do you know about...", "who is my
dentist" and "forget..." (tier 0, `answer`). Claude sees a memory only when it
shares words with the request, and only as the privacy mode allows (privacy.py);
the room agent can add or drop one with its remember / forget tools."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
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


class Memory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.facts: list[dict[str, Any]] = []
        try:
            data = json.loads(path.read_text())
            self.facts = [f for f in data.get("facts", []) if isinstance(f, dict) and f.get("text")]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("memory file %s unreadable (%s); starting empty", path, exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"facts": self.facts}, indent=1))
        tmp.replace(self.path)

    def add(self, text: str) -> str:
        fact = " ".join(str(text or "").split()).strip().rstrip(".")
        if not fact:
            raise ToolError("nothing to remember")
        if len(fact) > MAX_CHARS:
            raise ToolError(f"that's too long to remember (max {MAX_CHARS} characters)")
        if any(ord(c) < 32 for c in fact):
            raise ToolError("that has control characters")
        # The same fact said again (in other small words) replaces the old copy.
        new = keywords(fact)
        self.facts = [
            f for f in self.facts if f["text"].lower() != fact.lower() and keywords(f["text"]) != new
        ]
        self.facts.append({"text": fact, "ts": time.time()})
        del self.facts[:-MAX_FACTS]
        self._save()
        return fact

    def matching(self, about: str) -> list[str]:
        """Facts containing every content word of `about` ("dentist", "Pat")."""
        want = keywords(about)
        if not want:
            return []
        return [f["text"] for f in reversed(self.facts) if want <= keywords(f["text"])]

    def forget(self, about: str) -> list[str]:
        gone = self.matching(about)
        if gone:
            self.facts = [f for f in self.facts if f["text"] not in gone]
            self._save()
        return gone

    def relevant(self, request: str, limit: int = 5) -> list[str]:
        """Facts sharing a content word with the request, most overlap (then newest) first."""
        want = keywords(request)
        scored = [(len(want & keywords(f["text"])), i, f["text"]) for i, f in enumerate(self.facts)]
        hits = sorted((s for s in scored if s[0]), reverse=True)
        return [t for _, _, t in hits[:limit]]

    def newest(self, limit: int = 10) -> list[str]:
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


def answer(text: str, memory: Memory) -> str | None:
    """The spoken answer if this is about Scout's memory, else None."""
    t = _clean(text)
    if m := _REMEMBER.match(t):
        fact = m.group(1)
        if _NOT_A_FACT.match(fact):
            return None  # a reminder or a question: Claude
        try:
            return f"Okay, I'll remember that {spoken(memory.add(fact))}."
        except ToolError as exc:
            return f"Sorry, {exc}."
    if _RECALL_ALL.match(t):
        facts = memory.newest(5)
        if not facts:
            return "You haven't asked me to remember anything yet."
        more = f" And {len(memory.facts) - 5} more." if len(memory.facts) > 5 else ""
        return "You told me " + "; ".join(spoken(f) for f in facts) + "." + more
    if m := _RECALL.match(t):
        facts = memory.matching(m.group(1))
        if not facts:
            return f"You haven't told me anything about {spoken(m.group(1))}."
        return "You told me " + "; ".join(spoken(f) for f in facts[:3]) + "."
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
            return "You told me " + spoken(facts[0]) + "."
    return None
