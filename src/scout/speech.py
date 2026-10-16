"""Text helpers: wake-word matching, yes/no parsing, markdown → speakable text."""

from __future__ import annotations

import difflib
import itertools
import re
from datetime import date
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9']+")
# Curly and modifier apostrophes (iOS keyboards, some transcripts): U+2019, U+2018, U+02BC.
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
_LEADERS = {"hey", "hi", "hello", "ok", "okay", "yo", "a", "ay", "hay"}
# Filler that may come before the wake name without counting as words of a
# sentence ("Um, hey Claude", "So anyway, Claude"). At most _MAX_FILLERS of them.
_WAKE_FILLERS = {"uh", "um", "er", "erm", "ah", "oh", "so", "and", "anyway", "well", "alright"}
_MAX_FILLERS = 2
# Greetings that let a "greeted" name wake the assistant ("Hey Scott" is a misheard
# "Hey Scout"; "Scott said..." is about someone else).
_GREETINGS = {"hey", "hay", "hi", "hello", "ok", "okay", "yo", "ay"}

STOP_WORDS = {
    "stop",
    "stomp",
    "stopp",
    "cancel",
    "quiet",
    "silence",
    "shut up",
    "enough",
    "never mind",
    "nevermind",
    "be quiet",
    "hush",
    "shush",
}
RESET_PHRASES = {
    "new conversation",
    "new session",
    "start new conversation",
    "start new session",
    "start over",
    "start fresh",
    "reset",
    "forget everything",
    "clear context",
}


def normalize(text: str) -> str:
    """Lowercase, with curly apostrophes (iOS keyboards, some transcripts) made straight."""
    return text.translate(_APOSTROPHES).lower()


