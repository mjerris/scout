"""Syncs heal themselves before anyone is told (syncstate.py): a pass held back by a busy
room runs once it's overdue, in a gap in the talk, never mid-speech; a stalled sync is
woken (nudged) before anything is said, and nothing is said when that works; what's said
is for what the user must fix, with what to do. Plain code: no model decides anything.
Synthetic data only; every test has its own state file (conftest)."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout import config, mailindex, messages_mac, pulse, reminders_mac, syncstate, taskfarm, tasks
from scout.mac import ToolError

TZ = dt.timezone(dt.timedelta(hours=-4))
FAST = config.SyncConfig(overdue_times=3.0, gap_s=0.05, grace_min=10.0)
FARM_EVERY_S = config.TasksConfig().farm_every_min * 60


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(syncstate, "NUDGE_POLL_S", 0.01)
    monkeypatch.setattr(syncstate, "BUSY_RETRY_S", 0.01)
    monkeypatch.setattr(syncstate, "GAP_POLL_S", 0.01)


def cfg(**over: Any) -> config.Config:
    c = config.Config()
    for k, v in over.items():
        section, field = k.split("__")
        setattr(getattr(c, section), field, v)
    return c


async def n(k: int) -> int:
    return k


async def yes(v: bool) -> bool:
    return v


class Farm:
    """A fake farm pass that counts its runs, and fake Reminders work."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runs = 0

        async def farm(store: Any, a: Any, b: Any, **kw: Any) -> dict[str, int]:
            self.runs += 1
            return {"read": 0, "added": 0, "merged": 0, "skipped": 0}

        monkeypatch.setattr(taskfarm, "farm_pass", farm)
        monkeypatch.setattr(reminders_mac, "available", lambda: yes(True))
        for name in ("sync_from_reminders", "repair_owners", "mirror_missing", "link_reminders",
                     "github_followups"):  # fmt: skip
            monkeypatch.setattr(tasks, name, lambda store: n(0))


