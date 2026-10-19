"""Mail.app stuck answering AppleScript: noticed, fixed by restarting Mail, and the
message that did it left alone for a day. Plain code; no model decides anything.

What it's for (2026-10-19, 11:02 to 15:56): one request for a message's text made Mail
fetch it from the server synchronously, on the thread that answers AppleScript
(NSScriptCommand -> -[MCMessage content] -> MCInvocationQueue runInvocationOnQueue-
Synchronously, waiting forever). Every later request, even `name of every account`,
queued behind it, so every mail pass timed out until Mail was restarted by hand.

    noticing     TIMEOUTS_BEFORE_PROBE Mail scripts in a row timed out (any caller: the
                 mail pass, mail_read_full, summaries) -> the probe, `count accounts` with
                 a short timeout. Mail answering it is slow, not stuck: nothing is done.
    the culprit  the scripts that read message text write `scout-progress reading <id>`
                 to stderr just before asking for it (mac.TimedOut keeps those lines), so
                 the id a timed-out read was on is known. Once the probe confirms Mail is
                 stuck, that id is skipped for SKIP_S (state/mail_health.json): after
                 the restart it can't hang Mail again, and a day later it's tried again.
    healing      Mail is quit: a quit Apple event sent without waiting for an answer
                 (graceful when Mail's event loop still runs), SIGTERM after QUIT_WAIT_S/2,
                 SIGKILL after QUIT_WAIT_S. An AppleScript `quit` that waits would hang
                 like everything else. Mail autosaves drafts as they're typed, so a
                 draft open in a window survives even the forced quit (minus the last
                 few seconds of typing). Then it's opened again hidden and in the
                 background (open -g -j -a Mail) and must answer the probe within
                 LAUNCH_WAIT_S; the mail pass is then tried again, once.
    limits       at most one restart per RESTART_EVERY_S (kept across Scout restarts).
                 A restart that didn't bring Mail back, or one needed again within the
                 hour, gives up: mail passes fail with STUCK (syncstate.NEEDS_USER, said
                 once a day by the pulse) without asking Mail anything but the probe,
                 until Mail answers it again.

Silent when it works, apart from an INFO line in the log."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from . import mac
from .config import DATA
from .mac import ToolError

log = logging.getLogger(__name__)

T = TypeVar("T")

PATH = DATA / "state" / "mail_health.json"
TIMEOUTS_BEFORE_PROBE = 2
PROBE_TIMEOUT_S = 10.0
RESTART_EVERY_S = 3600.0
QUIT_WAIT_S = 20.0
LAUNCH_WAIT_S = 90.0  # Mail opening a big inbox
SKIP_S = 24 * 3600.0
STUCK = (
    "Mail is stuck and isn't answering Scout, and restarting it didn't help. Quit Mail "
    "(Force Quit if it won't) and open it again."
)  # syncstate.NEEDS_USER matches this

# `with timeout` bounds Mail's answer inside the script; mac._run bounds the process.
PROBE = 'with timeout of 8 seconds\ntell application "Mail" to count accounts\nend timeout'
QUIT_NOW = 'ignoring application responses\ntell application "Mail" to quit\nend ignoring'
MAIL_BINARY = "/Contents/MacOS/Mail"  # the end of Mail.app's executable path


def _no_tests(what: str) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError(f"tests must not {what} the real Mail")


async def _probe() -> bool:
    """Whether Mail answers AppleScript: False only when it doesn't in time (a refusal,
    or Mail not set up, is an answer)."""
    try:
        await mac._run("/usr/bin/osascript", "-e", PROBE, timeout=PROBE_TIMEOUT_S)
    except mac.TimedOut:
        return False
    except ToolError:
        return True
    return True


@dataclass
class Procs:
    """How Mail's process is found, quit and opened. Tests make their own."""

    async def pid(self) -> int | None:
        _no_tests("look up")
        try:
            out = await mac._run("/usr/bin/pgrep", "-x", "Mail", timeout=5.0)
        except ToolError:  # pgrep exits 1 when there's none
            return None
        for line in out.split():
            if line.isdigit():
                try:
                    path = await mac._run("/bin/ps", "-o", "comm=", "-p", line, timeout=5.0)
                except ToolError:
                    continue
                if path.endswith("Mail.app" + MAIL_BINARY):
                    return int(line)
        return None

    async def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def ask_to_quit(self) -> None:
        _no_tests("quit")
        with contextlib.suppress(ToolError):
            await mac._run("/usr/bin/osascript", "-e", QUIT_NOW, timeout=5.0)

    def signal(self, pid: int, sig: int) -> None:
        _no_tests("signal")
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)

    async def launch(self) -> None:
        _no_tests("open")
        await mac._run("/usr/bin/open", "-g", "-j", "-a", "Mail", timeout=30.0)