def words(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def strip_wake(
    text: str, names: list[str], max_position: int, greeted: list[str] | tuple[str, ...] = ()
) -> str | None:
    """If text is addressed to the assistant, return the command after the wake
    name (possibly ""). Otherwise None.

    The name has to be said to the assistant, not about it: at the start (after
    "hey", "okay", a filler or two), or set off by a comma ("Oh and Claude, ...",
    "Excuse me, Claude, ..."). "I think Claude is great" and "Did Claude finish
    the build?" are about it, so they don't wake it. `greeted` names (near-misses that
    are also everyday names, like "Scott") count only straight after a greeting."""
    raw = text.strip().translate(_APOSTROPHES)
    tokens = list(re.finditer(r"[A-Za-z0-9']+", raw))
    wanted = {n.lower() for n in names}
    alias = {n.lower() for n in greeted}
    seen = 0  # words before the name that count toward max_position
    fillers = 0
    prev = ""
    for m in tokens:
        w = m.group().lower()
        if w in alias and prev in _GREETINGS:
            return raw[m.end() :].lstrip(" ,.!?:;-")
        if w in wanted:
            before = raw[: m.start()].rstrip()
            after = raw[m.end() :]
            # "I asked Claude, and it said no" is about it; "He Claude, ..." (a
            # misheard "hey") is to it.
            vocative = (
                seen == 0
                or before.endswith((",", ".", "!", "?", ";", ":", "-"))
                or (seen <= 1 and bool(re.match(r"\s*[,.!?;:]", after)))
            )
            if vocative:
                return after.lstrip(" ,.!?:;-")
            return None
        prev = w
        if w in _LEADERS:
            continue
        if w in _WAKE_FILLERS and fillers < _MAX_FILLERS:
            fillers += 1
            continue
        seen += 1
        if seen >= max_position:
            break
    return None


# Words that may surround a stop without changing it ("stop it now please",
# "okay, that's enough", "um, stop").
_STOP_FILLER = {
    "please",
    "now",
    "it",
    "that",
    "that's",
    "thats",
    "ok",
    "okay",
    "claude",
    "just",
    "right",
    "talking",
    "speaking",
    "a",
    "the",
    "um",
    "uh",
    "er",
    "oh",
    "alright",
    "so",
}
_STOP_EXTRA = re.compile(r"\b(?:thank you|thanks|all right)\b")
_SHH = re.compile(r"^sh+$")


def _is_stop_words(w: list[str]) -> bool:
    w = [x for x in w if x not in _STOP_FILLER]
    if not w:
        return False
    i = 0
    while i < len(w):  # one or more stop phrases: "stop, stop", "stop! stop!"
        if i + 1 < len(w) and f"{w[i]} {w[i + 1]}" in STOP_WORDS:
            i += 2
        elif w[i] in STOP_WORDS or _SHH.match(w[i]):
            i += 1
        else:
            return False
    return True


def is_stop(cmd: str) -> bool:
    """True only when the whole utterance is a stop phrase, give or take filler
    ("stop", "stop talking", "that's enough", "um, stop", "stop, stop", "stop,
    thank you"). "Cancel the timer" or "no, cancel it" are requests/answers,
    not stops."""
    return _is_stop_words(words(_STOP_EXTRA.sub(" ", normalize(cmd))))


def is_stop_utterance(
    text: str, names: list[str], max_position: int, greeted: list[str] | tuple[str, ...] = ()
) -> bool:
    """True when the whole utterance is a stop, with or without the wake name
    before or after it: "Stop, Claude.", "Claude, stop, stop.", "Stop. Stop.
    Stop.", "Um, stop." The name may only sit within the first max_position
    words or at the end."""
    w = words(_STOP_EXTRA.sub(" ", normalize(text)))
    wanted = {n.lower() for n in names}
    alias = {n.lower() for n in greeted}
    kept: list[str] = []
    for i, x in enumerate(w):
        if x in _LEADERS:
            continue
        if x in wanted and (i < max_position or i == len(w) - 1):
            continue
        if x in alias and i > 0 and w[i - 1] in _GREETINGS:  # "Hey Scott, stop"
            continue
        kept.append(x)
    return _is_stop_words(kept)


_RESET_FILLER = {
    "please",
    "now",
    "um",
    "uh",
    "ok",
    "okay",
    "alright",
    "so",
    "just",
    "let's",
    "lets",
    "a",
    "the",
    "claude",
}


def is_reset(cmd: str) -> bool:
    """True only for the whole phrase ("new conversation", "start over please",
    "let's start a new conversation"), not "reset my router" or "reset it"."""
    w = [x for x in words(cmd) if x not in _RESET_FILLER]
    return " ".join(w) in RESET_PHRASES


# ---------------------------------------------------------------- yes / no ---
# Only a short, clear answer counts. Anything else is None and the caller asks
# again: an unclear reply must never approve a tool call.
_MAX_ANSWER_WORDS = 6
_ANSWER_FILLER = {
    "um",
    "uh",
    "er",
    "erm",
    "ah",
    "oh",
    "hmm",
    "hm",
    "mm",
    "mmm",
    "well",
    "so",
    "please",
    "thanks",
    "sir",
    "ma'am",
    "just",
    "claude",
    "claud",
    "klaud",
    "hey",
}
# "mm-hmm" and "uh-huh" are yes; mapped before the filler is dropped.
_YES_SOUNDS = re.compile(r"\b(?:m+-?hm+|mm+ hm+|uh-?huh|uh huh|mhm)\b")
# Agreeing phrases that contain a "no" word: "sure, no problem", "why not".
_AGREE = re.compile(
    r"\b(?:(?:i )?don't see why not|why not|no problem|not a problem|no prob|no worries|no doubt|"
    r"(?:i )?don't mind|no go ahead|no go for it)\b"
)
# Neither yes nor no on their own: "yes, don't ask again" is a yes because of the "yes".
_NEUTRAL = re.compile(
    r"\b(?:(?:you )?(?:don't|do not|never) (?:need to |have to |bother )?ask(?:ing)?(?: me)?(?: again| anymore)?|"
    r"(?:you )?(?:don't|do not) (?:need|have) to ask(?: me)?(?: again| anymore)?|no need to ask(?: me)?(?: again)?|"
    r"stop asking(?: me)?(?: again)?)\b"
)
_UNSURE = re.compile(
    r"\b(?:not sure|unsure|maybe|perhaps|possibly|probably|i don't know|don't know|dunno|no idea|"
    r"let me (?:think|see|check|look)|i guess|i think so|i suppose|kind of|sort of|"
    r"if|unless|assuming|provided|actually|although|though|except|whatever you think)\b"
)
_QUESTION = re.compile(r"\b(?:what|what's|whats|which|who|whose|where|when|how|why|huh|eh|pardon|repeat)\b")
_PAUSE = re.compile(
    r"\b(?:hold on|hang on|hold up|one (?:sec|second|moment|minute)|(?:a|just a) (?:sec|second|moment|minute|bit)|"
    r"give me|gimme)\b"
)
_WEEKDAY_WORDS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_LATER = re.compile(
    r"\b(?:later|in a (?:minute|bit|moment|sec|second|while)|tomorrow|tonight|after|before|noon|"
    r"this (?:morning|afternoon|evening|weekend)|next (?:week|month|time)|" + _WEEKDAY_WORDS + r"|"
    r"at (?:\d|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)|\d{1,2} ?(?:am|pm|a m|p m))\b"
)
# A yes that asks for something different ("yes, but change the subject first",
# "fine, but make it shorter", "correct the typo"): unclear, so the caller asks again.
_CHANGE = re.compile(
    r"\b(?:but \w|instead|first|only|change|changed|fix|edit|update|rewrite|reword|shorter|longer|"
    r"make (?:it|that|this)|correct (?:the|it|that|this|my)|go back)\b"
)
_NO = re.compile(
    r"\b(?:no|nope|nah|nay|don't|dont|do not|deny|denied|negative|stop|cancel|skip|never|never mind|"
    r"wait|abort|reject|rejected|decline|declined|refuse|hold off|go away|"
    r"not|isn't|aren't|won't|wouldn't|shouldn't|can't|cannot|doesn't|didn't|mustn't)\b"
)
_YES = re.compile(
    r"\b(?:yes|yeah|yea|yep|yup|ya|yah|sure|ok|okay|alright|all right|approve|approved|allow|allowed|"
    r"affirmative|go ahead|go|go for it|do it|do that|do|run it|proceed|carry on|correct|right|fine|"
    r"absolutely|definitely|certainly|of course|sounds good|good to go|perfect|you bet|agreed|agree|"
    r"agreeidiom)\b"
)


@dataclass
class _Reply:
    text: str  # normalized words joined by spaces, idioms replaced
    question: bool  # a "?" is left that isn't part of an agreeing idiom ("why not?")


def _reply(text: str) -> _Reply:
    t = _YES_SOUNDS.sub(" yes ", normalize(text))
    t = re.sub(r"\bthank you\b", " ", t)
    question = "?" in re.sub(r"why not\s*\?", " ", t)
    t = re.sub(r"^\W*(?:okay|ok|alright|all right|right),? so\b", " ", t)  # "okay so ..." leads in
    w = " ".join(x for x in words(t) if x not in _ANSWER_FILLER)
    w = _AGREE.sub(" agreeidiom ", w)
    w = _NEUTRAL.sub(" ", w)
    return _Reply(re.sub(r"\s+", " ", w).strip(), question)


def _decide(r: _Reply) -> bool | None:
    w = r.text
    if not w:
        return None
    if _UNSURE.search(w) or _QUESTION.search(w):
        return None
    if _NO.search(w):
        return False
    if _LATER.search(w):
        return False  # "do it later", "sure, in a minute": not now
    if _CHANGE.search(w):
        return None
    if _PAUSE.search(w):
        return None
    if len(_YES.sub("y", w).split()) > _MAX_ANSWER_WORDS:  # "go ahead" counts as one
        return None
    if _YES.search(w):
        return None if r.question else True  # "yes?" / "okay?" is asking back
    return None


_LATER_ANSWER = re.compile(
    r"\b(?:later|not now|not yet|another time|next time|some other time|(?:skip|leave) (?:it|that|this) for now|"
    r"ask me (?:again )?(?:later|another time|tomorrow)|in a (?:bit|while|minute))\b"
)


def says_later(text: str) -> bool:
    """A "later" for a question that takes one (setup: "later", "not now", "ask me
    tomorrow"); a clear yes or no with it ("no, not now") stays that answer's."""
    w = " ".join(words(normalize(text)))
    if not _LATER_ANSWER.search(w):
        return False
    rest = _LATER_ANSWER.sub(" ", w)
    return not re.search(r"\b(?:no|nope|never|yes|yeah|sure)\b", rest)


def parse_yes_no(text: str) -> bool | None:
    """True for a clear yes, False for a clear no, None when unclear (the caller
    asks again). Only short replies count; a question, a pause ("okay, hold
    on"), a condition ("yes if ...") or doubt is never a yes."""
    return _decide(_reply(text))


# "Yes, but always ask" means keep asking: a one-time yes.
_KEEP_ASKING = re.compile(
    r"\b(?:(?:i |you )?always (?:want to be asked|ask|asks|warn|check|confirm|tell|prompt|"
    r"double check|run (?:it|that|this|things) by|let me know|notify|remind)"
    r"(?: (?:me|with me|first|before|it|that))*|ask(?: me)? always|not always)\b"
)
# Explicit permanent approval: "yes, always", "yes, don't ask me again".
_FOREVER = re.compile(
    r"\b(?:always|from now on|(?:don't|do not|never) ask(?: me)? (?:again|anymore)|stop asking(?: me)?|"
    r"no need to ask(?: me)? again|(?:you )?(?:don't|do not) (?:need|have) to ask(?: me)? again)\b"
)


def parse_answer(text: str) -> bool | str | None:
    """Like parse_yes_no, plus "always" for an explicit permanent approval
    ("yes, always", "always allow that", "yes, don't ask me again", "yes, from
    now on"). "Always" never turns an unclear or negative answer into an
    approval, and "always ask/warn/check me", "ask me always" or "not always"
    mean keep asking: a one-time yes when the rest is a clear yes."""
    question = _reply(text).question
    w = _KEEP_ASKING.sub(" ", " ".join(words(_YES_SOUNDS.sub(" yes ", normalize(text)))))
    if not _FOREVER.search(w):
        return _decide(_Reply(_reply(w).text, question))
    rest = _reply(_FOREVER.sub(" ", w)).text
    if not rest:
        # A bare "always" ("Always.", "Hmm, always"): yes, unless it was asked back.
        return "always" if re.search(r"\balways\b", w) and not question else None
    answer = _decide(_Reply(rest, question))
    return "always" if answer is True else answer


# ------------------------------------------------------------- read-backs ---
READ_BACK_WORDS = 15  # a message up to this long is read back whole
_FIRST_SENTENCE = re.compile(r"(.+?[.!?])(?=\s|$)")


def about_words(n: int) -> str:
    """A word count as it's said: "about 20", "about 350", "about 2,300"."""
    step = 10 if n < 60 else 50 if n < 1000 else 100
    return f"about {max(step, round(n / step) * step):,}"


def read_back(text: str, words: int = READ_BACK_WORDS) -> str:
    """A message to send, said back short (the whole text is on the user's screen): all
    of it when it's short, else how it starts (its first sentence when that's short too,
    else its first `words` words) and roughly how long it is. Never the whole of a long
    body: a 2,000-character Slack message read back went on long enough that the yes
    said over it was lost."""
    flat = " ".join(to_speech(text).split())
    if not flat:
        return "It's empty."
    w = flat.split()
    if len(w) <= words:
        return f"It says: {flat}" + ("" if flat[-1] in ".!?" else ".")
    m = _FIRST_SENTENCE.match(flat)
    first = (
        m.group(1) if m and len(m.group(1).split()) <= words else " ".join(w[:words]).rstrip(",;:") + "..."
    )
    return f"It starts: {first} It's {about_words(len(w))} words."


# ------------------------------------------------------------------ "wait" ---
_SPAN = r"(?:sec|second|minute|moment|bit)"
_WAIT_CORE = (
    rf"(?:just\s+)?(?:hang on|hold on|hold up|wait)(?:\s+(?:a|one|just a)\s+{_SPAN})?"
    rf"|(?:one|just a)\s+{_SPAN}"
)
_WAIT_TAIL = r"(?:[\s,]+(?:please|thanks|thank you))?[\s,.!]*$"
# "hang on", "wait", "one second" only on their own, after punctuation or a
# filler/"and", so "tell Bob to hang on", "set a timer for one second" and
# "play the song Hold On" stay ordinary requests.
_WAIT = re.compile(
    rf"(?:^|[,.;:!?-]\s*|\b(?:and|so|okay|ok|uh|um|oh|well|now|but)\s+)"
    rf"(?P<w>(?:{_WAIT_CORE})(?:[\s,.!-]+(?:{_WAIT_CORE}))*){_WAIT_TAIL}",
    re.I,
)
# "give me a sec" is unambiguous anywhere at the end.
_GIVE_ME = re.compile(rf"\b(?P<w>(?:give me|gimme)\s+(?:a|one)\s+{_SPAN}){_WAIT_TAIL}", re.I)


def split_wait(text: str) -> tuple[str, bool]:
    """("rest", True) when the user ends with "hang on", "wait", "give me a sec"..."""
    t = text.translate(_APOSTROPHES)
    if t.rstrip().endswith("?"):  # "how long should I hang on?" asks, it doesn't pause
        return text, False
    m = _WAIT.search(t) or _GIVE_ME.search(t)
    if not m:
        return text, False
    return text[: m.start("w")].strip(" ,.;:-"), True


# --------------------------------------------------------------- to_speech ---
_CODE_MARK = "\x00"


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]  # fmt: skip
_MONTH_RE = r"(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_DATES = re.compile(
    r"(?P<on>\bon )?(?:"
    r"\b(?P<m1>\d{1,2})/(?P<d1>\d{1,2})/(?P<y1>\d{4}|\d{2})\b|"  # 09/30/2026 (US order)
    r"\b(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?![\d:T])|"  # 2026-09-30
    r"\b" + _MONTH_RE + r" (?P<d3>\d{1,2})(?:st|nd|rd|th)?, (?P<y3>\d{4})\b"  # October 8, 2026
    r")",
    re.I,
)


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def speak_date(day: date, today: date, on: bool = False) -> str:
    """A date the way people say it: 'today', 'on Tuesday', 'last Friday', 'September 30th'."""
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta < 7:
        return ("on " if on else "") + day.strftime("%A")
    if -7 < delta < -1:
        return ("on " if on else "") + "last " + day.strftime("%A")
    words = f"{_MONTHS[day.month - 1]} {_ordinal(day.day)}"
    if day.year != today.year:
        words += f", {day.year}"
    return ("on " if on else "") + words


