"""Cross-references between records (docs/links.md, "Cross-references"): plain-code rules
for what a Slack message, an email, a meeting or a note points at.

Two rels, both read either way (index.Index.links):

- `mentions`: the text names the other record. A pasted link (a Granola note's page, a
  Slack permalink, Mail's message:// link), a GitHub issue, pull request or commit (its
  URL, "owner/repo#123", or "#123" when the same text names exactly one repo), or a
  meeting named by its title near its date ("per the vendor pricing review", "in
  today's standup").
- `discusses`: matched by meaning (the vectors every item has, index.py): a Slack
  message or email that talks about a meeting ("as discussed", "action items") and is
  nearest in meaning to exactly one meeting from just before it, with someone in common.
  Only above a measured cosine (DISCUSS_MIN, scripts/bench_xref.py) and by a margin over
  the next one: a missing link costs less than a wrong one.

Everything here is plain functions over text and candidates; the store (index.py) feeds
them and keeps the links.
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from .links import title_words

# --- GitHub references --------------------------------------------------------------------

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_GH_ITEM = re.compile(
    rf"https?://(?:www\.)?github\.com/({_OWNER})/({_REPO})/(issues|pulls?)/(\d{{1,7}})\b", re.I
)
_GH_COMMIT = re.compile(
    rf"https?://(?:www\.)?github\.com/({_OWNER})/({_REPO})/commit/([0-9a-f]{{7,40}})\b", re.I
)
_GH_REPO = re.compile(rf"https?://(?:www\.)?github\.com/({_OWNER})/({_REPO})(?=[/?#\s>)\]]|$)", re.I)
_URL = re.compile(r"https?://\S+")
_GH_SHORT = re.compile(rf"(?<![\w./#@-])({_OWNER})/({_REPO})#(\d{{1,7}})\b")
# "#117", "PR 117", "pull request #117", "issue 42": resolved only when the text names one repo.
_GH_BARE = re.compile(r"(?<![\w/&#.-])(?:#|\b(?:prs?|pull request|issue|bug)\s+#?)(\d{1,7})\b", re.I)
_NOT_REPOS = frozenset({"orgs", "settings", "marketplace", "notifications", "sponsors", "features", "topics",
                        "collections", "apps", "login", "search", "pulls", "issues", "explore"})  # fmt: skip


class GitHubRef(NamedTuple):
    id: str  # github:owner/repo#123 or github:owner/repo@<sha>
    repo: str  # owner/repo, lower case
    number: int | None
    sha: str | None
    kind: str  # "pr", "issue", "commit", or "" (an issue or a PR: the API says which)
    url: str  # where it opens (a short ref's issue URL redirects to its PR)


def _repo(owner: str, name: str) -> str | None:
    name = name.removesuffix(".git").rstrip(".")
    if not name or owner.lower() in _NOT_REPOS:
        return None
    return f"{owner}/{name}".lower()


def item_ref(repo: str, number: int, kind: str = "") -> GitHubRef:
    path = "pull" if kind == "pr" else "issues"
    return GitHubRef(f"github:{repo}#{number}", repo, number, None, kind,
                     f"https://github.com/{repo}/{path}/{number}")  # fmt: skip


def commit_ref(repo: str, sha: str) -> GitHubRef:
    sha = sha.lower()
    return GitHubRef(f"github:{repo}@{sha[:12]}", repo, None, sha, "commit",
                     f"https://github.com/{repo}/commit/{sha}")  # fmt: skip


def parse_id(id: str) -> GitHubRef | None:
    """A github record id back as a ref (github:owner/repo#123, github:owner/repo@sha)."""
    m = re.fullmatch(r"github:([^/\s]+/[^#@\s]+)#(\d+)", id)
    if m:
        return item_ref(m.group(1), int(m.group(2)))
    m = re.fullmatch(r"github:([^/\s]+/[^#@\s]+)@([0-9a-f]{7,40})", id)
    return commit_ref(m.group(1), m.group(2)) if m else None


def repos_named(text: str) -> list[str]:
    """The repos a text names by a github.com link or an owner/repo#n reference."""
    out: dict[str, None] = {}
    for m in _GH_REPO.finditer(text):
        if r := _repo(m.group(1), m.group(2)):
            out[r] = None
    for m in _GH_SHORT.finditer(text):
        if r := _repo(m.group(1), m.group(2)):
            out[r] = None
    return list(out)


def github_refs(text: str, allowed: Allow | None = None) -> list[GitHubRef]:
    """The GitHub issues, pull requests and commits a text names, in order, each once:
    their URLs, "owner/repo#123", and a bare "#123" or "PR 123" when the text names
    exactly one repo. Only repos `allowed` lets through."""
    found: list[tuple[int, GitHubRef]] = []
    for m in _GH_ITEM.finditer(text):
        kind = "pr" if m.group(3).lower().startswith("pull") else "issue"
        found += (
            [(m.start(), item_ref(r, int(m.group(4)), kind))] if (r := _repo(m.group(1), m.group(2))) else []
        )
    for m in _GH_COMMIT.finditer(text):
        found += [(m.start(), commit_ref(r, m.group(3)))] if (r := _repo(m.group(1), m.group(2))) else []
    for m in _GH_SHORT.finditer(text):
        found += [(m.start(), item_ref(r, int(m.group(3))))] if (r := _repo(m.group(1), m.group(2))) else []
    repos = repos_named(text)
    if len(repos) == 1 and _GH_WORDS.search(_URL.sub(" ", text)):
        # The URLs themselves end in /pull/117: don't count those numbers twice.
        spans = [(m.start(), m.end()) for p in (_GH_ITEM, _GH_COMMIT, _GH_SHORT) for m in p.finditer(text)]
        for m in _GH_BARE.finditer(text):
            if not any(a <= m.start() < b for a, b in spans):
                word = m.group(0).lower()
                kind = (
                    "pr" if word.startswith(("pr", "pull")) else "issue" if word.startswith("issue") else ""
                )
                found.append((m.start(), item_ref(repos[0], int(m.group(1)), kind)))
    out: dict[str, GitHubRef] = {}
    for _, ref in sorted(found, key=lambda x: x[0]):
        if ref.id in out or (allowed is not None and not allowed(ref.repo)):
            continue
        out[ref.id] = ref
    return list(out.values())


class Allow:
    """The `[github] orgs` / `repos` allow-list: empty lets every repo through."""

    def __init__(self, orgs: Iterable[str] = (), repos: Iterable[str] = ()) -> None:
        self.orgs = {o.strip().lower() for o in orgs if o.strip()}
        self.repos = {r.strip().lower() for r in repos if r.strip()}

    def __call__(self, repo: str) -> bool:
        if not self.orgs and not self.repos:
            return True
        return repo.lower() in self.repos or repo.split("/", 1)[0].lower() in self.orgs


# A task is about a GitHub item when its words say so: the item itself, or one of these.
_GH_WORDS = re.compile(
    r"\b(?:prs?|pull requests?|issues?|review(?:ing|ed)?|merg(?:e|ed|ing)|commits?|the fix|patch|"
    r"branch|ticket|bug|approve|land(?:ed|ing)?|github)\b",
    re.I,
)
_GH_TALK = re.compile(rf"{_GH_WORDS.pattern}|#\d+", re.I)


def task_subject(title: str, evidence: str, items: Sequence[GitHubRef]) -> GitHubRef | None:
    """The GitHub item a found task is about, or None: the one its title or quote names
    (by URL, owner/repo#n, or #n / PR n when the source names one repo), else the only
    item its source names, when the title or quote talks about a PR, an issue or a review.
    Two named: None (a task about two things isn't closed by either)."""
    words = f"{title}\n{evidence}"
    named: dict[str, GitHubRef] = {r.id: r for r in github_refs(words)}
    repos = {r.repo for r in items}
    if len(repos) == 1:  # "#117" in the quote, with the repo named elsewhere in the message
        repo = next(iter(repos))
        for m in _GH_BARE.finditer(words):
            n = int(m.group(1))
            match = [r for r in items if r.number == n and r.repo == repo]
            ref = match[0] if match else item_ref(repo, n)
            named.setdefault(ref.id, ref)
    if named:
        return next(iter(named.values())) if len(named) == 1 else None
    if len(items) == 1 and _GH_TALK.search(words):
        return items[0]
    return None


# --- links pasted into text ----------------------------------------------------------------

_GRANOLA = re.compile(r"https?://notes\.granola\.ai/[^\s<>()\[\]\"'|]+", re.I)
_SLACK = re.compile(
    r"https?://[a-z0-9-]+\.slack\.com/archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})(?:\?[^\s<>|]*?thread_ts=(\d+\.\d+))?",
    re.I,
)
_MESSAGE = re.compile(r"message://(?:%3C|<)([^\s<>]+?)(?:%3E|>)", re.I)


def granola_urls(text: str) -> list[str]:
    """Granola note pages in a text, without a query or trailing punctuation."""
    return list(
        dict.fromkeys(
            m.group(0).split("?", 1)[0].split("#", 1)[0].rstrip(".,;:!") for m in _GRANOLA.finditer(text)
        )
    )


def slack_links(text: str) -> list[tuple[str, str]]:
    """(channel, ts) of each Slack permalink in a text: a reply's thread root when the
    link says (thread_ts), else the message."""
    out: dict[tuple[str, str], None] = {}
    for m in _SLACK.finditer(text):
        ts = m.group(4) or f"{m.group(2)}.{m.group(3)}"
        out[(m.group(1).upper(), ts)] = None
    return list(out)


def message_ids(text: str) -> list[str]:
    """Message-IDs from Mail's message:// links in a text."""
    return list(dict.fromkeys(urllib.parse.unquote(m.group(1)).strip("<>") for m in _MESSAGE.finditer(text)))


# --- a meeting named by its title ----------------------------------------------------------

NAMED_BEFORE_S = 3 * 86400.0  # a text names a meeting from up to three days before it...
NAMED_AFTER_S = 86400.0  # ...or one coming in the next day ("for tomorrow's planning")
_CUES = ("meeting", "sync", "call", "standup", "stand-up", "1:1", "one-on-one", "review", "retro", "demo",
         "huddle", "session", "kickoff", "kick-off", "planning", "interview", "catch-up", "check-in",
         "offsite", "all-hands", "townhall", "workshop")  # fmt: skip
_DAY = re.compile(
    r"\b(today|this morning|this afternoon|yesterday|tomorrow|tonight)(?:'s|\u2019s)?\s+(1:1|[\w-]+)", re.I
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[:-][a-z0-9]+)*")


class Meeting(NamedTuple):
    id: str
    start: float
    title: str
    who: str = ""


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("\u2019", "'"))


