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