def speak_dates(text: str, today: date | None = None) -> str:
    """Numeric and long-form dates in text -> spoken words, relative when close."""
    today = today or date.today()

    def one(m: re.Match[str]) -> str:
        try:
            if m["m1"]:
                y = int(m["y1"]) + (2000 if len(m["y1"]) == 2 else 0)
                day = date(y, int(m["m1"]), int(m["d1"]))
            elif m["y2"]:
                day = date(int(m["y2"]), int(m["m2"]), int(m["d2"]))
            else:
                month = next(
                    i for i, name in enumerate(_MONTHS) if name.lower().startswith(m["mon"].lower()[:3])
                )
                day = date(int(m["y3"]), month + 1, int(m["d3"]))
        except (ValueError, StopIteration):
            return m.group(0)
        return speak_date(day, today, on=bool(m["on"]))

    return _DATES.sub(one, text)


def to_speech(md: str) -> str:
    """Flatten markdown into something pleasant to hear."""
    s = re.sub(r"```.*?(?:```|\Z)", f"\n{_CODE_MARK}\n", md, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<(https?://[^>\s]+)>", r"\1", s)
    s = re.sub(r"https?://[^\s<>]*[^\s<>.,;:!?)\]'\"]", "a link", s)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)  # tables
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*(\d+)[.)]\s+", r"\1. ", s, flags=re.M)
    # Emphasis only when the markers sit outside a word: my_var, a_b_c, 2 * 3
    # and file_name.py keep their characters.
    s = re.sub(r"(?<![\w*])(\*\*|\*)(?=[^\s*])(.+?)(?<=[^\s*])\1(?![\w*])", r"\2", s)
    s = re.sub(r"(?<!\w)(__|_)(?=[^\s_])(.+?)(?<=[^\s_])\1(?!\w)", r"\2", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"(?<![\w*])\*{1,2}(?=[A-Za-z_])", "", s)  # *args, **kwargs
    s = re.sub(r"\s*(?:→|⇒|->)\s*", " to ", s)
    s = re.sub(r"[☀-➿\U0001F000-\U0001FAFF]", "", s)  # emoji
    lines = [x.strip() for x in s.split("\n")]
    lines = [x for x in lines if x]
    if len(lines) > 1:
        # Line breaks and list items are pauses when read out.
        lines = [x if x == _CODE_MARK or x[-1] in ".!?:;," else x + "." for x in lines]
    s = " ".join(lines).replace(_CODE_MARK, " (code omitted) ")
    s = re.sub(r"\.\s*\.", ".", s)
    s = re.sub(r"\s+([.,!?])", r"\1", s)
    return speak_dates(re.sub(r"\s{2,}", " ", s).strip())


