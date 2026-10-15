"""The nightly pass that learns behavior rules from corrections (docs/behavior-rules.md).

Over a day's journal, plain code finds the turns where the user corrected Scout: a
correction said outright ("no, that's wrong", "I already told you ..."), a request
asked again soon after in nearly the same words, or a barge-in over an answer (the
turn that followed it). Only when there are some does the local model (tier 1's
LocalModel, on this Mac: no Claude, nothing leaves it) read them and draft at most
MAX_CANDIDATES rules, each with the turns it came from. They wait in
state/behavior_candidates.json for the morning briefing, which offers them one at a
time ("Twice yesterday you corrected how I read ticket numbers. Should I always read
ticket numbers digit by digit?"). Nothing is kept without a yes; a declined rule is
never offered again; one not answered is offered again for OFFER_DAYS days."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import behavior, journal
from .config import DATA
from .memory import _clean, keywords, spoken

log = logging.getLogger(__name__)

PATH = DATA / "state" / "behavior_candidates.json"
MAX_CANDIDATES = 3
MAX_SIGNALS = 12  # corrections the model reads, at most (its prompt stays small)
OFFER_DAYS = 7
REPEAT_WINDOW_S = 600.0  # a request asked again this soon, in nearly the same words
BEFORE_WINDOW_S = 900.0  # the answer a correction is about came at most this long before
BARGE_WINDOW_S = 300.0  # the turn after a barge-in onset, within this
REPEAT_OVERLAP = 0.6
FIELD_CHARS = 200

_CORRECTION = re.compile(
    r"^(?:no\b[,.!]?\s+\S|nope\b|wrong\b|that'?s (?:not (?:right|it|what i (?:asked|said|meant))|wrong|incorrect)"
    r"|that is (?:not right|wrong)|not what i (?:asked|said|meant)|i (?:already )?(?:told|said|asked)\b"
    r"|i meant\b|you (?:got|have) (?:it|that) wrong|actually,)",
    re.I,
)
_TOLD_YOU = re.compile(r"\b(?:i already told you|i told you (?:before|already)|how many times)\b", re.I)


class Model(Protocol):
    async def complete(
        self, system: str, user: str, max_tokens: int = 120, timeout_s: float = 20.0, prefix: str = ""
    ) -> str: ...


@dataclass(frozen=True)
class Turn:
    id: str
    ts: float
    q: str
    a: str


@dataclass(frozen=True)
class Signal:
    turn: Turn  # where the user corrected Scout
    before: Turn | None  # the answer it corrected
    why: str  # correction | repeat | barge-in


@dataclass
class Candidate:
    id: str
    rule: str
    about: str
    turns: list[str] = field(default_factory=list)
    day: str = ""
    status: str = "pending"  # pending | kept | declined


def day_turns(root: Path, day: dt.date) -> list[Turn]:
    """A journal day's exchanges, oldest first (the files under root/journal)."""
    p = journal.path_for(root, dt.datetime.combine(day, dt.time(12)).timestamp())
    try:
        items = journal.parse(p)
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for it in items:
        body = str(it.get("body") or "")
        a = next((ln.removeprefix("Scout: ") for ln in body.splitlines() if ln.startswith("Scout: ")), "")
        out.append(Turn(str(it["id"]), float(it.get("ts") or 0), str(it.get("title") or ""), a))
    return sorted(out, key=lambda t: t.ts)


def _overlap(a: str, b: str) -> float:
    ka, kb = keywords(a), keywords(b)
    if len(ka) < 2 or len(kb) < 2:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def signals(turns: Sequence[Turn], barge_ins: Sequence[float] = ()) -> list[Signal]:
    """The turns where the user corrected Scout, each with the answer it was about."""
    out: dict[str, Signal] = {}

    def before(i: int) -> Turn | None:
        if i > 0 and turns[i].ts - turns[i - 1].ts <= BEFORE_WINDOW_S:
            return turns[i - 1]
        return None

    for i, t in enumerate(turns):
        q = _clean(t.q)
        prev = before(i)
        if (_CORRECTION.match(q) and prev is not None) or _TOLD_YOU.search(q):
            out[t.id] = Signal(t, prev, "correction")
            continue
        for p in reversed(turns[:i]):
            if t.ts - p.ts > REPEAT_WINDOW_S:
                break
            if _overlap(q, p.q) >= REPEAT_OVERLAP:
                out[t.id] = Signal(t, p, "repeat")
                break
    for onset in barge_ins:
        j = next((n for n, t in enumerate(turns) if t.ts >= onset), None)
        if j is None or turns[j].ts - onset > BARGE_WINDOW_S or turns[j].id in out:
            continue
        out[turns[j].id] = Signal(turns[j], before(j), "barge-in")
    return sorted(out.values(), key=lambda s: s.turn.ts)


SYSTEM = f"""\
You read the conversation log of Scout, a voice assistant, on the user's own computer.
Each numbered item is a moment the user corrected Scout, asked again, or cut Scout off.
Find lasting preferences behind them: how the user wants Scout to behave every time (how
to say numbers, names or dates, what to include or leave out, how long to answer). Ignore
one-time mistakes (a misheard word, a wrong fact) and anything that isn't about Scout.
Write at most {MAX_CANDIDATES}, one JSON object per line, nothing else:
{{"about": "<what Scout got wrong, from Scout's side, e.g. how I read ticket numbers>", "rule": "<the rule in the user's words, e.g. Always read ticket numbers digit by digit>", "items": [<the item numbers it comes from>]}}
If there is no lasting preference, write NONE."""


