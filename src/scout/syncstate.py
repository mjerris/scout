"""Per-source sync status: whether each thing Scout keeps in sync is keeping up.

Every sync loop reports each pass here: Mail, meeting notes by email, Granola, Slack,
Slack's live events, the task farm, Reminders mirroring, the vectors for search by meaning, and link upkeep.
A pass records when it ran, whether it worked, how many items it added or updated, and
what's still waiting (the backlog) where that's known. A failure keeps only the
exception's type and a scrubbed message (no quoted text, addresses or links), never
mail or meeting text.

From that, each source is in one state:

    ok        the last pass worked, within its time
    connected / reconnecting
              a live connection (Slack's events): its state, in place of ok
    failing   the last pass failed (errors counts the failures in a row)
    stalled   no good pass for 3 times its interval (30 minutes at least)
    off       not set up in the config
    waiting   set up, no pass yet

The app and scout-memory both write state/sync_status.json, under a lock, each write
keeping the other's sources. `python -m scout.syncstate` prints a line per source
(scoutctl.sh status shows it), scout-memory serves it at GET /v1/sync, and tier 0 answers
"is everything syncing?".

Self-healing comes first, in plain code (no model decides anything here):

    a pass held back    a loop that waits for an idle room runs its pass anyway once it
                        is [sync] overdue_times intervals late (overdue), in the first
                        gap of gap_s seconds with nobody speaking or being listened to
                        (gap); a busy room is looked at again in BUSY_RETRY_S, not a
                        whole interval later
    a failed pass       retried sooner, with backoff (retry_in): 1, 2, 4... minutes up to
                        the interval; an error only the user can fix waits the interval
    a stalled source    the pulse wakes its loop (nudge; each loop sleeps with sleep(),
                        which returns early on a nudge) and says nothing

The pulse speaks (alerts) only when that didn't work or the user must act: an error only
the user can fix (NEEDS_USER: Reminders or Mail access, a token a service no longer
accepts), said with what to do; a failure that retrying hasn't fixed in an hour; a stall
still there grace_min after its nudge. Each once a day per source.
"""

from __future__ import annotations

import asyncio
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA, Config, SyncConfig, load
from .mac import ToolError

log = logging.getLogger(__name__)

PATH = DATA / "state" / "sync_status.json"

GOOD = ("ok", "connected", "reconnecting")  # reconnecting that keeps failing is "failing"
STALL_TIMES = 3  # no good pass in this many intervals: stalled
STALL_FLOOR_S = 1800.0  # but never sooner than this (the vectors look every minute)
SAY_AFTER_S = 3600.0  # the pulse speaks of a failure retrying hasn't fixed in this long
ERROR_CHARS = 160
NUDGE_POLL_S = 5.0  # a sleeping loop looks for a nudge this often
BUSY_RETRY_S = 30.0  # a pass held back by a busy room is looked at again this soon
GAP_POLL_S = 0.5  # while waiting for a gap in the talk, look this often
RETRY_FIRST_S = 60.0  # a failed pass is tried again after this, doubling up to its interval


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
    "slack_events": Source("Slack live events", "Slack's live connection"),
    "tasks": Source("Task farm", "The task farm"),
    "reminders": Source("Reminders", "Reminders mirroring", "task to put in Reminders", "tasks to put in Reminders"),
    "vectors": Source(
        "Search by meaning",
        "Indexing for search by meaning",
        "item to index for search by meaning",
        "items to index for search by meaning",
    ),
    "links": Source("Links", "Link upkeep", "record to read for cross-references", "records to read for cross-references"),
    "github": Source("GitHub", "GitHub sync", "GitHub item to fetch", "GitHub items to fetch"),
}  # fmt: skip


def enabled(cfg: Config) -> dict[str, bool]:
    """Which sources the config sets up (the rest are off)."""
    return {
        "mail": cfg.index.mail,
        "meetings_email": cfg.index.mail,
        "granola": bool(cfg.granola.api_key),
        "slack": bool(cfg.slack.token),
        "slack_events": bool(cfg.slack.token and cfg.slack.app_token),
        "tasks": cfg.tasks.farm,
        "reminders": cfg.tasks.farm and cfg.tasks.mirror and cfg.lists.backend != "scout",
        "vectors": cfg.index.semantic and cfg.index.background_embed,
        "links": True,
        "github": cfg.github.enabled,
    }


