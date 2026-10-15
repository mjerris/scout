"""Per-source sync status: whether each thing Scout keeps in sync is keeping up.

Every sync loop reports each pass here: Mail, meeting notes by email, Granola, Slack,
the task farm, Reminders mirroring, the vectors for search by meaning, and link upkeep.
A pass records when it ran, whether it worked, how many items it added or updated, and
what's still waiting (the backlog) where that's known. A failure keeps only the
exception's type and a scrubbed message (no quoted text, addresses or links), never
mail or meeting text.

From that, each source is in one state:

    ok        the last pass worked, within its time
    failing   the last pass failed (errors counts the failures in a row)
    stalled   no good pass for 3 times its interval (30 minutes at least)
    off       not set up in the config
    waiting   set up, no pass yet

The app and scout-memory both write state/sync_status.json, under a lock, each write
keeping the other's sources. `python -m scout.syncstate` prints a line per source
(scoutctl.sh status shows it), scout-memory serves it at GET /v1/sync, tier 0 answers
"is everything syncing?", and the pulse says once a day when a source has been failing
for an hour or has stalled.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA, Config, load

log = logging.getLogger(__name__)

PATH = DATA / "state" / "sync_status.json"

STALL_TIMES = 3  # no good pass in this many intervals: stalled
STALL_FLOOR_S = 1800.0  # but never sooner than this (the vectors look every minute)
SAY_AFTER_S = 3600.0  # the pulse speaks of a failure once it's lasted this long
ERROR_CHARS = 160


@dataclass(frozen=True)
class Source:
    label: str  # in `scout status`
    spoken: str  # the subject of a spoken sentence
    one: str = "item waiting"  # the backlog, said with a count
    many: str = "items waiting"


SOURCES: dict[str, Source] = {
    "mail": Source("Mail", "Mail sync", "mail message to read", "mail messages to read"),
    "meetings_email": Source("Meeting notes by email", "Bringing in meeting notes from email"),
    "granola": Source("Granola", "Granola sync", "Granola note to fetch again", "Granola notes to fetch again"),
    "slack": Source("Slack", "Slack sync"),
    "tasks": Source("Task farm", "The task farm"),
    "reminders": Source("Reminders", "Reminders mirroring", "task to put in Reminders", "tasks to put in Reminders"),
    "vectors": Source(
        "Search by meaning",
        "Indexing for search by meaning",
        "item to index for search by meaning",
        "items to index for search by meaning",
    ),
    "links": Source("Links", "Link upkeep"),
}  # fmt: skip


def enabled(cfg: Config) -> dict[str, bool]:
    """Which sources the config sets up (the rest are off)."""
    return {
        "mail": cfg.index.mail,
        "meetings_email": cfg.index.mail,
        "granola": bool(cfg.granola.api_key),
        "slack": bool(cfg.slack.token),
        "tasks": cfg.tasks.farm,
        "reminders": cfg.tasks.farm and cfg.tasks.mirror,
        "vectors": cfg.index.semantic and cfg.index.background_embed,
        "links": True,
    }


# --- recording ------------------------------------------------------------------------

_URL = re.compile(r"\b[a-z][\w+.-]*://\S+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED = re.compile(r"\"[^\"]*\"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|(?<!\w)'[^']*'(?!\w)")


def scrub(exc: BaseException) -> str:
    """An error, short and without private text: its type, and its message with quoted
    text, links, addresses and the home folder taken out."""
    msg = str(exc)
    msg = _URL.sub("<link>", msg)
    msg = _EMAIL.sub("<address>", msg)
    msg = _QUOTED.sub("<...>", msg)
    msg = msg.replace(str(Path.home()), "~")
    msg = " ".join(msg.split())
    out = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return out if len(out) <= ERROR_CHARS else out[: ERROR_CHARS - 1] + "…"


_lock = threading.Lock()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(
    source: str,
    every_s: float,
    *,
    error: BaseException | None = None,
    added: int = 0,
    updated: int = 0,
    backlog: int | None = None,
    started: float | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """One pass of `source` (which runs every `every_s`): a success with its counts, or
    `error`. Never fails the caller: a status that can't be written is logged."""
    path = PATH if path is None else path
    now = time.time() if now is None else now
    started = now if started is None else started
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.with_suffix(".lock").open("a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)  # the other process's write finishes first
            data = _read(path)
            e: dict[str, Any] = data[source] if isinstance(data.get(source), dict) else {}
            e.setdefault("first_seen", started)
            e["last_attempt"], e["every_s"] = started, float(every_s)
            if error is not None:
                e["errors"] = int(e.get("errors") or 0) + 1
                e["failing_since"] = e.get("failing_since") or started
                e["last_error"], e["error_at"] = scrub(error), now
            else:
                e.update(last_success=now, errors=0, failing_since=None, last_error=None)
                e.update(
                    added=int(added), updated=int(updated), backlog=backlog, took_s=round(now - started, 1)
                )
            data[source] = e
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(path)
    except OSError as exc:
        log.warning("sync status for %s not written: %s", source, exc)


@dataclass
class Pass:
    """What a pass did, filled in by the loop inside track()."""

    added: int = 0  # items new or changed in the store
    updated: int = 0  # other records it touched (a Message-ID, a reminder done)
    backlog: int | None = None  # what's still waiting, when known


@contextlib.contextmanager
def track(source: str, every_s: float, path: Path | None = None) -> Iterator[Pass]:
    """Record the pass inside the with-block: a success with its counts, or the error it
    raised (raised on, for the loop's own handling). A cancelled pass isn't recorded."""
    p = Pass()
    t0 = time.time()
    try:
        yield p
    except Exception as exc:
        record(source, every_s, error=exc, started=t0, path=path)
        raise
    record(source, every_s, added=p.added, updated=p.updated, backlog=p.backlog, started=t0, path=path)


# --- reading --------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    source: str
    label: str
    spoken: str
    state: str  # ok | failing | stalled | off | waiting
    errors: int = 0
    failing_since: float | None = None
    first_seen: float | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    added: int = 0
    updated: int = 0
    backlog: int | None = None
    every_s: float = 0.0


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def statuses(now: float | None = None, path: Path | None = None, cfg: Config | None = None) -> list[Status]:
    """Every source's status, in SOURCES order."""
    now = time.time() if now is None else now
    data = _read(PATH if path is None else path)
    on = enabled(cfg or load())
    out = []
    for name, src in SOURCES.items():
        e = data.get(name)
        e = e if isinstance(e, dict) else {}
        every = _num(e.get("every_s")) or 0.0
        good, first = _num(e.get("last_success")), _num(e.get("first_seen"))
        errors = int(e.get("errors") or 0)
        if not on.get(name, True):
            state = "off"
        elif not e:
            state = "waiting"
        elif errors:
            state = "failing"
        elif now - (good or first or now) > max(STALL_TIMES * every, STALL_FLOOR_S):
            state = "stalled"
        else:
            state = "ok"
        backlog = e.get("backlog")
        out.append(
            Status(
                name, src.label, src.spoken, state, errors, _num(e.get("failing_since")), first,
                _num(e.get("last_attempt")), good, e.get("last_error"), int(e.get("added") or 0),
                int(e.get("updated") or 0), int(backlog) if isinstance(backlog, int) else None, every,
            )
        )  # fmt: skip
    return out


def as_dicts(sts: list[Status]) -> list[dict[str, Any]]:
    return [dataclasses.asdict(s) for s in sts]


# --- saying it ------------------------------------------------------------------------


def _local(ts: float, now: dt.datetime) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, now.tzinfo) if now.tzinfo else dt.datetime.fromtimestamp(ts)


