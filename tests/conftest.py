"""Shared test setup."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

# Tests get their own Scout data folder (config, state, logs), never the real one in
# ~/Library/Application Support/Scout. Set before scout is imported: DATA is read once.
_TEST_HOME = Path(__file__).resolve().parents[1] / ".tmp" / "test-home" / str(os.getpid())
os.environ["SCOUT_HOME"] = str(_TEST_HOME)

import pytest  # noqa: E402

import scout.assistant as assistant_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_transcript_file(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests must not append to the real logs/transcript.jsonl."""
    null = logging.getLogger("scout.transcript.test")
    null.addHandler(logging.NullHandler())
    null.propagate = False
    monkeypatch.setattr(assistant_mod, "_transcript_log", lambda: null)
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    import shutil

    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_user_claude_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The room's options never pick up this machine's ~/.claude/settings.json in tests."""
    import scout.brain as brain_mod

    monkeypatch.setattr(brain_mod, "CLAUDE_SETTINGS", tmp_path / "no-claude-settings.json")


@pytest.fixture(autouse=True)
def _full_scan_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that fake Mail's full-scan script get it; tests/test_mail.py covers the range path."""
    import scout.mail_mac as mail_mac

    monkeypatch.setattr(mail_mac, "USE_RANGES", False)


@pytest.fixture(autouse=True)
def _own_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Each test gets a fresh local store (index.py), with no embedding model, and the
    Mac's Reminders as the list backend unless a test opts into Scout's own lists."""
    import scout.index as index_mod
    import scout.reminders_mac as reminders_mac

    import scout.appdb as appdb_mod
    import scout.memory as memory_mod
    import scout.memory_client as memory_client_mod

    ix = index_mod.Index(tmp_path / "memory.db", None)
    ix.journal_root = tmp_path / "memory"
    monkeypatch.setattr(index_mod, "_index", ix)
    monkeypatch.setattr(appdb_mod, "_db", appdb_mod.AppDB(tmp_path / "scout.db"))
    monkeypatch.setattr(memory_client_mod, "_memory", memory_mod.Memory(tmp_path / "memory.json"))
    import scout.trust as trust_mod

    monkeypatch.setattr(trust_mod, "_trust", trust_mod.Trust(tmp_path / "trust.json"))
    import scout.ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(reminders_mac, "_backend", lambda: "reminders")
    monkeypatch.setattr(reminders_mac, "_fallback_until", 0.0)
    yield
    ix.close()


@pytest.fixture(autouse=True)
def _fresh_access_checks() -> None:
    """Calendar and Reminders remember access for minutes: each test starts asking anew."""
    import scout.calendar_mac as calendar_mac
    import scout.reminders_mac as reminders_mac

    calendar_mac._cal_access.clear()
    reminders_mac._access.clear()


@pytest.fixture(autouse=True)
def _no_real_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may drive a real app (Notes, Mail, Reminders...) through osascript. Tests
    fake mac._run; one that forgets fails here instead of changing the user's data."""
    import scout.mac as mac

    real = mac._run

    async def guarded(*argv: str, timeout: float = 10.0) -> str:
        if argv and argv[0].endswith("osascript") and any("application" in a for a in argv):
            raise AssertionError(f"test reached a real app through osascript: {argv[:3]}")
        return await real(*argv, timeout=timeout)

    monkeypatch.setattr(mac, "_run", guarded)


@pytest.fixture(autouse=True)
def _own_sync_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Each test has its own sync status file (syncstate.py)."""
    import scout.syncstate as syncstate_mod

    monkeypatch.setattr(syncstate_mod, "PATH", tmp_path / "sync_status.json")
