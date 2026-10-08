"""Shared test setup."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

import claude_voice.assistant as assistant_mod


@pytest.fixture(autouse=True)
def _no_transcript_file(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests must not append to the real logs/transcript.jsonl."""
    null = logging.getLogger("claude_voice.transcript.test")
    null.addHandler(logging.NullHandler())
    null.propagate = False
    monkeypatch.setattr(assistant_mod, "_transcript_log", lambda: null)
    yield