@dataclass
class Health:
    """Mail's AppleScript health, for every caller in this process (HEALTH)."""

    probe: Callable[[], Awaitable[bool]] = _probe
    procs: Procs = field(default_factory=Procs)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    path: Path | None = None  # None: PATH (read each time, so tests can move it)
    timeouts: int = 0  # Mail scripts timed out in a row
    suspects: set[int] = field(default_factory=set)  # ids those were reading
    gave_up: bool = False  # a restart didn't fix it: STUCK until Mail answers again

    # --- what callers report ----------------------------------------------------------

    def answered(self) -> None:
        """A Mail script finished (whatever it said): Mail isn't stuck."""
        if self.gave_up:
            log.info("mail: Mail is answering AppleScript again")
        self.timeouts, self.gave_up = 0, False
        self.suspects.clear()

    def timed_out(self, exc: ToolError) -> None:
        """A Mail script timed out; the ids it was reading text for (mac.TimedOut)."""
        self.timeouts += 1
        for line in getattr(exc, "progress", []):
            what, _, mid = line.partition(" ")
            if what == "reading" and mid.isdigit():
                self.suspects.add(int(mid))

    @property
    def suspected(self) -> bool:
        return self.timeouts >= TIMEOUTS_BEFORE_PROBE

    # --- the skip list ----------------------------------------------------------------

    def _file(self) -> Path:
        return self.path if self.path is not None else PATH

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file().read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        p = self._file()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(p)
        except OSError as exc:
            log.warning("mail: health state not written: %s", exc)

    def skipped(self) -> set[int]:
        """Ids whose text isn't asked of Mail now: each hung Mail within SKIP_S."""
        now = self.clock()
        skip = self._load().get("skip") or {}
        return {int(k) for k, until in skip.items() if str(k).isdigit() and float(until) > now}

    def _skip(self, ids: set[int]) -> None:
        if not ids:
            return
        data = self._load()
        now = self.clock()
        skip = {k: v for k, v in (data.get("skip") or {}).items() if float(v) > now}  # expired go
        for i in ids:
            skip[str(i)] = now + SKIP_S
        data["skip"] = skip
        self._save(data)
        log.info("mail: not reading message %s from Mail for a day (Mail hung on it)", sorted(ids))

    # --- healing ----------------------------------------------------------------------

    async def check(self) -> None:
        """Before a pass: given up, Mail must answer the probe first (else STUCK); timeouts
        already counted (an interactive read) are healed now rather than after another."""
        if self.gave_up:
            if not await self.probe():
                raise ToolError(STUCK)
            self.answered()
        elif self.suspected:
            await self.heal()

    async def heal(self) -> bool:
        """Mail suspected stuck: True when it was restarted and answers now; False when it
        answered the probe (slow, not stuck). Raises ToolError(STUCK) when restarting
        didn't help or isn't allowed again yet."""
        if await self.probe():
            log.info("mail: %d Mail scripts timed out but Mail answers; not restarting it", self.timeouts)
            self.answered()
            return False
        self._skip(self.suspects)
        data = self._load()
        now = self.clock()
        last = data.get("restarted_at")
        if isinstance(last, int | float) and now - float(last) < RESTART_EVERY_S:
            log.warning("mail: Mail is stuck again within an hour of restarting it; giving up")
            self.gave_up = True
            raise ToolError(STUCK)
        data["restarted_at"] = now
        self._save(data)
        if await self._restart():
            log.info("mail: Mail was stuck answering AppleScript; restarted it")
            self.timeouts = 0
            self.suspects.clear()
            return True
        log.warning("mail: restarted Mail but it still isn't answering AppleScript; giving up")
        self.gave_up = True
        raise ToolError(STUCK)

    async def _restart(self) -> bool:
        p = self.procs
        pid = await p.pid()
        if pid is not None:
            await p.ask_to_quit()
            for sig, wait in ((signal.SIGTERM, QUIT_WAIT_S / 2), (signal.SIGKILL, QUIT_WAIT_S / 2)):
                if await self._gone(pid, wait):
                    break
                p.signal(pid, sig)
            if not await self._gone(pid, 5.0):
                return False
        await p.launch()
        until = self.clock() + LAUNCH_WAIT_S
        while not await self.probe():
            if self.clock() >= until:
                return False
            await self.sleep(2.0)
        return True

    async def _gone(self, pid: int, within: float) -> bool:
        until = self.clock() + within
        while await self.procs.alive(pid):
            if self.clock() >= until:
                return False
            await self.sleep(0.5)
        return True

    async def guarded(self, run: Callable[[], Awaitable[T]]) -> T:
        """A mail pass, healed: checked first (check), and after a failure with Mail
        suspected stuck, Mail restarted (heal) and the pass run once more."""
        await self.check()
        try:
            return await run()
        except ToolError:
            if not self.suspected or not await self.heal():
                raise
        return await run()


HEALTH = Health()