async def until(cond: Callable[[], bool], within: float = 2.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


def run_loop(coro: Any, body: Callable[[], Any]) -> Any:
    async def go() -> Any:
        task = asyncio.create_task(coro)
        try:
            return await body()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    return asyncio.run(go())


def farm_loop(idle: Callable[[], bool], active: Callable[[], bool]) -> Any:
    return taskfarm.loop(idle, lambda: None, active=active, first_s=0, sync=FAST)


# --- the farm runs when overdue, never mid-speech --------------------------------------


def test_the_farm_runs_when_overdue_even_while_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug: a 35-minute voice session left the room never idle, so the farm never
    ran. Overdue, it runs in the first gap in the talk."""
    farm = Farm(monkeypatch)
    late = time.time() - 4 * FARM_EVERY_S
    syncstate.record("tasks", FARM_EVERY_S, now=late)
    syncstate.record("reminders", FARM_EVERY_S, now=late)

    async def body() -> bool:
        return await until(lambda: farm.runs >= 1)

    assert run_loop(farm_loop(idle=lambda: False, active=lambda: False), body)
    sts = {s.source: s for s in syncstate.statuses(cfg=cfg())}
    assert sts["tasks"].state == "ok" and sts["reminders"].state == "ok"


def test_a_farm_pass_not_yet_overdue_waits_for_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    farm = Farm(monkeypatch)
    syncstate.record("tasks", FARM_EVERY_S, now=time.time() - 60)
    idle = [False]

    async def body() -> tuple[int, bool]:
        before = farm.runs if not await until(lambda: farm.runs > 0, 0.3) else -1
        idle[0] = True  # the room goes idle: the pass runs at the next look, not an interval later
        return before, await until(lambda: farm.runs >= 1)

    assert run_loop(farm_loop(idle=lambda: idle[0], active=lambda: False), body) == (0, True)


def test_no_pass_runs_while_the_room_is_mid_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    farm = Farm(monkeypatch)
    syncstate.record("tasks", FARM_EVERY_S, now=time.time() - 4 * FARM_EVERY_S)
    speaking = [True]

    async def body() -> tuple[bool, bool]:
        ran_mid_speech = await until(lambda: farm.runs > 0, 0.3)
        speaking[0] = False  # a gap in the talk
        return ran_mid_speech, await until(lambda: farm.runs >= 1)

    assert run_loop(farm_loop(idle=lambda: False, active=lambda: speaking[0]), body) == (False, True)


def test_the_gap_must_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """A moment between two words isn't a gap: quiet has to hold gap_s in a row."""
    flips = iter([False, True] * 20)

    async def go() -> float:
        t0 = time.monotonic()
        await syncstate.gap(lambda: next(flips, False), 0.05)
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.05


def test_the_mail_loop_runs_when_overdue_even_while_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    runs = [0]

    async def sync_pass(ix: Any) -> dict[str, Any]:
        runs[0] += 1
        return {"stored": 0, "message_ids": 0, "left": 0}

    monkeypatch.setattr(mailindex, "sync_pass", sync_pass)
    monkeypatch.setattr(mailindex, "backfill_meetings", lambda ix, days: n(0))
    syncstate.record("mail", 1200, now=time.time() - 4 * 1200)

    async def body() -> bool:
        return await until(lambda: runs[0] >= 1)

    loop = mailindex.loop(lambda: False, active=lambda: False, first_s=0, sync=FAST)
    assert run_loop(loop, body)


# --- a nudge wakes the loop ------------------------------------------------------------


def test_a_nudge_wakes_a_sleeping_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    farm = Farm(monkeypatch)

    async def body() -> bool:
        assert await until(lambda: farm.runs == 1)  # the first pass, then a 10-minute sleep
        await asyncio.sleep(0.05)
        assert farm.runs == 1
        syncstate.nudge("tasks")
        return await until(lambda: farm.runs == 2)

    assert run_loop(farm_loop(idle=lambda: True, active=lambda: False), body)


def test_a_nudge_runs_a_pass_in_a_busy_room(monkeypatch: pytest.MonkeyPatch) -> None:
    """Woken while the room is busy (a long voice session): the pass runs in a gap."""
    farm = Farm(monkeypatch)
    idle = [True]

    async def body() -> bool:
        assert await until(lambda: farm.runs == 1)
        idle[0] = False
        syncstate.nudge("reminders")
        return await until(lambda: farm.runs == 2)

    assert run_loop(farm_loop(idle=lambda: idle[0], active=lambda: False), body)


# --- the pulse: heal first, then speak ---------------------------------------------------


class Said:
    def __init__(self) -> None:
        self.nudged: list[str] = []

    def make(self) -> pulse.Pulse:
        async def e(start: str, end: str) -> dict[str, Any]:
            return {"events": []}

        async def r() -> dict[str, Any]:
            return {"reminders": []}

        async def t() -> list[tasks.Task]:
            return []

        def sync() -> list[syncstate.Status]:
            return syncstate.statuses(now=self.now.timestamp(), cfg=cfg())

        def nudge(source: str) -> None:
            self.nudged.append(source)
            syncstate.nudge(source, now=self.now.timestamp())

        return pulse.Pulse(10, 15, e, r, t, sync=sync, nudge=nudge, grace_min=10)

    def check(self, p: pulse.Pulse, now: dt.datetime) -> list[str]:
        self.now = now
        return asyncio.run(p.check(now))


NOON = dt.datetime(2026, 10, 15, 12, 0, tzinfo=TZ)


def test_an_overdue_sync_is_nudged_before_any_announcement() -> None:
    syncstate.record("tasks", FARM_EVERY_S, now=(NOON - dt.timedelta(minutes=35)).timestamp())
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []  # stalled: woken, nothing said
    assert s.nudged == ["tasks"]
    assert s.check(p, NOON + dt.timedelta(minutes=5)) == []  # within the grace: not woken again
    assert s.nudged == ["tasks"]


def test_nothing_is_said_when_the_nudge_works() -> None:
    syncstate.record("tasks", FARM_EVERY_S, now=(NOON - dt.timedelta(minutes=35)).timestamp())
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    syncstate.record(
        "tasks", FARM_EVERY_S, now=(NOON + dt.timedelta(minutes=1)).timestamp()
    )  # the woken pass
    assert s.check(p, NOON + dt.timedelta(minutes=15)) == []
    assert s.check(p, NOON + dt.timedelta(minutes=30)) == []


def test_a_stall_the_nudge_did_not_fix_is_said_with_what_to_do() -> None:
    syncstate.record("tasks", FARM_EVERY_S, now=(NOON - dt.timedelta(minutes=35)).timestamp())
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    later = s.check(p, NOON + dt.timedelta(minutes=15))
    assert later == [
        "The task farm has stalled; its last good pass was at 11:25 this morning, and waking it "
        "didn't help. Try restarting Scout."
    ]
    assert s.check(p, NOON + dt.timedelta(minutes=30)) == []  # once a day


def test_a_failure_that_needs_the_user_is_said_at_once_with_the_action() -> None:
    syncstate.record(
        "reminders", FARM_EVERY_S, now=(NOON - dt.timedelta(minutes=10)).timestamp(),
        error=ToolError("Reminders access is denied. Turn it on in System Settings, Privacy and Security, Reminders."),
    )  # fmt: skip
    s = Said()
    said = s.check(s.make(), NOON)
    assert said == [
        "Reminders mirroring needs you. Turn on Reminders access for Scout in System Settings, "
        "under Privacy and Security, Reminders."
    ]
    assert s.nudged == []


SOURCES_WOKEN = ["tasks", "links", "mail", "meetings_email"]  # syncs the pulse wakes, alike


def _stall(source: str, took_s: float = 0.0) -> None:
    """A good pass 35 minutes before NOON (taking took_s), none since: stalled at NOON."""
    end = (NOON - dt.timedelta(minutes=35)).timestamp()
    syncstate.record(source, FARM_EVERY_S, started=end - took_s, now=end)


@pytest.mark.parametrize("source", SOURCES_WOKEN)
def test_a_woken_pass_that_works_after_a_while_says_nothing(source: str) -> None:
    """2026-10-19 16:08: the stall was said in the same second its loop was woken, and the
    woken pass then worked 23 s later. Nothing is decided while the woken pass may run."""
    _stall(source)
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == [] and s.nudged == [source]
    for minutes in (0, 1, 5, 9):  # the woken pass is still running: nothing said
        assert s.check(p, NOON + dt.timedelta(minutes=minutes, seconds=1)) == []
    syncstate.record(source, FARM_EVERY_S, started=NOON.timestamp() + 1, now=NOON.timestamp() + 600)
    for minutes in (11, 15, 40):
        assert s.check(p, NOON + dt.timedelta(minutes=minutes)) == []


@pytest.mark.parametrize("source", SOURCES_WOKEN)
def test_a_woken_pass_that_fails_is_said_once(source: str) -> None:
    _stall(source)
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    assert s.check(p, NOON + dt.timedelta(seconds=30)) == []  # running
    syncstate.record(
        source, FARM_EVERY_S, error=RuntimeError("boom"), started=NOON.timestamp() + 1,
        now=NOON.timestamp() + 60,
    )  # fmt: skip
    first = s.check(p, NOON + dt.timedelta(minutes=2))  # judged as soon as it failed
    assert len(first) == 1 and "waking it didn't help" in first[0]
    assert s.check(p, NOON + dt.timedelta(minutes=15)) == []
    assert s.check(p, NOON + dt.timedelta(minutes=40)) == []


def test_a_sync_woken_again_is_not_said_in_that_same_check() -> None:
    """No check came between the woken pass's time running out and the next wake: the
    sync is woken again then, and judged by that pass, not in the same breath."""
    _stall("tasks")
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    assert s.check(p, NOON + dt.timedelta(minutes=25)) == [] and s.nudged == ["tasks", "tasks"]
    assert s.check(p, NOON + dt.timedelta(minutes=30)) == []  # that pass's 10 minutes
    assert len(s.check(p, NOON + dt.timedelta(minutes=36))) == 1


def test_a_woken_pass_that_never_finishes_is_said_once_after_its_time() -> None:
    _stall("tasks")
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    assert s.check(p, NOON + dt.timedelta(minutes=9)) == []
    said = [*s.check(p, NOON + dt.timedelta(minutes=10)), *s.check(p, NOON + dt.timedelta(minutes=45))]
    assert len(said) == 1 and "waking it didn't help" in said[0]


def test_meeting_notes_stalled_behind_a_stuck_mail_are_not_said_as_well() -> None:
    """2026-10-19: meeting notes by email run after the mail pass in one loop; with Mail
    stuck they never ran, and their stall was said on top of Mail's own trouble. Then Mail
    was restarted and the woken pass worked: nothing more is said."""
    t = NOON.timestamp()
    syncstate.record("meetings_email", 1200, now=t - 5 * 3600)  # 10:42-ish: the last good pass
    syncstate.record("mail", 1200, now=t - 5 * 3600)
    syncstate.record("mail", 1200, error=TimeoutError("/usr/bin/osascript timed out"), now=t - 600)
    s = Said()
    p = s.make()
    said = []
    for minutes in (0, 18, 35):  # meeting notes woken, their pass never runs: mail fails first
        said += s.check(p, NOON + dt.timedelta(minutes=minutes))
        syncstate.record("mail", 1200, error=TimeoutError("timed out"), now=t + minutes * 60 + 60)
    assert "meetings_email" in s.nudged
    assert not any("meeting notes" in x.lower() for x in said), said
    # Mail restarted; the woken pass works for both
    end = t + 40 * 60
    syncstate.record("mail", 1200, started=end - 23, now=end)
    syncstate.record("meetings_email", 1200, started=end - 1, now=end)
    assert s.check(p, NOON + dt.timedelta(minutes=41)) == []
    assert s.check(p, NOON + dt.timedelta(minutes=70)) == []


def test_a_slow_sync_s_woken_pass_gets_its_usual_time() -> None:
    """A sync whose passes take 6 minutes isn't judged at the 10-minute grace."""
    _stall("tasks", took_s=360)
    s = Said()
    p = s.make()
    assert s.check(p, NOON) == []
    assert s.check(p, NOON + dt.timedelta(minutes=15)) == []  # 3 x 6 min = 18 min to finish
    assert len(s.check(p, NOON + dt.timedelta(minutes=18))) == 1


@pytest.mark.parametrize(
    ("source", "error", "words"),
    [
        ("slack", "SlackError: conversations.history: invalid_auth", "Slack no longer accepts"),
        ("github", "GitHubError: HTTP 401", "gh auth login"),
        ("granola", "GranolaError: /v1/notes: HTTP 401", "Granola no longer accepts"),
        ("mail", "ToolError: Not allowed to control Mail yet.", "Automation"),
    ],
)
def test_credentials_and_permissions_need_the_user(source: str, error: str, words: str) -> None:
    action = syncstate.needs_user(source, error)
    assert action is not None and words in action
    assert syncstate.needs_user(source, "TimeoutError: timed out") is None


def test_a_passing_failure_is_retried_not_said() -> None:
    s = Said()
    p = s.make()
    syncstate.record(
        "mail", 1200, error=TimeoutError("timed out"), now=(NOON - dt.timedelta(minutes=20)).timestamp()
    )
    assert s.check(p, NOON) == []
    syncstate.record(
        "mail", 1200, error=TimeoutError("timed out"), now=(NOON + dt.timedelta(minutes=50)).timestamp()
    )
    assert s.check(p, NOON + dt.timedelta(minutes=55)) == [
        "Mail sync has been failing since 11:40 this morning, and retrying hasn't fixed it. "
        "Scout status shows the error."
    ]


def test_retries_back_off_up_to_the_interval() -> None:
    assert syncstate.retry_in("mail", 1200) == 1200  # never ran: the interval
    waits = []
    for _ in range(6):
        syncstate.record("mail", 1200, error=TimeoutError("timed out"))
        waits.append(syncstate.retry_in("mail", 1200))
    assert waits == [60, 120, 240, 480, 960, 1200]
    syncstate.record("mail", 1200)
    assert syncstate.retry_in("mail", 1200) == 1200
    syncstate.record("slack", 900, error=ToolError("invalid_auth"))
    assert syncstate.retry_in("slack", 900) == 900  # the user's to fix: no hurry


# --- Reminders ---------------------------------------------------------------------------


def test_the_farm_says_why_reminders_is_away(monkeypatch: pytest.MonkeyPatch) -> None:
    Farm(monkeypatch)
    monkeypatch.setattr(reminders_mac, "available", lambda: yes(False))
    monkeypatch.setattr(reminders_mac, "refusal", lambda: "Reminders access is denied.")

    async def body() -> bool:
        return await until(lambda: syncstate.by_name(cfg=cfg())["reminders"].state == "failing")

    assert run_loop(farm_loop(idle=lambda: True, active=lambda: False), body)
    r = syncstate.by_name(cfg=cfg())["reminders"]
    assert r.action is not None and "Reminders access" in r.action


def test_reminders_mirroring_is_off_with_scouts_own_lists() -> None:
    assert not syncstate.enabled(cfg(lists__backend="scout"))["reminders"]
    assert syncstate.enabled(cfg(lists__backend="auto"))["reminders"]


# --- the messages helper is restarted, not reported ------------------------------------


def _fake_helper(sock: Path) -> Callable[[], None]:
    """Start answering on `sock` (as launchd restarting the helper would)."""

    def start() -> None:
        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(b'{"ok": true, "access": "granted"}\n')
            await writer.drain()
            writer.close()

        asyncio.get_running_loop().create_task(asyncio.start_unix_server(serve, path=str(sock)))

    return start


@pytest.fixture
def short() -> Any:
    """Unix socket paths are limited to 104 bytes: a short folder in the repo's .tmp."""
    import os
    import shutil

    d = Path(__file__).resolve().parents[1] / ".tmp" / "sh" / str(os.getpid())
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_a_stopped_messages_helper_is_restarted(short: Path) -> None:
    tmp_path = short
    sock, token = tmp_path / "m.sock", tmp_path / "token"
    token.write_text("t")
    binary = tmp_path / "scout-messages"
    binary.write_text("")
    restarts: list[int] = []
    start = _fake_helper(sock)

    def restart() -> None:
        restarts.append(1)
        start()

    h = messages_mac.Helper(socket=sock, token=token, binary=binary, restart=restart, restart_wait_s=2.0)
    assert asyncio.run(messages_mac.status(h)) == {"ok": True, "access": "granted"}
    assert restarts == [1]


def test_a_messages_helper_that_wont_restart_says_what_to_do(short: Path) -> None:
    tmp_path = short
    sock, token = tmp_path / "m.sock", tmp_path / "token"
    token.write_text("t")
    binary = tmp_path / "scout-messages"
    binary.write_text("")
    restarts: list[int] = []
    h = messages_mac.Helper(socket=sock, token=token, binary=binary, restart=lambda: restarts.append(1),
                            restart_wait_s=0.1)  # fmt: skip
    with pytest.raises(ToolError, match="isn't running"):
        asyncio.run(messages_mac.status(h))
    with pytest.raises(ToolError, match="isn't running"):
        asyncio.run(messages_mac.status(h))
    assert restarts == [1]  # tried once, not on every call


# --- scout-memory's loops: woken by a nudge, retried with backoff -------------------------


class _Cfg:
    api_key = "g-test"
    every_min = 30.0


def test_the_granola_loop_wakes_on_a_nudge_and_backs_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scout import granola, index, memory_service

    runs = [0]

    def sync_pass(ix: Any, get: Any, since: Any) -> tuple[int, int, Any]:
        runs[0] += 1
        if runs[0] == 2:
            raise granola.GranolaError("/v1/notes: timed out")
        return 0, 0, since

    monkeypatch.setattr(granola, "fetcher", lambda key: None)
    monkeypatch.setattr(granola, "sync_pass", sync_pass)
    monkeypatch.setattr(granola, "PENDING", tmp_path / "pending.json")

    async def body() -> list[float]:
        assert await until(lambda: runs[0] == 1)  # then a 30-minute sleep
        syncstate.nudge("granola")
        assert await until(lambda: runs[0] == 2)  # woken; this one fails
        assert await until(lambda: syncstate.by_name(cfg=cfg(granola__api_key="g"))["granola"].errors == 1)
        return [syncstate.retry_in("granola", 1800)]

    ix = index.default()
    assert isinstance(ix, index.Index)
    assert run_loop(memory_service.granola_loop(ix, _Cfg(), tmp_path / "g.json"), body) == [60.0]


# --- the room: what counts as speaking or listening -------------------------------------


def test_the_room_is_active_only_while_scout_speaks_or_listens() -> None:
    from test_routing import make

    r = make()
    a = r.a
    assert not a.room_active()  # idle
    for state in ("listening", "speaking", "confirming"):
        a.state = state
        assert a.room_active()
    a.state = "thinking"  # Claude working on a long turn: a gap a pass may use
    assert not a.room_active()
    a.state = "idle"
    a.last_onset = time.monotonic()  # speech Scout isn't listening for (a TV)
    assert not a.room_active()