# --------------------------------------------------------- strip_own_speech ---
_SOUNDS_ALIKE = 0.6


def _alike(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b), autojunk=False).ratio()


_SOUND_MAP = (
    ("ph", "f"),
    ("th", "t"),
    ("ck", "k"),
    ("c", "k"),
    ("q", "k"),
    ("x", "ks"),
    ("z", "s"),
    ("y", "i"),
    ("w", "u"),
)


def _sound(ws: list[str]) -> str:
    """A rough sound-alike key: "the mic" and "mike" both become "...mik"."""
    s = re.sub(r"[^a-z0-9]", "", "".join(ws))
    for a, b in _SOUND_MAP:
        s = s.replace(a, b)
    s = re.sub(r"([a-z])\1+", r"\1", s)
    return re.sub(r"(?<=[^aeiou])e$", "", s)


def _sounds_alike(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    return max(_alike(a, b), difflib.SequenceMatcher(None, _sound(a), _sound(b), autojunk=False).ratio())


def _ours(h: list[str], sp: list[str]) -> list[bool]:
    """For each heard word, whether it is (probably) our own speech."""
    ours = [False] * len(h)
    blocks = [b for b in difflib.SequenceMatcher(None, sp, h, autojunk=False).get_matching_blocks() if b.size]
    kept: list[difflib.Match] = []
    exact: set[int] = set()  # heard words that matched ours exactly but were judged the user's
    for b in blocks:
        if not kept:
            # A long run of our words, or our opening words: one or two common
            # words in the middle ("yes" in "say yes or no") is a coincidence.
            ok = b.size >= 3 or (b.a == 0 and (b.size >= 2 or b.b == 0 or len(sp) == 1))
        else:
            prev = kept[-1]
            gap_h = h[prev.b + prev.size : b.b]
            gap_s = sp[prev.a + prev.size : b.a]
            # Our words in between came back misheard: we're still talking.
            misheard = bool(gap_h and gap_s) and _sounds_alike(gap_h, gap_s) >= _SOUNDS_ALIKE
            # A short match after the user cut in only counts when it carries
            # straight on from what we were saying and ends the clip ("do you
            # want me to yes run it"). Otherwise it's the user's own words
            # ("I found three items, open the first one").
            ok = b.size >= 3 or misheard or (not gap_s and b.b + b.size == len(h))
            # We can't have said many words in no time: a match that skips far
            # ahead in our speech is a later sentence of ours that happens to
            # share the user's words ("what about the ...").
            ok = ok and len(gap_s) <= len(gap_h) + 2
        if ok:
            kept.append(b)
        else:
            exact.update(range(b.b, b.b + b.size))
    if not kept:
        return ours
    for b in kept:
        for i in range(b.b, b.b + b.size):
            ours[i] = True
    # Between two of our runs: misheard words of ours if they sound like what we
    # said there ("the mic" for "mike").
    for prev, nxt in itertools.pairwise(kept):
        span = range(prev.b + prev.size, nxt.b)
        said_there = sp[prev.a + prev.size : nxt.a]
        heard_there = [h[i] for i in span]
        if (
            said_there
            and not exact.intersection(span)
            and _sounds_alike(heard_there, said_there) >= _SOUNDS_ALIKE
        ):
            for i in span:
                ours[i] = True
    # Before the first run: the clip may begin on the end of what we said before
    # it. Misheard words never match exactly, so the user's exact words stop the search.
    first = kept[0]
    head, said = h[: first.b], sp[: first.a]
    if head and said:
        limit = next((first.b - 1 - i for i in range(first.b - 1, -1, -1) if i in exact), len(head))
        for k in range(limit, 0, -1):
            tail_of_said = [said[-n:] for n in range(max(1, k - 1), min(len(said), k + 1) + 1)]
            if max(_alike(head[-k:], x) for x in tail_of_said) >= _SOUNDS_ALIKE:
                for i in range(first.b - k, first.b):
                    ours[i] = True
                break
    # After the last run: what we said next may have come back misheard ("Claude
    # Max account" -> "call my count"). Only the words right after the match can
    # overlap the clip, so later sentences of ours are never compared.
    last = kept[-1]
    rest, tail = h[last.b + last.size :], sp[last.a + last.size :]
    if rest and tail:
        start = last.b + last.size
        limit = next((i - start for i in range(start, len(h)) if i in exact), len(rest))
        for k in range(limit, 0, -1):
            windows = [tail[i : i + n] for i in range(min(3, len(tail))) for n in range(max(1, k - 1), k + 2)]
            if max(_alike(rest[:k], x) for x in windows if x) >= _SOUNDS_ALIKE:
                for i in range(last.b + last.size, last.b + last.size + k):
                    ours[i] = True
                break
    return ours


_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}