def _in_order(want: Sequence[str], toks: Sequence[str], slack: int = 1) -> bool:
    """Whether `want` appears in `toks` in order, at most `slack` other words between two."""
    for i, t in enumerate(toks):
        if t != want[0]:
            continue
        at, ok = i, True
        for w in want[1:]:
            nxt = [j for j in range(at + 1, min(len(toks), at + 2 + slack)) if toks[j] == w]
            if not nxt:
                ok = False
                break
            at = nxt[0]
        if ok:
            return True
    return False


def _cue_of(title: str) -> set[str]:
    toks = set(_tokens(title))
    return {c for c in _CUES if c in toks}


def named_meeting(text: str, when: float, meetings: Iterable[Meeting]) -> str | None:
    """The meeting (id) a text names by its title, or None: one from NAMED_BEFORE_S
    before the text to NAMED_AFTER_S after whose title words all appear in order ("the
    vendor pricing review"; a one-word title with its own cue word, "vendor sync"), or
    the only meeting that day with the cue word after "today's" / "yesterday's" /
    "tomorrow's" ("in today's standup"). Of several, the one that started in the day
    before the text, when only one did; else None."""
    toks = _tokens(text)
    if not toks:
        return None
    near = [m for m in meetings if when - NAMED_BEFORE_S <= m.start <= when + NAMED_AFTER_S]
    hits: set[str] = set()
    for m in near:
        words = [w for w in _tokens(m.title) if w in title_words(m.title)]
        if not words:
            continue
        if len(words) >= 2 and _in_order(words, toks):
            hits.add(m.id)
        elif len(words) == 1:  # "Vendor sync": the word with its cue, "vendor sync" / "vendor call"
            cues = _cue_of(m.title) or {"meeting"}
            if any(_in_order([words[0], c], toks, 0) for c in [*cues, "meeting"]):
                hits.add(m.id)
    day0 = dt.date.fromtimestamp(when)
    for d in _DAY.finditer(text):
        word, cue = d.group(1).lower(), d.group(2).lower()
        if cue not in _CUES:
            continue
        shift = {"yesterday": -1, "tomorrow": 1}.get(word, 0)
        day = day0 + dt.timedelta(days=shift)
        same = [m for m in near if dt.date.fromtimestamp(m.start) == day and cue in _tokens(m.title)]
        if len(same) == 1:
            hits.add(same[0].id)
    if len(hits) == 1:
        return next(iter(hits))
    recent = [m.id for m in near if m.id in hits and when - 86400 <= m.start <= when]
    return recent[0] if len(recent) == 1 else None