def _part(t: dt.datetime) -> str:
    return "morning" if t.hour < 12 else "afternoon" if t.hour < 17 else "evening"


def when_words(ts: float, now: dt.datetime, at: bool = False) -> str:
    """'9 this morning', 'noon', 'yesterday afternoon', 'Monday', 'October 1'; with
    `at`, ready to follow 'was': 'at 9 this morning', 'on Monday'."""
    t = _local(ts, now)
    days = (now.date() - t.date()).days
    if days <= 0:
        if (t.hour, t.minute) in ((12, 0), (0, 0)):
            words = "noon" if t.hour == 12 else "midnight"
        else:
            words = f"{t.hour % 12 or 12}{t.strftime(':%M') if t.minute else ''} this {_part(t)}"
        return f"at {words}" if at else words
    if days == 1:
        return f"yesterday {_part(t)}"
    words = t.strftime("%A") if days < 7 else f"{t:%B} {t.day}"
    return f"on {words}" if at else words


def problem(s: Status, now: dt.datetime) -> str:
    """The sentence for a failing or stalled source."""
    if s.state == "failing" and s.failing_since is not None:
        return f"{s.spoken} has been failing since {when_words(s.failing_since, now)}."
    if s.last_success is not None:
        return f"{s.spoken} has stalled; its last good pass was {when_words(s.last_success, now, at=True)}."
    return f"{s.spoken} hasn't finished a pass since {when_words(s.first_seen or now.timestamp(), now)}."


