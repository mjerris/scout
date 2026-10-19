"""Mail stuck answering AppleScript heals itself (mail_heal.py): timeouts lead to the
probe, a stuck Mail is restarted (at most once an hour) and the pass retried; the
message it hung on is skipped for a day; a restart that didn't help is said once; only
text Mail has downloaded is asked for. Fakes for osascript, the probe, Mail's process
and the clock: no test touches the real Mail (conftest refuses it)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import signal
from pathlib import Path
from typing import Any

import pytest

from scout import config, index, mac, mail_heal, mail_mac, mailindex, messages_mac, syncstate
from scout.mac import ToolError

NOW = dt.datetime.now().astimezone()


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


class FakeMail(mail_heal.Procs):
    """Mail's process: pid 4242; quits on SIGTERM unless `stubborn` (then on SIGKILL);
    `answers_after_restart`: whether the probe answers once it's opened again."""

    def __init__(self, health: Health, stubborn: bool = False, answers_after_restart: bool = True) -> None:
        self.health = health
        self.stubborn = stubborn
        self.answers_after_restart = answers_after_restart
        self.running = True
        self.did: list[str] = []

    async def pid(self) -> int | None:
        return 4242 if self.running else None

    async def alive(self, pid: int) -> bool:
        return self.running

    async def ask_to_quit(self) -> None:
        self.did.append("quit event")  # Mail is stuck: it can't act on it

    def signal(self, pid: int, sig: int) -> None:
        self.did.append(signal.Signals(sig).name)
        if sig == signal.SIGKILL or not self.stubborn:
            self.running = False

    async def launch(self) -> None:
        self.did.append("open")
        self.running = True
        self.health.answering = self.answers_after_restart


class Health(mail_heal.Health):
    answering: bool = False
    probes: int = 0


def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[Health, FakeMail, Clock]:
    clock = Clock()
    h = Health(clock=clock, sleep=clock.sleep, path=tmp_path / "mail_health.json")

    async def probe() -> bool:
        h.probes += 1
        return h.answering

    h.probe = probe
    procs = FakeMail(h, **kw)
    h.procs = procs
    monkeypatch.setattr(mail_heal, "HEALTH", h)
    return h, procs, clock


def timeout(*reading: int) -> mac.TimedOut:
    return mac.TimedOut("/usr/bin/osascript timed out", [f"reading {i}" for i in reading])


INBOX = [
    {"id": 41, "date": (NOW - dt.timedelta(minutes=30)).isoformat(), "sender": "Pat <pat@example.com>", "subject": "Hello", "read": False},
    {"id": 40, "date": (NOW - dt.timedelta(minutes=40)).isoformat(), "sender": "Sam <sam@example.com>", "subject": "Lunch?", "read": True},
]  # fmt: skip


class StuckMail:
    """A Mail scripting fake: reading text of a message in `hangs` times out (as mac._run
    does, naming it) while `stuck`; listing times out while stuck too."""

    def __init__(self, hangs: set[int]) -> None:
        self.hangs = hangs
        self.stuck = True
        self.read: list[list[int]] = []

    async def __call__(self, script: str, *args: str) -> str:
        if script == mail_mac._LIST:
            if self.stuck:
                raise timeout()
            return json.dumps({"messages": INBOX})
        if script == mail_mac._READ_MANY:
            ids = [int(i) for i in args[0].split(",")]
            self.read.append(ids)
            if self.stuck or set(ids) & self.hangs:
                self.stuck = True
                raise timeout(*sorted(set(ids) & self.hangs))
            return json.dumps(
                {"messages": [{**m, "body": f"text {m['id']}"} for m in INBOX if m["id"] in ids]}
            )
        raise AssertionError("unexpected Mail script")


# --- noticing, restarting, retrying ---------------------------------------------------


