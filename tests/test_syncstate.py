"""Per-source sync status (syncstate.py): what each pass records, the state derived from
it (ok, failing, stalled, off, waiting), how it's said and printed, the pulse's once-a-day
line, tier 0's answer, the memory service's route, and each sync loop's hook. Synthetic
data only; every test has its own state file (conftest)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from scout import (
    config,
    index,
    local_intents,
    mailindex,
    memory_service,
    pulse,
    reminders_mac,
    syncstate,
    taskfarm,
    tasks,
)
from scout.mac import ToolError
from scout.memory import Memory

TZ = dt.timezone(dt.timedelta(hours=-4))
MORNING = dt.datetime(2026, 10, 15, 9, 0, tzinfo=TZ)
NOON = dt.datetime(2026, 10, 15, 12, 0, tzinfo=TZ)


def at(d: dt.datetime) -> float:
    return d.timestamp()


def cfg(**over: Any) -> config.Config:
    c = config.Config()
    c.granola.api_key = "g-test"
    c.slack.token = "xoxp-test"  # noqa: S105 (a made-up token)
    c.slack.app_token = "xapp-test"  # noqa: S105 (a made-up token)
    for k, v in over.items():
        section, field = k.split("__")
        setattr(getattr(c, section), field, v)
    return c


def by_source(sts: list[syncstate.Status]) -> dict[str, syncstate.Status]:
    return {s.source: s for s in sts}


# --- recording ------------------------------------------------------------------------


def test_a_good_pass_records_its_counts() -> None:
    syncstate.record("mail", 1200, added=3, updated=1, backlog=40, started=at(MORNING), now=at(MORNING) + 5)
    raw = json.loads(syncstate.PATH.read_text())["mail"]
    assert raw["last_attempt"] == at(MORNING) and raw["last_success"] == at(MORNING) + 5
    assert (raw["added"], raw["updated"], raw["backlog"], raw["errors"]) == (3, 1, 40, 0)
    assert raw["last_error"] is None and raw["every_s"] == 1200


def test_errors_count_up_and_a_success_clears_them() -> None:
    syncstate.record("slack", 900, error=TimeoutError("timed out"), now=at(MORNING))
    syncstate.record("slack", 900, error=TimeoutError("timed out"), now=at(MORNING) + 900)
    s = by_source(syncstate.statuses(now=at(MORNING) + 1000, cfg=cfg()))["slack"]
    assert s.state == "failing" and s.errors == 2
    assert s.failing_since == at(MORNING)  # the first error of the run
    assert s.last_error == "TimeoutError: timed out"
    syncstate.record("slack", 900, added=2, now=at(MORNING) + 1800)
    s = by_source(syncstate.statuses(now=at(MORNING) + 1800, cfg=cfg()))["slack"]
    assert s.state == "ok" and s.errors == 0 and s.failing_since is None and s.last_error is None
    assert s.added == 2


def test_track_records_success_and_failure_and_reraises() -> None:
    with syncstate.track("granola", 1800) as p:
        p.added, p.backlog = 4, 1
    with pytest.raises(ValueError), syncstate.track("slack", 900):
        raise ValueError("bad reply")
    sts = by_source(syncstate.statuses(cfg=cfg()))
    assert sts["granola"].state == "ok" and sts["granola"].added == 4 and sts["granola"].backlog == 1
    assert sts["slack"].state == "failing" and sts["slack"].last_error == "ValueError: bad reply"


def test_a_cancelled_pass_is_not_an_error() -> None:
    async def go() -> None:
        with syncstate.track("mail", 1200):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(go())
    assert "mail" not in json.loads(syncstate.PATH.read_text() if syncstate.PATH.exists() else "{}")


def test_errors_are_scrubbed_of_private_text() -> None:
    exc = ToolError(
        'Mail said: can\'t read "Budget for Pat" from pat@example.com at https://mail.example.com/x?id=9 '
        f"in {Path.home()}/Library/Mail/V10 " + "x" * 300
    )
    said = syncstate.scrub(exc)
    assert said.startswith("ToolError: Mail said:")
    for secret in ("Budget", "pat@example.com", "mail.example.com", str(Path.home())):
        assert secret not in said
    assert len(said) <= syncstate.ERROR_CHARS
    assert "Lease" not in syncstate.scrub(ValueError("no reply about \u201cLease renewal\u201d"))
    assert syncstate.scrub(KeyError("message_id")) == "KeyError: <...>"
    assert syncstate.scrub(RuntimeError()) == "RuntimeError"
    assert "\n" not in syncstate.scrub(OSError("line one\nline two"))


def test_two_writers_never_lose_each_others_sources(tmp_path: Path) -> None:
    """The app and the memory service write the same file: every write keeps the rest."""
    path = tmp_path / "sync.json"
    names = list(syncstate.SOURCES)

    def write(name: str) -> None:
        for i in range(20):
            syncstate.record(name, 60, added=i, path=path)

    threads = [threading.Thread(target=write, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(path.read_text())
    assert set(data) == set(names) and all(data[n]["added"] == 19 for n in names)


def test_a_broken_file_is_started_over(tmp_path: Path) -> None:
    path = tmp_path / "sync.json"
    path.write_text("{not json")
    assert syncstate.statuses(path=path, cfg=cfg())[0].state == "waiting"
    syncstate.record("mail", 1200, path=path)
    assert json.loads(path.read_text())["mail"]["errors"] == 0


def test_a_write_that_fails_never_fails_the_pass(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    syncstate.record("mail", 1200, path=blocked / "sync.json")  # its folder is a file: logged only


# --- derived state --------------------------------------------------------------------


def test_states_ok_stalled_off_and_waiting() -> None:
    now = at(NOON)
    syncstate.record("mail", 1200, now=now - 600)  # 10 minutes ago: ok
    syncstate.record("granola", 1800, now=now - 3 * 3600)  # 3 hours, every 30 min: stalled
    syncstate.record("vectors", 60, now=now - 600)  # 10 min at every minute: under the floor, ok
    syncstate.record("slack", 900, now=now - 60)
    sts = by_source(syncstate.statuses(now=now, cfg=cfg(slack__token="")))
    assert sts["mail"].state == "ok"
    assert sts["granola"].state == "stalled"
    assert sts["vectors"].state == "ok"
    assert sts["slack"].state == "off"  # not set up, whatever it recorded before
    assert sts["tasks"].state == "waiting"  # set up, no pass yet
    assert [s.source for s in syncstate.statuses(now=now, cfg=cfg())] == list(syncstate.SOURCES)


def test_never_succeeding_stalls_from_its_first_attempt() -> None:
    syncstate.record("slack", 900, error=OSError("down"), now=at(MORNING))
    s = by_source(syncstate.statuses(now=at(NOON), cfg=cfg()))["slack"]
    assert s.state == "failing" and s.last_success is None


def test_what_is_off_follows_the_config() -> None:
    on = syncstate.enabled(cfg())
    assert all(on.values())
    off = syncstate.enabled(
        cfg(index__mail=False, tasks__farm=False, index__semantic=False, granola__api_key="")
    )
    assert not off["mail"] and not off["meetings_email"] and not off["tasks"] and not off["reminders"]
    assert not off["vectors"] and not off["granola"] and off["links"]
    assert not syncstate.enabled(cfg(tasks__mirror=False))["reminders"]
    assert not syncstate.enabled(cfg(slack__app_token=""))["slack_events"]  # polling only
    assert not syncstate.enabled(cfg(slack__token=""))["slack_events"]


# --- saying it ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "words"),
    [
        (dt.datetime(2026, 10, 15, 9, 0), "9 this morning"),
        (dt.datetime(2026, 10, 15, 9, 30), "9:30 this morning"),
        (dt.datetime(2026, 10, 15, 12, 0), "noon"),
        (dt.datetime(2026, 10, 15, 14, 0), "2 this afternoon"),
        (dt.datetime(2026, 10, 15, 19, 15), "7:15 this evening"),
        (dt.datetime(2026, 10, 14, 16, 0), "yesterday afternoon"),
        (dt.datetime(2026, 10, 12, 8, 0), "Monday"),
        (dt.datetime(2026, 10, 1, 8, 0), "October 1"),
    ],
)
def test_when_words(when: dt.datetime, words: str) -> None:
    now = dt.datetime(2026, 10, 15, 20, 0, tzinfo=TZ)
    assert syncstate.when_words(at(when.replace(tzinfo=TZ)), now) == words


def test_speak_everything_fine_with_backlog() -> None:
    syncstate.record("mail", 1200, backlog=1, now=at(NOON))
    syncstate.record("vectors", 60, backlog=1200, now=at(NOON))
    said = syncstate.speak(syncstate.statuses(now=at(NOON), cfg=cfg()), NOON)
    assert said == (
        "Everything's syncing. Still to do: 1 mail message to read, "
        "1,200 items to index for search by meaning."
    )


def test_speak_problems_first() -> None:
    syncstate.record("mail", 1200, error=TimeoutError("timed out"), now=at(MORNING))
    syncstate.record("granola", 1800, now=at(MORNING) - 3600)
    syncstate.record("slack", 900, now=at(NOON))
    said = syncstate.speak(syncstate.statuses(now=at(NOON), cfg=cfg()), NOON)
    assert said == (
        "Mail sync has been failing since 9 this morning. "
        "Granola sync has stalled; its last good pass was at 8 this morning. "
        "The rest are syncing."
    )


def test_speak_nothing_set_up() -> None:
    c = cfg(index__mail=False, tasks__farm=False, index__semantic=False, granola__api_key="", slack__token="")
    sts = [s for s in syncstate.statuses(cfg=c) if s.source != "links"]
    assert syncstate.speak(sts, NOON) == "No syncing is set up."
    assert syncstate.speak(syncstate.statuses(cfg=c), NOON) == "Nothing has synced yet."


def test_lines_one_per_source() -> None:
    syncstate.record("mail", 1200, added=3, updated=1, backlog=40, now=at(NOON) - 300)
    syncstate.record("slack", 900, error=TimeoutError("timed out"), now=at(NOON) - 60)
    out = syncstate.lines(syncstate.statuses(now=at(NOON), cfg=cfg(granola__api_key="")), NOON)
    assert len(out) == len(syncstate.SOURCES)
    mail = next(line for line in out if line.startswith("mail "))
    assert "ok" in mail and "11:55" in mail and "+3 added, 1 updated" in mail and "40 mail messages" in mail
    slack = next(line for line in out if line.startswith("slack "))
    assert "failing" in slack and "1 error since 11:59" in slack and "TimeoutError: timed out" in slack
    assert "off" in next(line for line in out if line.startswith("granola "))
    assert "no pass yet" in next(line for line in out if line.startswith("tasks "))


def test_main_prints_the_lines(capsys: pytest.CaptureFixture[str]) -> None:
    syncstate.record("mail", 1200)
    syncstate.main()
    assert any(line.startswith("mail ") for line in capsys.readouterr().out.splitlines())


# --- the pulse ------------------------------------------------------------------------


def quiet_pulse(sts: list[syncstate.Status]) -> pulse.Pulse:
    async def e(start: str, end: str) -> dict[str, Any]:
        return {"events": []}

    async def r() -> dict[str, Any]:
        return {"reminders": []}

    async def t() -> list[tasks.Task]:
        return []

    return pulse.Pulse(10, 15, e, r, t, sync=lambda: sts)


def failing_mail(since: dt.datetime, now: dt.datetime) -> list[syncstate.Status]:
    syncstate.record("mail", 1200, error=TimeoutError("timed out"), now=at(since))
    syncstate.record("mail", 1200, error=TimeoutError("timed out"), now=at(now))
    return syncstate.statuses(now=at(now), cfg=cfg())


def test_the_pulse_says_a_long_failure_once_a_day() -> None:
    p = quiet_pulse(failing_mail(MORNING, NOON))
    assert asyncio.run(p.check(NOON)) == ["Mail sync has been failing since 9 this morning."]
    assert asyncio.run(p.check(NOON + dt.timedelta(hours=2))) == []  # not again today
    tomorrow = NOON + dt.timedelta(days=1)
    assert asyncio.run(p.check(tomorrow)) == ["Mail sync has been failing since yesterday morning."]


def test_the_pulse_waits_out_a_short_failure() -> None:
    p = quiet_pulse(failing_mail(NOON - dt.timedelta(minutes=20), NOON))
    assert asyncio.run(p.check(NOON)) == []  # 20 minutes: not yet worth saying


def test_the_pulse_says_a_stall() -> None:
    syncstate.record("granola", 1800, now=at(MORNING) - 3600)
    p = quiet_pulse(syncstate.statuses(now=at(NOON), cfg=cfg()))
    assert asyncio.run(p.check(NOON)) == [
        "Granola sync has stalled; its last good pass was at 8 this morning."
    ]


def test_the_pulse_is_silent_in_quiet_hours() -> None:
    night = dt.datetime(2026, 10, 15, 23, 0, tzinfo=TZ)
    p = quiet_pulse(failing_mail(MORNING, night))
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    async def go() -> None:
        task = asyncio.create_task(pulse.run(say, p, "22:00-08:00", now=lambda: night))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert said == []


def test_a_broken_status_file_never_stops_the_pulse() -> None:
    def broken() -> list[syncstate.Status]:
        raise OSError("disk")

    p = quiet_pulse([])
    p.sync = broken
    assert asyncio.run(p.check(NOON)) == []


# --- tier 0 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said", ["Is everything syncing?", "What's the sync status?", "sync status", "is the sync working"]
)
def test_tier0_answers_from_the_status(said: str) -> None:
    sts = failing_mail(MORNING, NOON)
    ctx = local_intents.Context(timers=None, now=lambda: NOON, sync=lambda: sts)
    out = asyncio.run(local_intents.answer(said, ctx))
    assert out is not None and out.startswith("Mail sync has been failing since 9 this morning.")


# --- the memory service route and its client ------------------------------------------


def test_the_route_and_client(tmp_path: Path) -> None:
    syncstate.record("mail", 1200, added=2)
    ix = index.Index(tmp_path / "memory.db", None)
    tok = tmp_path / "memory.token"
    svc = memory_service.Service(ix, Memory(tmp_path / "memory.json"), memory_service.token(tok))

    from aiohttp import web

    from scout import memory_client

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    port: list[int] = []
    runner = web.AppRunner(svc.app(), access_log=None)

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port.append(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert ready.wait(5)
        got = memory_client.sync_status(memory_client.Client(f"http://127.0.0.1:{port[0]}", tok))
        mail = next(s for s in got if s["source"] == "mail")
        assert mail["state"] == "ok" and mail["added"] == 2
        assert [s["source"] for s in got] == list(syncstate.SOURCES)
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
        ix.close()


# --- each loop reports ----------------------------------------------------------------


def stop_after(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio.sleep returns n times, then cancels the loop."""
    calls = [0]
    real = asyncio.sleep

    async def sleep(s: float, *a: Any) -> None:
        calls[0] += 1
        if calls[0] > n:
            raise asyncio.CancelledError
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)


