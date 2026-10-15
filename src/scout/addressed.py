"""Does speech without the wake word look addressed to Scout? Plain code, no model.

A follow-up after an answer needs no wake word, so the room also hears what people say
to each other then (a call, a meeting). Heard live: "It's not just memory, by the way,
Brian" and "It's asking me my..." were answered as requests. A follow-up has to look
like one: a question, a command, a short fragment that continues the answer ("and
tomorrow?", "the second one"), an answer to a question Scout just asked, or a request
("I meant Saturday"). Statements, talk aimed at someone else ("..., Brian", "You will
want to..."), unfinished speech ("...") and acknowledgements ("Oh, I know") are not.

`reason()` returns why the speech isn't taken as a follow-up, or None when it is."""

from __future__ import annotations

import re

from . import speech

# Words that may come first without changing what the sentence is ("Okay, what about...").
_FILLERS = {
    "um", "uh", "er", "erm", "ah", "oh", "hmm", "so", "okay", "ok", "well", "wait",
    "actually", "alright", "now", "hey", "sorry", "right", "then", "yeah", "no", "just",
    "only", "yes", "yep", "sure",
}  # fmt: skip
# Joining words that make the rest a continuation of the last exchange ("and tomorrow?").
_CONTINUE = {"and", "or", "also", "plus", "but", "instead"}
_QUESTION = {
    "what", "what's", "whats", "when", "when's", "where", "where's", "who", "who's", "whom",
    "whose", "why", "how", "how's", "which", "is", "isn't", "are", "aren't", "was", "were",
    "can", "can't", "could", "would", "will", "won't", "do", "does", "doesn't", "did",
    "didn't", "should", "shall", "may", "might", "have", "has", "hasn't", "any", "anything",
}  # fmt: skip
# Things Scout is asked to do: a sentence that starts with one is a command.
_COMMAND = {
    "tell", "give", "read", "show", "send", "set", "add", "remind", "call", "text", "open",
    "play", "find", "search", "check", "look", "make", "put", "change", "cancel", "delete",
    "remove", "repeat", "say", "start", "schedule", "move", "email", "mail", "reply", "draft",
    "list", "turn", "mute", "unmute", "use", "try", "keep", "save", "remember", "forget",
    "note", "book", "get", "pull", "summarize", "explain", "translate", "spell", "describe",
    "compare", "skip", "go", "continue", "include", "exclude", "mark",
    "archive", "forward", "message", "ping", "post", "write", "update", "rename", "snooze",
    "dismiss", "please", "convert", "count", "share", "link",
    "navigate", "route", "map", "pause", "resume", "replay", "next", "again", "more",
}  # fmt: skip
# The first word of a statement: "I think...", "It's...", "They said...".
_SUBJECT = {
    "i", "i'm", "i've", "i'll", "i'd", "we", "we're", "we've", "we'll", "he", "he's", "she",
    "she's", "they", "they're", "they've", "it", "it's", "its", "that", "that's", "this",
    "there", "there's", "these", "those", "mine", "you", "you're", "you'll", "you've", "you'd", "your", "y'all", "everybody", "everyone",
    "somebody", "someone", "people", "nobody",
}  # fmt: skip
# ...except these, which ask Scout for something ("I meant Saturday", "I'd like Thai").
_REQUEST = re.compile(
    r"^(?:i (?:want|wanted|need|meant|mean|said|asked)|i'd (?:like|prefer|rather)|"
    r"i would (?:like|prefer|rather)|i'm asking|it should be|make it|that should be)\b"
)
# Acknowledgements: nothing to answer.
_ACK = {
    "thanks", "thank you", "thank you so much", "thanks a lot", "got it", "i know",
    "oh i know", "i see", "okay", "ok", "cool", "nice", "great", "perfect", "right",
    "uh huh", "uh-huh", "mm hmm", "yeah", "yep", "yup", "wow", "interesting", "makes sense",
    "fair enough", "sounds good", "good", "awesome", "no problem", "sure", "alright",
    "exactly", "totally", "absolutely", "definitely", "true", "agreed", "same", "of course",
    "right right", "yeah yeah", "okay okay", "i did", "me too", "you too", "bye", "see you",
}  # fmt: skip
# A sentence that opens with one of these explains or sets up; it asks nothing by itself.
_SUBORDINATE = {"because", "since", "although", "though", "unless", "whereas", "cause", "'cause"}
# ...and these open a clause the sentence goes on from ("When you get a chance, read it").
_LEADING_CLAUSE = {"when", "if", "while", "after", "before", "once", "whenever", "because", "since"}
_SENTENCE = re.compile(r"(?<=[.?!])\s+")
# A name said to someone, at the start ("Brian, ...") or the end ("..., Brian."). Whisper
# capitalizes names; ordinary words that may start a sentence before a comma are skipped.
_VOCATIVE_START = re.compile(
    r"^\s*(?:(?:Hey|Hi|Hello|Oh|Okay|So|Thanks|Thank you),?\s+)?([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?\s*,"
)
_VOCATIVE_MID = re.compile(r",\s*([A-Z][a-z]+)\s*,")
_VOCATIVE_END = re.compile(r",\s*([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)?\s*[.!?]*\s*$")
_CONTENT = re.compile(r"[a-z0-9']{4,}")
_STOP_CONTENT = {
    "about", "that", "this", "with", "from", "have", "what", "when", "where", "there",
    "their", "them", "they", "your", "just", "like", "will", "would", "could", "should",
    "it's", "that's", "what's", "it'll", "been", "were", "also", "then", "than", "into",
    "some", "more", "very", "much", "only", "over", "here", "want", "need", "tell",
}  # fmt: skip
# Said to a group, or a question about the listener's own experience ("Can you hear
# me?", "What do you think?", "Did you get my email?"): people, not Scout.
_GROUP = {"everyone", "everybody", "guys", "folks", "y'all", "yall", "team", "you all"}
_PERSONAL = re.compile(
    r"\byou(?:'re)?\s+(?:\w+\s+)?(?:hear|see|think|feel|get|got|want|mean|agree|guys|there|ready|okay|still)\b"
)
MAX_WORDS = 25  # longer than this without the wake word is talk, not a follow-up
FRAGMENT_WORDS = 4  # a fragment this short continues the answer ("for tomorrow")
TOPIC_FRAGMENT_WORDS = 7  # ...and up to this long if it shares a word with the answer