def test_timeouts_lead_to_the_probe_then_a_restart_then_the_pass_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h, procs, _ = make(tmp_path, monkeypatch)
    mail = StuckMail(hangs={41})
    runs: list[str] = []

    async def a_pass() -> str:
        runs.append("pass")
        if mail.stuck and len(runs) == 1:
            await mail_mac._call(mail, mail_mac._LIST, "1")
        return "done"

    h.timed_out(timeout(41))  # an earlier read hung on 41 (the culprit)
    caplog.set_level(logging.INFO, logger="scout.mail_heal")

    async def go() -> str:
        try:
            return await h.guarded(a_pass)
        finally:
            mail.stuck = False

    # one timeout counted before, the pass's own is the second: probe, restart, pass again
    assert asyncio.run(go()) == "done"
    assert runs == ["pass", "pass"]
    assert procs.did == ["quit event", "SIGTERM", "open"]
    assert h.timeouts == 0 and not h.gave_up
    assert h.skipped() == {41}
    assert "Mail was stuck answering AppleScript; restarted it" in caplog.text


def test_a_stubborn_mail_is_force_quit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, procs, clock = make(tmp_path, monkeypatch, stubborn=True)
    h.timeouts = 2
    t0 = clock()
    assert asyncio.run(h.heal()) is True
    assert procs.did == ["quit event", "SIGTERM", "SIGKILL", "open"]
    assert clock() - t0 >= mail_heal.QUIT_WAIT_S  # waited before each signal


def test_one_timeout_is_not_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, procs, _ = make(tmp_path, monkeypatch)

    async def a_pass(script: str, *args: str) -> str:
        raise timeout()

    with pytest.raises(ToolError, match="timed out"):
        asyncio.run(h.guarded(lambda: mail_mac._call(a_pass, mail_mac._LIST)))
    assert h.timeouts == 1 and h.probes == 0 and procs.did == []


def test_mail_answering_the_probe_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, procs, _ = make(tmp_path, monkeypatch)
    h.answering = True  # slow, not stuck
    h.timed_out(timeout(41))
    h.timed_out(timeout())
    assert asyncio.run(h.heal()) is False
    assert procs.did == [] and h.probes == 1
    assert h.timeouts == 0 and h.skipped() == set()  # nothing skipped: Mail wasn't stuck on it


def test_at_most_one_restart_an_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, procs, clock = make(tmp_path, monkeypatch)
    h.timeouts = 2
    assert asyncio.run(h.heal()) is True
    procs.did.clear()
    h.answering = False  # stuck again, 30 minutes later
    clock.t += 1800
    h.timeouts = 2
    with pytest.raises(ToolError, match="restarting it didn't help"):
        asyncio.run(h.heal())
    assert procs.did == [] and h.gave_up
    # the limit is kept across Scout restarts (a fresh Health, the same file)
    fresh, procs2, _ = make(tmp_path, monkeypatch)
    fresh.clock, fresh.sleep = clock, clock.sleep
    fresh.timeouts = 2
    with pytest.raises(ToolError):
        asyncio.run(fresh.heal())
    assert procs2.did == []
    # an hour after the restart, another is allowed
    clock.t += 1801
    fresh.timeouts, fresh.gave_up = 2, False
    assert asyncio.run(fresh.heal()) is True
    assert procs2.did == ["quit event", "SIGTERM", "open"]