# --- matched by meaning --------------------------------------------------------------------

DISCUSS_MIN = (
    0.76  # bge-small cosine (best window pair): 0.04 over the best wrong one in scripts/bench_xref.py
)
DISCUSS_MARGIN = 0.03  # over the next meeting
DISCUSS_BEFORE_S = 3 * 86400.0  # a meeting up to three days before the text
DISCUSS_AFTER_S = 2 * 3600.0  # or starting just after (a message sent as the meeting began)
# Words that say a message or email is about a meeting at all (no cue: no match by meaning).
MEETING_TALK = re.compile(
    r"\b(?:meeting|sync|call|standup|stand-up|discuss(?:ed|ion)?|recap|agenda|action items?|"
    r"follow(?:ing)?[ -]?up|1:1|retro|demo|huddle|notes from|we (?:agreed|decided|talked)|"
    r"per (?:the|our)|as (?:mentioned|we said)|earlier today)\b",
    re.I,
)


def first_names(who: str, me: Iterable[str] = ()) -> set[str]:
    """First names of the people in a who line ("Pat Doe, Sam <sam@x.com>"), the user's
    own (`[tasks] me`) left out."""
    mine = {n.split()[0].lower() for n in me if n.strip() and "@" not in n}
    out = set()
    for part in re.split(r"[,;]", who):
        part = re.sub(r"<[^>]*>", "", part).strip().strip('"')
        if "@" in part:
            part = part.split("@", 1)[0].replace(".", " ")
        words = re.findall(r"[a-z][a-z'-]+", part.lower())
        if words and words[0] not in mine and words[0] not in ("me", "them", "someone", "a"):
            out.add(words[0])
    return out


def nearest(scored: Sequence[tuple[str, float]], floor: float = DISCUSS_MIN,
            margin: float = DISCUSS_MARGIN) -> str | None:  # fmt: skip
    """The best of (id, cosine) when it reaches `floor` and beats the next by `margin`."""
    if not scored:
        return None
    ranked = sorted(scored, key=lambda x: -x[1])
    best = ranked[0]
    if best[1] < floor or (len(ranked) > 1 and best[1] - ranked[1][1] < margin):
        return None
    return best[0]


# A meeting's notes naming a Slack channel ("posted in #eng-voice").
_CHANNEL = re.compile(r"(?<![\w&/#])#([a-z][a-z0-9_-]{1,79})\b")


def channels_named(text: str) -> list[str]:
    return list(dict.fromkeys(f"#{m.group(1).lower()}" for m in _CHANNEL.finditer(text)))