def _numbers_as_digits(tokens: list[str]) -> tuple[list[str], list[list[int]]]:
    """Collapse spelled-out numbers into digits, the way Whisper writes them, so
    our "about a hundred fixes" matches the mic's "about 100 fixes". Returns the
    new tokens and, for each, the indices of the original tokens it came from."""
    out: list[str] = []
    src: list[list[int]] = []
    i = 0
    while i < len(tokens):
        j, total, current, used = i, 0, 0, False
        if tokens[j] == "a" and j + 1 < len(tokens) and tokens[j + 1] in _SCALES:
            current, j, used = 1, j + 1, True  # "a hundred"
        while j < len(tokens):
            t = tokens[j]
            if t in _UNITS:
                current += _UNITS[t]
            elif t in _SCALES and (used or current):
                current = max(current, 1) * _SCALES[t]
                if _SCALES[t] >= 1000:
                    total, current = total + current, 0
            elif t == "and" and used and j + 1 < len(tokens) and tokens[j + 1] in _UNITS:
                pass  # "two hundred and five"
            else:
                break
            used = True
            j += 1
        if used and j > i:
            out.append(str(total + current))
            src.append(list(range(i, j)))
            i = j
        else:
            out.append(tokens[i])
            src.append([i])
            i += 1
    return out, src