# Errors only the user can fix: (sources, what the error says, what to do), checked in
# order. Fixed text, said as is; everything else is retried (retry_in) before it's said.
NEEDS_USER: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("reminders",), ("Reminders access", "access not granted"),
     "Turn on Reminders access for Scout in System Settings, under Privacy and Security, Reminders."),
    (("mail", "meetings_email"), ("Not allowed to control Mail",),
     "Allow Scout to control Mail in System Settings, under Privacy and Security, Automation."),
    (("mail", "meetings_email"), ("Mail isn't set up",), "Open Mail once and add an account."),
    (("slack", "slack_events"), ("invalid_auth", "not_authed", "token_revoked", "account_inactive"),
     "Slack no longer accepts Scout's token. Put a new one in Scout's config, in the slack section."),
    (("github",), ("HTTP 401",),
     "GitHub no longer accepts Scout's token. Run gh auth login, or put a new token in Scout's config."),
    (("granola",), ("HTTP 401", "HTTP 403"),
     "Granola no longer accepts Scout's API key. Put a new one in Scout's config, in the granola section."),
)  # fmt: skip


def needs_user(source: str, error: str | None) -> str | None:
    """What the user must do about this error, or None when retrying may fix it."""
    if not error:
        return None
    for sources, says, action in NEEDS_USER:
        if source in sources and any(w in error for w in says):
            return action
    return None


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
    conn: str | None = None,
) -> None:
    """One pass of `source` (which runs every `every_s`): a success with its counts, or
    `error`. Never fails the caller: a status that can't be written is logged. `conn`:
    a live connection's state (connected, reconnecting), shown in place of "ok"."""
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
            if conn is not None:
                e["conn"] = conn
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


