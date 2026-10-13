"""Every osascript call ends its options with "--" before the arguments, so an argument
that starts with "-e" (an email subject is the sender's to choose) is data, never more
script. The real osascript is run only on harmless scripts that touch no app."""

from __future__ import annotations

import asyncio
import shutil
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from scout import mac, mail_mac, notes

HOSTILE = '-eApp=Application.currentApplication();console.log("INJECTED")'
_REAL_RUN = mac._run


@pytest.fixture(autouse=True)
def _no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only scripts that address no app may reach the real osascript."""

    async def run(*argv: str, timeout: float = 10.0) -> str:
        script = " ".join(argv)
        if "tell application" in script or "Application(" in script.replace(HOSTILE, ""):
            raise AssertionError("a test tried to script an app")
        return await _REAL_RUN(*argv, timeout=timeout)

    monkeypatch.setattr(mac, "_run", run)


@pytest.fixture
def argvs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []

    async def run(*argv: str, timeout: float = 10.0) -> str:
        seen.append(argv)
        return '{"error": "no such message"}' if "-l" in argv else "x-coredata://note/1"

    monkeypatch.setattr(mac, "_run", run)
    return seen


def _data_after_dashdash(argv: tuple[str, ...], first: str) -> None:
    assert argv[0] == "/usr/bin/osascript"
    assert "--" in argv and argv.index("--") == argv.index(first) - 1, argv


def test_every_osascript_call_stops_option_parsing_before_its_arguments(
    argvs: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = SimpleNamespace(notes=SimpleNamespace(apple=True, apple_folder=HOSTILE))
    monkeypatch.setattr(notes, "load", lambda: cfg)
    monkeypatch.setattr(notes, "_apple_off_until", 0.0)

    async def go() -> None:
        await mac.osascript("return 1", HOSTILE)
        await mail_mac._applescript("return 1", HOSTILE)
        await mail_mac._jxa("1", HOSTILE)
        await notes.add("water filter is due")
        await notes.delete({"id": 1})

    asyncio.run(go())
    assert len(argvs) == 5
    for argv in argvs[:4]:
        _data_after_dashdash(argv, HOSTILE)
    _data_after_dashdash(argvs[4], "x-coredata://note/1")  # the Apple Notes id from the add


def test_a_hostile_subject_reaches_find_as_an_argument(argvs: list[tuple[str, ...]]) -> None:
    """The scenario: an index row's subject is '-e...', and Mail can't find its id, so
    _find passes the subject to osascript."""
    row: dict[str, Any] = {"subject": HOSTILE, "date": "2026-10-01T14:00:00Z", "mailbox": "imap://x/INBOX"}
    with pytest.raises(mac.ToolError):
        asyncio.run(mail_mac._find(mail_mac._jxa, 41, row))
    _data_after_dashdash(argvs[0], HOSTILE)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("osascript"), reason="needs macOS")
def test_real_osascript_keeps_dash_e_arguments_as_data() -> None:
    """Harmless scripts only: no app is asked for anything."""

    async def go() -> tuple[str, str]:
        js = await mail_mac._jxa("function run(argv) { return argv[0] }", HOSTILE)
        apple = await mac.osascript("on run argv\nreturn item 1 of argv\nend run", "-e")
        return js, apple

    js, apple = asyncio.run(go())
    assert js == HOSTILE  # without "--" osascript runs it as script and argv[0] is gone
    assert apple == "-e"