def _strip(heard: str, spoken: str) -> tuple[str, int]:
    h_raw, sp_raw = words(heard), words(spoken)
    if not h_raw or not sp_raw:
        return heard, len(h_raw)
    h, h_src = _numbers_as_digits(h_raw)
    sp, _ = _numbers_as_digits(sp_raw)
    ours = _ours(h, sp)
    if not any(ours):
        return heard, len(h_raw)
    keep = sorted(k for tok_src, o in zip(h_src, ours, strict=True) if not o for k in tok_src)
    left = [h_raw[k] for k in keep]
    return " ".join(left), len(left)


def mostly_ours(heard: str, spoken: str, spoken_alt: str = "", share: float = 0.75) -> bool:
    """Is a clip (nearly) all our own speech? For deciding a barge-in: our voice leaking
    past the echo canceller comes back as a fragment of what we just said ("It's 58"),
    which strip_own_speech rightly keeps when the user might have said it as a reply."""
    h, _ = _numbers_as_digits(words(heard))
    if not h:
        return False
    best = 0
    for said in {spoken, spoken_alt} - {""}:
        sp, _ = _numbers_as_digits(words(said))
        m = difflib.SequenceMatcher(None, sp, h, autojunk=False)
        best = max(best, sum(b.size for b in m.get_matching_blocks()))
    return best >= share * len(h)