def _backlog(s: Status) -> str:
    src = SOURCES[s.source]
    n = s.backlog or 0
    return f"{n:,} {src.one if n == 1 else src.many}"


def speak(sts: list[Status], now: dt.datetime) -> str:
    """Tier 0's answer to "is everything syncing?"."""
    on = [s for s in sts if s.state != "off"]
    if not on:
        return "No syncing is set up."
    bad = [s for s in on if s.state in ("failing", "stalled")]
    good = [s for s in on if s.state == "ok"]
    parts = [problem(s, now) for s in bad]
    if not bad:
        if not good:
            return "Nothing has synced yet."
        parts.append("Everything's syncing.")
    elif good:
        parts.append("The rest are syncing.")
    if todo := [_backlog(s) for s in on if s.backlog]:
        parts.append(f"Still to do: {', '.join(todo)}.")
    return " ".join(parts)


def alerts(sts: list[Status], now: dt.datetime) -> list[tuple[str, str]]:
    """(source, sentence) for the pulse: failing for SAY_AFTER_S or more, or stalled."""
    t = now.timestamp()
    return [
        (s.source, problem(s, now))
        for s in sts
        if s.state == "stalled"
        or (s.state == "failing" and s.failing_since is not None and t - s.failing_since >= SAY_AFTER_S)
    ]


# --- printing it ----------------------------------------------------------------------


def _clock(ts: float, now: dt.datetime) -> str:
    t = _local(ts, now)
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%b} {t.day} {t:%H:%M}"


def _ago(ts: float, now: dt.datetime) -> str:
    s = max(0.0, now.timestamp() - ts)
    if s < 3600:
        return f"{round(s / 60)} min ago"
    if s < 2 * 86400:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def lines(sts: list[Status], now: dt.datetime) -> list[str]:
    """One line per source, for `scout status`."""
    out = []
    for s in sts:
        if s.state == "off":
            detail = "not set up"
        elif s.state == "waiting":
            detail = "no pass yet"
        else:
            bits = []
            if s.state == "failing" and s.failing_since is not None:
                bits.append(
                    f"{s.errors} error{'s' * (s.errors != 1)} since {_clock(s.failing_since, now)}: {s.last_error}"
                )
            if s.last_success is not None:
                bits.append(f"last good {_clock(s.last_success, now)} ({_ago(s.last_success, now)})")
                bits.append(f"+{s.added} added, {s.updated} updated")
            if s.backlog:
                bits.append(f"backlog {_backlog(s)}")
            detail = "; ".join(bits) or "no good pass yet"
        out.append(f"{s.source:<15}{s.state:<9}{detail}")
    return out


def main() -> None:
    for line in lines(statuses(), dt.datetime.now().astimezone()):
        print(line)


if __name__ == "__main__":
    main()