def state(name: str) -> syncstate.Status:
    return by_source(syncstate.statuses(cfg=cfg()))[name]


def test_the_mail_loop_reports_mail_and_meeting_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sync_pass(ix: Any) -> dict[str, Any]:
        return {"stored": 5, "message_ids": 2, "left": 7}

    async def backfill(ix: Any, days: float) -> int:
        raise ToolError("Mail took too long")

    monkeypatch.setattr(mailindex, "sync_pass", sync_pass)
    monkeypatch.setattr(mailindex, "backfill_meetings", backfill)
    stop_after(1, monkeypatch)  # the startup wait, then one pass
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mailindex.loop(lambda: True))
    m = state("mail")
    assert (m.state, m.added, m.updated, m.backlog) == ("ok", 5, 2, 7)
    e = state("meetings_email")
    assert e.state == "failing" and e.last_error == "ToolError: Mail took too long"


def test_the_mail_loop_reports_nothing_while_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_after(1, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mailindex.loop(lambda: False))
    assert state("mail").state == "waiting"


def test_sync_pass_counts_what_is_left() -> None:
    import test_mailindex  # its fake Mail

    out = asyncio.run(mailindex.sync_pass(index.default(), test_mailindex.fake_mail([])))
    assert out["left"] == 0


def test_the_farm_loop_reports_tasks_and_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    async def farm(store: Any, a: Any, b: Any) -> dict[str, int]:
        return {"read": 2, "added": 1, "merged": 1, "skipped": 0}

    async def n(k: int) -> int:
        return k

    monkeypatch.setattr(taskfarm, "farm_pass", farm)
    monkeypatch.setattr(tasks, "sync_from_reminders", lambda store: n(1))
    monkeypatch.setattr(tasks, "repair_owners", lambda store: n(0))
    monkeypatch.setattr(tasks, "mirror_missing", lambda store: n(3))
    monkeypatch.setattr(tasks, "link_reminders", lambda store: n(2))
    monkeypatch.setattr(reminders_mac, "available", lambda: yes(True))
    index.default().task_add("Send Pat the budget", {"status": "open", "owner": "me"})
    stop_after(1, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(taskfarm.loop(lambda: True, lambda: None))
    t = state("tasks")
    assert (t.state, t.added, t.updated) == ("ok", 1, 1)
    r = state("reminders")
    assert (r.state, r.added, r.updated, r.backlog) == ("ok", 3, 3, 1)  # one open task not mirrored


async def yes(v: bool) -> bool:
    return v


def test_reminders_away_reports_failing_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def farm(store: Any, a: Any, b: Any) -> dict[str, int]:
        return {"read": 0, "added": 0, "merged": 0, "skipped": 0}

    monkeypatch.setattr(taskfarm, "farm_pass", farm)
    monkeypatch.setattr(reminders_mac, "available", lambda: yes(False))
    index.default().task_add("Send Pat the budget", {"status": "open", "owner": "me"})
    stop_after(1, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(taskfarm.loop(lambda: True, lambda: None))
    r = state("reminders")
    assert r.state == "failing" and "Reminders isn't available" in (r.last_error or "")
    assert state("tasks").state == "ok"


def test_a_failed_farm_pass_reports_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def farm(store: Any, a: Any, b: Any) -> dict[str, int]:
        raise RuntimeError("model down")

    monkeypatch.setattr(taskfarm, "farm_pass", farm)
    stop_after(1, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(taskfarm.loop(lambda: True, lambda: None))
    assert state("tasks").state == "failing" and state("reminders").state == "waiting"


def local_store() -> index.Index:
    ix = index.default()
    assert isinstance(ix, index.Index)
    return ix


class _Cfg:
    api_key = token = "x"  # noqa: S105 (a made-up token)
    every_min, backfill_days, skip = 15.0, 14.0, list[str]()


def test_the_granola_loop_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scout import granola

    monkeypatch.setattr(granola, "fetcher", lambda key: None)
    monkeypatch.setattr(granola, "sync_pass", lambda ix, get, since: (3, 4, "2026-10-15"))
    monkeypatch.setattr(granola, "PENDING", tmp_path / "pending.json")
    (tmp_path / "pending.json").write_text(json.dumps({"n1": 1.0, "n2": 2.0}))
    stop_after(0, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(memory_service.granola_loop(local_store(), _Cfg(), tmp_path / "granola.json"))
    g = state("granola")
    assert (g.state, g.added, g.backlog, g.every_s) == ("ok", 4, 2, 900)


def test_the_slack_loop_reports_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scout import slack

    def boom(*a: Any, **kw: Any) -> tuple[int, int]:
        raise slack.SlackError("Slack said invalid_auth")

    monkeypatch.setattr(slack, "fetcher", lambda token: None)
    monkeypatch.setattr(slack, "sync_pass", boom)
    stop_after(0, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(memory_service.slack_loop(local_store(), _Cfg(), tmp_path / "slack.json"))
    s = state("slack")
    assert s.state == "failing" and s.last_error == "SlackError: Slack said invalid_auth"


def test_upkeep_reports_links() -> None:
    ix = index.default()
    assert isinstance(ix, index.Index)
    ix.upkeep()
    assert state("links").state == "ok"


def test_the_background_pass_reports_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each batch is a pass: together they add every pending item, and the backlog falls
    to 0. (A pass after the backlog is gone adds 0, so only the sum is fixed.)"""
    from test_index import FakeEmbedder

    monkeypatch.setattr(index, "BACKLOG_PAUSE_S", 0.01)
    passes: list[tuple[int, int | None]] = []
    real = syncstate.record

    def spy(source: str, every_s: float, **kw: Any) -> None:
        if source == "vectors":
            passes.append((kw.get("added", 0), kw.get("backlog")))
        real(source, every_s, **kw)

    monkeypatch.setattr(syncstate, "record", spy)
    ix = index.Index(tmp_path / "v.db", FakeEmbedder(), min_similarity=0.3)
    for i in range(10):
        ix.put(f"n{i}", "turn", f"note {i}", f"note number {i}", ts=i)
    assert ix.pending_count() == 10

    async def run() -> None:
        task = asyncio.create_task(ix.background(lambda: True, every_s=30, batch=4))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if passes and passes[-1][1] == 0:
                break
        task.cancel()

    asyncio.run(run())
    assert sum(a for a, _ in passes) == 10 and passes[-1][1] == 0
    assert [a for a, _ in passes][:3] == [4, 4, 2]
    assert state("vectors").state == "ok"
    ix.close()