def _cut(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= FIELD_CHARS else s[: FIELD_CHARS - 1] + "…"


def prompt(found: Sequence[Signal], day: dt.date) -> str:
    lines = [f"Corrections on {day:%A, %B} {day.day}:"]
    for n, s in enumerate(found, 1):
        lines.append(f"{n}. ({s.why})")
        if s.before is not None:
            lines.append(f"   The user asked: {_cut(s.before.q)}")
            lines.append(f"   Scout answered: {_cut(s.before.a)}")
        lines.append(f"   Then the user said: {_cut(s.turn.q)}")
    return "\n".join(lines)


def parse(out: str, found: Sequence[Signal], day: dt.date) -> list[Candidate]:
    """The model's lines as candidates; anything malformed is dropped."""
    got: list[Candidate] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        rule = behavior.normalized(str(d.get("rule") or ""))
        about = " ".join(str(d.get("about") or "").split()).rstrip(".")[:80]
        raw = d.get("items")
        items: list[Any] = raw if isinstance(raw, list) else []
        idx = [i for i in items if isinstance(i, int) and 1 <= i <= len(found)]
        if not rule or len(rule.split()) < 3 or len(rule) > behavior.MAX_CHARS or not idx or not about:
            continue
        rid = behavior.rule_id(rule)
        if any(c.id == rid for c in got):
            continue
        turns = list(dict.fromkeys(found[i - 1].turn.id for i in idx))
        got.append(Candidate(rid, rule, about, turns, day.isoformat()))
    return got


class Candidates:
    """Suggested rules waiting for a yes, the ones declined, and when the pass and the
    monthly review last ran (state/behavior_candidates.json)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PATH
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _all(self, data: dict[str, Any]) -> list[Candidate]:
        return [
            Candidate(str(c["id"]), str(c["rule"]), str(c.get("about") or ""),
                      [str(t) for t in c.get("turns") or []], str(c.get("day") or ""),
                      str(c.get("status") or "pending"))
            for c in data.get("candidates") or []
            if isinstance(c, dict) and c.get("id") and c.get("rule")
        ]  # fmt: skip

    def pending(self, today: dt.date | None = None) -> list[Candidate]:
        cutoff = ((today or dt.date.today()) - dt.timedelta(days=OFFER_DAYS)).isoformat()
        return [c for c in self._all(self._load()) if c.status == "pending" and c.day >= cutoff]

    def known(self) -> set[str]:
        """Ids of every rule already suggested (pending, kept or declined)."""
        return {c.id for c in self._all(self._load())}

    def add(self, new: Sequence[Candidate], day: dt.date) -> None:
        with self._lock:
            data = self._load()
            data["candidates"] = [asdict(c) for c in self._all(data)] + [asdict(c) for c in new]
            data["last_pass"] = day.isoformat()
            self._save(data)

    def resolve(self, cid: str, accepted: bool) -> None:
        with self._lock:
            data = self._load()
            cs = self._all(data)
            for c in cs:
                if c.id == cid:
                    c.status = "kept" if accepted else "declined"
            data["candidates"] = [asdict(c) for c in cs]
            self._save(data)

    def last_pass(self) -> str:
        return str(self._load().get("last_pass") or "")

    def review_due(self, today: dt.date) -> bool:
        return str(self._load().get("last_review") or "") != f"{today:%Y-%m}"

    def reviewed(self, today: dt.date) -> None:
        with self._lock:
            data = self._load()
            data["last_review"] = f"{today:%Y-%m}"
            self._save(data)


async def nightly(
    model: Model,
    day: dt.date,
    root: Path,
    barge_ins: Sequence[float] = (),
    cands: Candidates | None = None,
    rules: behavior.Behavior | None = None,
) -> list[Candidate]:
    """The pass for one day: its corrections, the model's drafts (only when there are
    corrections), kept as candidates unless they're rules already or were suggested
    before. Never fails the caller: a model error leaves nothing."""
    cands = cands or Candidates()
    rules = rules or behavior.default()
    found = signals(day_turns(root, day), barge_ins)[-MAX_SIGNALS:]
    if not found:
        log.info("rules: no corrections on %s", day)
        cands.add([], day)
        return []
    try:
        out = await model.complete(SYSTEM, prompt(found, day), max_tokens=300, timeout_s=120.0)
    except Exception as exc:
        log.warning("rules: the local model didn't read %s's corrections (%s)", day, exc)
        return []
    have = {r.id for r in rules.rules()} | cands.known()
    new = [c for c in parse(out, found, day) if c.id not in have][:MAX_CANDIDATES]
    cands.add(new, day)
    log.info("rules: %d corrections on %s, %d rules suggested", len(found), day, len(new))
    return new


def offer_text(c: Candidate) -> str:
    """ "Twice yesterday you corrected how I read ticket numbers. Should I always read
    ticket numbers digit by digit?" """
    n = len(c.turns)
    times = {1: "Yesterday", 2: "Twice yesterday"}.get(n, f"{n} times yesterday")
    rule = spoken(c.rule)
    return f"{times} you corrected {c.about}. Should I {rule[:1].lower() + rule[1:]}?"


def recent_talk(root: Path, today: dt.date, days: int = behavior.UNUSED_DAYS) -> list[str]:
    """What was asked and answered on the last `days` days (for the monthly review)."""
    out: list[str] = []
    for n in range(1, days + 1):
        for t in day_turns(root, today - dt.timedelta(days=n)):
            out += [t.q, t.a]
    return out