def nudge(source: str, now: float | None = None, path: Path | None = None) -> None:
    """Wake `source`'s loop for a pass now (it sleeps with sleep()); the pulse does this
    for a stalled source before saying anything. Never fails the caller."""
    path = PATH if path is None else path
    now = time.time() if now is None else now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.with_suffix(".lock").open("a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = _read(path)
            e: dict[str, Any] = data[source] if isinstance(data.get(source), dict) else {}
            e["nudged_at"] = now
            data[source] = e
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(path)
        log.info("sync: %s woken for a pass", source)
    except OSError as exc:
        log.warning("sync: %s not woken: %s", source, exc)


def _entry(source: str, path: Path | None) -> dict[str, Any]:
    e = _read(PATH if path is None else path).get(source)
    return e if isinstance(e, dict) else {}


def nudged(sources: tuple[str, ...], after: float, path: Path | None = None) -> bool:
    """Whether any of `sources` was nudged after `after` (a time.time())."""
    data = _read(PATH if path is None else path)
    for name in sources:
        e = data.get(name)
        at = _num(e.get("nudged_at")) if isinstance(e, dict) else None
        if at is not None and at > after:
            return True
    return False


def overdue(
    source: str,
    every_s: float,
    since: float,
    times: float,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """No pass tried for `times` intervals: since its last attempt, or since `since` (the
    loop's start) when there's none."""
    now = time.time() if now is None else now
    e = _entry(source, path)
    last = _num(e.get("last_attempt"))
    return now - (since if last is None else last) > times * every_s


def retry_in(source: str, every_s: float, path: Path | None = None) -> float:
    """How long until the next pass: the interval, or after a failure sooner, with backoff
    (RETRY_FIRST_S, doubling with each failure in a row, never past the interval). An error
    only the user can fix waits the whole interval."""
    e = _entry(source, path)
    errors = int(e.get("errors") or 0)
    if not errors or needs_user(source, e.get("last_error")):
        return every_s
    return float(min(every_s, RETRY_FIRST_S * 2.0 ** min(errors - 1, 16)))


async def sleep(sources: tuple[str, ...], seconds: float, path: Path | None = None) -> bool:
    """Sleep `seconds`, or until one of `sources` is nudged (True)."""
    t0 = time.time()
    end = t0 + seconds
    while (left := end - time.time()) > 0:
        await asyncio.sleep(min(left, NUDGE_POLL_S))
        if nudged(sources, t0, path):
            return True
    return False


async def gap(active: Callable[[], bool], gap_s: float) -> None:
    """Return once nobody has been speaking or listened to for `gap_s` seconds in a row."""
    quiet_since: float | None = None
    while True:
        if active():
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        if quiet_since is not None and time.monotonic() - quiet_since >= gap_s:
            return
        await asyncio.sleep(min(GAP_POLL_S, max(0.01, gap_s / 4)))


async def may_run(
    sources: tuple[str, ...],
    every_s: float,
    idle: Callable[[], bool],
    active: Callable[[], bool],
    since: float,
    cfg: SyncConfig,
    path: Path | None = None,
) -> bool:
    """Whether an idle-only pass may run now: the room is idle; or the pass is overdue
    (or nudged since `since`, the loop's last pass) and, after waiting for a gap in the
    talk, runs anyway. False: look again in BUSY_RETRY_S."""
    if idle():
        return True
    late = any(overdue(s, every_s, since, cfg.overdue_times, path=path) for s in sources)
    if not late and not nudged(sources, since, path):
        return False
    log.info("sync: %s overdue with the room busy; running in the next gap", "/".join(sources))
    await gap(active, cfg.gap_s)
    return True


@dataclass
class Pass:
    """What a pass did, filled in by the loop inside track()."""

    added: int = 0  # items new or changed in the store
    updated: int = 0  # other records it touched (a Message-ID, a reminder done)
    backlog: int | None = None  # what's still waiting, when known
    failed: str = ""  # why the pass couldn't do its work, when it didn't raise (Reminders away)


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
    if p.failed:
        record(source, every_s, error=ToolError(p.failed), started=t0, path=path)
        return
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
    nudged_at: float | None = None  # when the pulse last woke its loop
    action: str | None = None  # failing with an error only the user can fix: what to do


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
            state = str(e.get("conn") or "ok")  # a live connection says which: connected...
        backlog = e.get("backlog")
        out.append(
            Status(
                name, src.label, src.spoken, state, errors, _num(e.get("failing_since")), first,
                _num(e.get("last_attempt")), good, e.get("last_error"), int(e.get("added") or 0),
                int(e.get("updated") or 0), int(backlog) if isinstance(backlog, int) else None, every,
                _num(e.get("nudged_at")), needs_user(name, e.get("last_error")) if state == "failing" else None,
            )
        )  # fmt: skip
    return out


def by_name(
    now: float | None = None, path: Path | None = None, cfg: Config | None = None
) -> dict[str, Status]:
    """statuses() by source name."""
    return {s.source: s for s in statuses(now, path, cfg)}


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
    """The sentence for a failing or stalled source (with what to do, when it's the user's
    to fix)."""
    if s.action:
        return f"{s.spoken} needs you. {s.action}"
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
    good = [s for s in on if s.state in GOOD]
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


def to_nudge(sts: list[Status], now: float, grace_s: float) -> list[str]:
    """The stalled sources to wake: not woken since their last good pass, or woken
    `grace_s` ago or more and still stalled (woken again)."""
    out = []
    for s in sts:
        if s.state != "stalled":
            continue
        good = s.last_success or s.first_seen or 0.0
        if s.nudged_at is None or s.nudged_at <= good or now - s.nudged_at >= grace_s:
            out.append(s.source)
    return out


def alerts(sts: list[Status], now: dt.datetime, grace_s: float) -> list[tuple[str, str]]:
    """(source, sentence) for the pulse, only when healing didn't work or the user must
    act: an error only the user can fix (with what to do); a failure retrying hasn't
    fixed in SAY_AFTER_S; a stall still there `grace_s` after its loop was woken."""
    t = now.timestamp()
    out = []
    for s in sts:
        if s.state == "failing" and s.action:
            out.append((s.source, f"{s.spoken} needs you. {s.action}"))
        elif s.state == "failing" and s.failing_since is not None and t - s.failing_since >= SAY_AFTER_S:
            out.append((s.source, f"{s.spoken} has been failing since {when_words(s.failing_since, now)}, and "
                                  "retrying hasn't fixed it. Scout status shows the error."))  # fmt: skip
        elif s.state == "stalled" and _woken_in_vain(s, t, grace_s):
            last = (f"its last good pass was {when_words(s.last_success, now, at=True)}"
                    if s.last_success is not None else "it hasn't finished a pass yet")  # fmt: skip
            out.append((s.source, f"{s.spoken} has stalled; {last}, and waking it didn't help. "
                                  "Try restarting Scout."))  # fmt: skip
    return out


def _woken_in_vain(s: Status, now: float, grace_s: float) -> bool:
    good = s.last_success or s.first_seen or 0.0
    return s.nudged_at is not None and s.nudged_at > good and now - s.nudged_at >= grace_s


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
            if s.action:
                bits.append(f"needs you: {s.action}")
            detail = "; ".join(bits) or "no good pass yet"
        out.append(f"{s.source:<15}{s.state:<9}{detail}")
    return out


def main() -> None:
    for line in lines(statuses(), dt.datetime.now().astimezone()):
        print(line)


if __name__ == "__main__":
    main()