def test_a_restart_that_did_not_help_is_said_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, procs, clock = make(tmp_path, monkeypatch, answers_after_restart=False)
    h.timeouts = 2
    with pytest.raises(ToolError) as err:
        asyncio.run(h.heal())
    assert procs.did[-1] == "open" and h.gave_up
    started = dt.datetime.now().astimezone()
    syncstate.record("mail", 1200, error=err.value, now=started.timestamp())
    syncstate.record("meetings_email", 1200, now=started.timestamp() - 7200)  # stalled behind it

    # later passes: the probe only, no restart, the same error
    async def must_not_run() -> None:
        raise AssertionError("Mail asked while it's stuck")

    for k in range(3):
        clock.t += 1200
        with pytest.raises(ToolError) as again:
            asyncio.run(h.guarded(must_not_run))
        syncstate.record("mail", 1200, error=again.value, now=started.timestamp() + 60 * (k + 1))
    assert procs.did.count("open") == 1

    from scout import pulse, tasks

    async def e(start: str, end: str) -> dict[str, Any]:
        return {"events": []}

    async def r() -> dict[str, Any]:
        return {"reminders": []}

    async def t() -> list[tasks.Task]:
        return []

    when = started + dt.timedelta(minutes=5)
    c = config.Config()
    p = pulse.Pulse(10, 15, e, r, t, sync=lambda: syncstate.statuses(now=when.timestamp(), cfg=c),
                    nudge=lambda s: None)  # fmt: skip
    said = asyncio.run(p.check(when)) + asyncio.run(p.check(when + dt.timedelta(minutes=20)))
    assert said == [
        "Mail sync needs you. Mail is stuck, and restarting it didn't help. Quit Mail, with Force Quit if "
        "it won't, and open it again."
    ]  # meeting notes by email, stalled behind it, isn't said as well

    # Mail answers again (the user restarted it): the next pass runs
    h.answering = True

    async def works() -> str:
        return "ok"

    assert asyncio.run(h.guarded(works)) == "ok" and not h.gave_up


def test_a_read_while_stuck_fails_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, _, _ = make(tmp_path, monkeypatch)
    h.gave_up = True

    async def mail(script: str, *args: str) -> str:
        raise AssertionError("Mail asked while it's stuck")

    with pytest.raises(ToolError, match="Mail is stuck"):
        asyncio.run(mail_mac._call(mail, mail_mac._READ, "41", "100"))


def test_the_real_process_control_refuses_under_tests() -> None:
    procs = mail_heal.Procs()
    for call in (procs.pid(), procs.ask_to_quit(), procs.launch()):
        with pytest.raises(RuntimeError, match="tests must not"):
            asyncio.run(call)
    with pytest.raises(RuntimeError):
        procs.signal(1, signal.SIGTERM)


# --- the culprit: skipped for a day -----------------------------------------------------


def test_the_message_mail_hung_on_is_skipped_for_a_day_then_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, clock = make(tmp_path, monkeypatch)
    mail = StuckMail(hangs={41})
    mail.stuck = False
    ix = index.default()
    with pytest.raises(ToolError):  # the first pass hangs Mail reading 41
        asyncio.run(mailindex.sync_pass(ix, mail))
    assert h.suspects == {41} and h.timeouts == 1
    h.timed_out(timeout())  # and the next request queues behind it
    mail.stuck = True

    async def restarted() -> bool:
        ok = await h.heal()
        mail.stuck = False
        return ok

    assert asyncio.run(restarted()) is True
    assert h.skipped() == {41}
    mail.read.clear()
    out = asyncio.run(mailindex.sync_pass(ix, mail))
    assert mail.read == [[40]]  # 41 isn't asked for
    assert (out["stored"], out["held"], out["left"]) == (1, 1, 0)  # 41 kept as sender and subject
    held = ix.get("mail:41")
    assert held is not None and held.meta.get("partial") and held.title == "Hello"
    with pytest.raises(ToolError, match="won't ask Mail for it again until"):
        asyncio.run(mail_mac.message(41, mail))
    assert asyncio.run(mail_mac.bodies([41], mail, remember=False, stored=False)) == {}

    clock.t += mail_heal.SKIP_S + 1  # a day later it's read again
    mail.hangs = set()
    assert h.skipped() == set()
    mail.read.clear()
    asyncio.run(mailindex.sync_pass(ix, mail))
    assert mail.read == [[41]]
    filled = ix.get("mail:41")
    assert filled is not None and not filled.meta.get("partial") and "text 41" in filled.body


