"""Text helpers: wake-word matching, yes/no parsing, markdown → speakable text."""

from __future__ import annotations

import difflib
import re

_WORD = re.compile(r"[a-z']+")
_LEADERS = {"hey", "hi", "hello", "ok", "okay", "yo", "a", "ay", "hay"}

STOP_WORDS = {"stop", "stomp", "stopp", "cancel", "quiet", "shut up", "enough", "never mind", "nevermind", "be quiet", "hush"}
RESET_PHRASES = {"new conversation", "start over", "reset", "new session", "forget everything", "clear context"}
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "approve", "approved", "allow", "affirmative",
        "go ahead", "do it", "proceed", "please do", "go for it", "correct", "fine"}
_NO = {"no", "nope", "nah", "don't", "dont", "deny", "denied", "negative", "stop", "cancel", "skip",
       "do not", "never mind", "wait"}


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def strip_wake(text: str, names: list[str], max_position: int) -> str | None:
    """If text is addressed to the assistant, return the command after the wake
    name (possibly ""). Otherwise None."""
    raw = text.strip()
    tokens = list(re.finditer(r"[A-Za-z']+", raw))
    seen = 0
    for m in tokens:
        w = m.group().lower()
        if w in names:
            rest = raw[m.end():].lstrip(" ,.!?:;-")
            return rest
        if w not in _LEADERS:
            seen += 1
        if seen >= max_position:
            break
    return None


_STOP_FILLER = {"please", "now", "it", "that", "that's", "thats", "ok", "okay", "claude", "just",
                "right", "talking", "a", "the"}


def is_stop(cmd: str) -> bool:
    """True only when the whole utterance is a stop phrase, give or take filler
    ("stop", "stop talking", "that's enough", "never mind please"). "Cancel the
    timer" or "no, cancel it" are requests/answers, not stops."""
    w = [x for x in words(cmd) if x not in _STOP_FILLER]
    return bool(w) and " ".join(w) in STOP_WORDS


def is_reset(cmd: str) -> bool:
    w = " ".join(words(cmd))
    return any(p in w for p in RESET_PHRASES) and len(w.split()) <= 5


def parse_yes_no(text: str) -> bool | None:
    w = " ".join(words(text))
    padded = f" {w} "
    if any(f" {n} " in padded for n in _NO):
        return False
    if any(f" {y} " in padded for y in _YES):
        return True
    return None


_WAIT = re.compile(
    r"[\s,.;:!-]*\b(?:hang on|hold on|wait(?: a (?:sec|second|minute|moment))?|"
    r"give me a (?:sec|second|minute|moment)|one (?:sec|second|moment)|just a (?:sec|second|moment))"
    r"[\s,.!?]*$", re.I)


def split_wait(text: str) -> tuple[str, bool]:
    """("rest", True) when the user ends with "hang on", "wait", "give me a sec"..."""
    m = _WAIT.search(text)
    if not m:
        return text, False
    return text[:m.start()].strip(" ,.;:-"), True


def parse_answer(text: str) -> bool | str | None:
    """Like parse_yes_no, plus "always" ("yes always", "always allow that")."""
    answer = parse_yes_no(text)
    if answer is not False and "always" in words(text):
        return "always"
    return answer


def to_speech(md: str) -> str:
    """Flatten markdown into something pleasant to hear."""
    s = re.sub(r"```.*?```", " (code omitted) ", md, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"https?://\S+", "a link", s)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)  # tables
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*(\d+)[.)]\s+", r"\1. ", s, flags=re.M)
    s = re.sub(r"(\*\*|__|\*|_|~~)(.+?)\1", r"\2", s)
    s = re.sub(r"[☀-➿\U0001F000-\U0001FAFF]", "", s)  # emoji
    s = re.sub(r"\n{2,}", ". ", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"\.\s*\.", ".", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def strip_own_speech(heard: str, spoken: str) -> str:
    """Remove the assistant's own words (picked up by the mic) from a transcript,
    returning whatever the user said after them."""
    h, sp = words(heard), words(spoken)
    if not h or not sp:
        return heard
    blocks = [b for b in difflib.SequenceMatcher(None, sp, h, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return heard
    end = blocks[-1].b + blocks[-1].size
    # Whatever we said after the last exact match may have come back misheard
    # ("Claude Max account" -> "call my count"); drop it if it sounds alike.
    tail = sp[blocks[-1].a + blocks[-1].size:]
    rest = " ".join(h[end:])
    if tail and rest:
        # Compare against each suffix of what we said (the start may have matched).
        best = max(difflib.SequenceMatcher(None, rest, " ".join(tail[i:])).ratio()
                   for i in range(len(tail)))
        if best >= 0.6:
            return ""
    return rest