def _words(text: str) -> list[str]:
    return speech.words(text)


def _strip_fillers(ws: list[str]) -> list[str]:
    i = 0
    while i < len(ws) - 1 and ws[i] in _FILLERS:
        i += 1
    return ws[i:]


def _vocative(text: str, wake: set[str]) -> str | None:
    """A name the speech is said to that isn't Scout's ("Good call, Brian")."""
    for pat in (_VOCATIVE_START, _VOCATIVE_MID, _VOCATIVE_END):
        m = pat.search(text)
        if m is None:
            continue
        name = m.group(1).lower()
        if name in wake or name in _FILLERS or name in _QUESTION or name in _COMMAND:
            continue
        if name in _SUBJECT or name in _CONTINUE or " ".join(_words(m.group(0))) in _ACK:
            continue
        return m.group(1)
    return None


def _asked(answer: str) -> bool:
    """Did the answer end by asking something ("Want me to send it?")."""
    return answer.rstrip().endswith("?")


def _topic(ws: list[str], answer: str) -> bool:
    mine = {w for w in ws if _CONTENT.fullmatch(w) and w not in _STOP_CONTENT}
    theirs = {w for w in _words(answer) if _CONTENT.fullmatch(w) and w not in _STOP_CONTENT}
    return bool(mine & theirs)


def _form(sentence: str, answer: str, names: frozenset[str] = frozenset()) -> str | None:
    """What one sentence is: "question", "command", "request", "fragment", "ack", or
    None for a statement. `names`: Scout's, left out ("Thank you, Scott")."""
    ws = _strip_fillers([w for w in _words(sentence) if w not in names])
    if not ws:
        return "ack"
    joined = " ".join(ws)
    if joined in _ACK or " ".join(w for w in _words(sentence) if w not in names) in _ACK:
        return "ack"
    if ws[0] in _CONTINUE and len(ws) > 1:  # "and tomorrow?", "or the other one"
        rest = _form(" ".join(ws[1:]), answer, names)
        return None if rest == "ack" else rest
    if ws[0] in _LEADING_CLAUSE and "," in sentence and not sentence.rstrip().endswith("?"):
        # "When you're searching for it, it's a certain time": the main clause decides.
        return _form(sentence.rsplit(",", 1)[1], answer, names)
    if ws[0] in _SUBORDINATE:
        return None
    if ws[0] in _QUESTION:
        return "question"
    if ws[0] in _SUBJECT and not _REQUEST.match(joined):
        return None  # "It says, how do I enable?" is a statement, even with a "?"
    if sentence.rstrip().endswith("?"):
        return "question"
    if ws[0] in _COMMAND or (ws[0] in ("don't", "do") and len(ws) > 1 and ws[1] in _COMMAND):
        return "command"
    if _REQUEST.match(joined):
        return "request"
    if ws[0] in _SUBJECT:
        return None
    if len(ws) <= FRAGMENT_WORDS or (len(ws) <= TOPIC_FRAGMENT_WORDS and _topic(ws, answer)):
        return "fragment"
    return None


def reason(
    text: str,
    answer: str = "",
    wake_names: tuple[str, ...] | list[str] = ("scout",),
    greeted_names: tuple[str, ...] | list[str] = (),
) -> str | None:
    """Why `text` (heard without the wake word after `answer`) isn't a follow-up, or None.
    Naming Scout anywhere makes it one; a greeted near-miss ("Scott") is not someone else."""
    wake = {n.lower() for n in wake_names}
    ws = _words(text)
    if not ws:
        return "nothing said"
    if wake & set(ws):  # "what about tomorrow, Scout?"
        return None
    stripped = text.rstrip()
    if stripped.endswith(("...", "…")):
        return "unfinished (said to someone else?)"
    names = frozenset(wake | {n.lower() for n in greeted_names})
    if (name := _vocative(text, set(names))) is not None:
        return f"said to {name}"
    if _GROUP & set(ws) or "you all" in " ".join(ws):
        return "said to the group"
    if _PERSONAL.search(" ".join(ws)):
        return "a question for a person (about what they hear, see or think)"
    if len(ws) > MAX_WORDS:
        return f"too long for a follow-up ({len(ws)} words)"
    # An answer to Scout's own question: "yes please", "no, the other one".
    if _asked(answer) and speech.parse_yes_no(text) is not None:
        return None
    forms = [_form(s, answer, names) for s in _SENTENCE.split(stripped) if s.strip()]
    meant = [f for f in forms if f != "ack"]
    if not meant:
        return "an acknowledgement, not a request"
    if meant[0] is None:
        return "a statement, not a question or request"
    return None