def test_the_culprit_comes_from_the_script_s_own_progress_lines() -> None:
    """mac._run keeps what a killed program wrote: the real osascript, on a script that
    addresses no app, says which id it's reading and then hangs."""
    script = 'function run(argv) { console.log("scout-progress reading 41"); delay(5); return "x"; }'

    async def go() -> mac.TimedOut:
        try:
            await mac._run("/usr/bin/osascript", "-l", "JavaScript", "-e", script, timeout=1.5)
        except mac.TimedOut as exc:
            return exc
        raise AssertionError("didn't time out")

    exc = asyncio.run(go())
    assert exc.progress == ["reading 41"] and str(exc) == "/usr/bin/osascript timed out"
    h = mail_heal.Health()
    h.timed_out(exc)
    assert h.suspects == {41}


def test_progress_lines_are_not_errors() -> None:
    script = 'function run(argv) { console.log("scout-progress reading 7"); throw new Error("boom"); }'
    with pytest.raises(ToolError) as err:
        asyncio.run(mac._run("/usr/bin/osascript", "-l", "JavaScript", "-e", script))
    assert "boom" in str(err.value) and "scout-progress" not in str(err.value)


def test_every_script_that_reads_text_says_which_message_first() -> None:
    for script in (mail_mac._READ, mail_mac._READ_MANY, mail_mac._FIND):
        at = script.index("m.content()")
        assert 'console.log("scout-progress reading "' in script[:at], script[:80]


# --- only downloaded text is asked for --------------------------------------------------


class Files:
    """messages_mac.mail's mail_local: which ids have their file on this Mac."""

    def __init__(self, here: set[int], fails: bool = False) -> None:
        self.here = here
        self.fails = fails
        self.asked: list[list[int]] = []

    async def __call__(
        self, op: str, args: dict[str, Any] | None = None, helper: Any = None
    ) -> dict[str, Any]:
        if op != "mail_local":
            raise ToolError("mail index: not under test")
        if self.fails:
            raise ToolError("mail index: bad_request: unknown op")
        ids = list((args or {})["ids"])
        self.asked.append(ids)
        return {"ok": True, "local": {str(i): "full" if i in self.here else "none" for i in ids}, "stores": 3}


def test_only_downloaded_messages_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make(tmp_path, monkeypatch)
    files = Files(here={40})
    monkeypatch.setattr(messages_mac, "mail", files)
    monkeypatch.setattr(mail_mac, "INDEX", mail_mac.Index())
    mail = StuckMail(hangs={41})  # 41 isn't downloaded: asking would hang Mail
    mail.stuck = False
    ix = index.default()
    out = asyncio.run(mailindex.sync_pass(ix, mail))
    assert mail.read == [[40]]
    assert (out["stored"], out["held"], out["left"]) == (1, 1, 0)
    held = ix.get("mail:41")
    assert held is not None and held.meta.get("partial")
    with pytest.raises(ToolError, match="hasn't downloaded"):
        asyncio.run(mail_mac.message(41, mail))
    # once Mail has downloaded it, the next pass reads it
    files.here.add(41)
    mail.hangs = set()
    mail.read.clear()
    asyncio.run(mailindex.sync_pass(ix, mail))
    assert mail.read == [[41]]


def test_reads_go_on_when_the_helper_cannot_tell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An older helper (no mail_local), no Full Disk Access, or a Mac whose files the helper
    finds none of: Mail is asked as before, guarded by the timeouts and the restart."""
    make(tmp_path, monkeypatch)
    for files in (Files(here=set(), fails=True), Files(here=set())):
        monkeypatch.setattr(messages_mac, "mail", files)
        monkeypatch.setattr(mail_mac, "INDEX", mail_mac.Index())
        mail = StuckMail(hangs=set())
        mail.stuck = False
        got = asyncio.run(mail_mac.bodies([40, 41], mail, remember=False, stored=False))
        assert sorted(got) == [40, 41] and mail.read == [[40, 41]]