def strip_own_speech(heard: str, spoken: str, spoken_alt: str = "") -> str:
    """Remove the assistant's own words (picked up by the mic) from a transcript,
    returning whatever the user said before, between and after them.

    spoken is only the speech that could have overlapped the clip. spoken_alt
    is the same speech as it was pronounced ("config dot pie" for config.py),
    when that differs; whichever explains more of the clip wins."""
    best = _strip(heard, spoken)
    if spoken_alt and spoken_alt != spoken:
        alt = _strip(heard, spoken_alt)
        if alt[1] < best[1]:
            best = alt
    return best[0]


# --------------------------------------------------------------- identifiers ---
# Ticket, issue, PR and order numbers are read digit by digit ("ticket 6 1 7 2 3"), not
# as an amount ("sixty-one thousand ..."). After one of these words (and an optional
# "number", "no." or "#"), a number of two or more digits; after the ambiguous ones
# ("in case 120 people come", "order 12 pizzas") four or more, unless "number" or "#"
# says it's an identifier.
_ID_WORDS = (
    r"tickets?|issues?|prs?|pull\s+requests?|invoices?|confirmations?(?:\s+(?:code|number))?|"
    r"tracking(?:\s+number)?|bookings?|reservations?|claims?|policy|policies|incidents?|bugs?|"
    r"refs?|cases?\s+number|order\s+number|account\s+number|ids?"
)
_ID_LOOSE = r"cases?|orders?|accounts?|references?|requests?|builds?|jobs?|items?|records?"
_ID_LEAD = r"(?:[ \t]*(?:number|num\.?|no\.|#|:))?[ \t]*#?"
_ID_NUM = r"\d{1,3}(?:,\d{3})+(?![\d,]*\.\d)|\d+"
_ID_END = r"(?![\w.,:/-]?\d)(?!\w)"
_AFTER_WORD = re.compile(
    rf"\b(?P<w>{_ID_WORDS})(?P<lead>{_ID_LEAD})(?P<n>{_ID_NUM}){_ID_END}"
    rf"|\b(?P<lw>{_ID_LOOSE})(?P<llead>{_ID_LEAD})(?P<ln>{_ID_NUM}){_ID_END}",
    re.I,
)
# "#117", "owner/repo#123": "number 1 1 7".
_HASH_NUM = re.compile(r"(?<![\w&])#(\d{2,})" + _ID_END)
_REPO_HASH = re.compile(r"(?<=[\w.-])#(\d+)" + _ID_END)
# A bare integer of five or more digits is an identifier too (a zip code, a ticket said
# without its word), unless it's money, a measurement or a count of something.
_BARE = re.compile(r"(?<![\w$€£¥.,:/#+-])(\d{5,})" + _ID_END)
_MONEY_AFTER = re.compile(r"\s*(?:dollars?|bucks|usd|eur|euros?|pounds?|gbp|cents?|yen)\b", re.I)
_UNIT_AFTER = re.compile(
    r"\s*(?:%|percent\b|k\b|x\b|"
    r"(?:miles?|mi|km|kilomet(?:er|re)s?|met(?:er|re)s?|m|feet|foot|ft|yards?|yds?|inch(?:es)?|"
    r"mph|kph|km/h|kg|kilos?|kilograms?|grams?|g|lbs?|tons?|tonnes?|gallons?|gal|lit(?:er|re)s?|"
    r"ml|calories|cals?|kcal|steps|people|persons|users|customers|members|views|downloads|visitors|"
    r"subscribers|followers|rows|records|lines|bytes|kb|mb|gb|tb|words|times|hours?|hrs?|minutes?|"
    r"mins?|seconds?|secs?|days?|weeks?|months?|years?|acres?|sq|square|cubic|degrees?|watts?|w|"
    r"kwh|wh|volts?|v|amps?|rpm|hz|khz|mhz|ghz|pixels?|px|units?|pieces?|copies|items|messages|"
    r"emails|requests|tokens|votes|points)\b)",
    re.I,
)
_QUANTITY_BEFORE = re.compile(
    r"(?:\b(?:about|around|over|under|nearly|roughly|almost|approximately|than|some|total(?:ing)?|"
    r"up\s+to|at\s+least|at\s+most)|[~≈<>])\s*$",
    re.I,
)


def _digits(n: str) -> str:
    return " ".join(n.replace(",", ""))


def speak_ids(text: str) -> str:
    """Ticket, issue, PR, order and similar numbers, and long bare integers, said digit
    by digit: "ticket 61723" -> "ticket 6 1 7 2 3", "#117" -> "number 1 1 7". Money
    ("$61,723"), quantities ("61,000", "12000 steps"), years, times and short numbers
    ("PR 6", "Highway 61") are left for the voice to say as numbers."""

    def after_word(m: re.Match[str]) -> str:
        if m["w"] is not None:
            word, lead, n = m["w"], m["lead"], m["n"]
            if len(n.replace(",", "")) < 2:
                return m.group(0)
        else:
            word, lead, n = m["lw"], m["llead"], m["ln"]
            plain = len(n.replace(",", ""))
            said = re.search(r"number|num|no\.|#", lead, re.I)
            if plain < 2 or (plain < 4 and not said) or _UNIT_AFTER.match(text, m.end()):
                return m.group(0)
        lead = lead.replace("#", "").strip()
        sep = lead if lead == ":" else f" {lead}" if lead else ""
        return f"{word}{sep} {_digits(n)}"

    def bare(m: re.Match[str]) -> str:
        if _MONEY_AFTER.match(text, m.end()) or _UNIT_AFTER.match(text, m.end()):
            return m.group(0)
        if _QUANTITY_BEFORE.search(text, 0, m.start()):
            return m.group(0)
        return _digits(m.group(1))

    text = _AFTER_WORD.sub(after_word, text)
    text = _REPO_HASH.sub(lambda m: " number " + _digits(m.group(1)), text)
    text = _HASH_NUM.sub(lambda m: "number " + _digits(m.group(1)), text)
    return _BARE.sub(bare, text)
