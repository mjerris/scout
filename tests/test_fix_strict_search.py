"""Strict privacy: local_search can't be used to probe mail text."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from scout import index, privacy, shared_tools


@pytest.fixture
def strict() -> Iterator[None]:
    before = privacy.current()
    privacy.configure("strict")
    yield
    privacy._current = before


def test_strict_search_shows_no_mail_hit_for_words_in_its_text(strict: None) -> None:
    store = index.default()
    store.put("mail:1", "mail", title="Lunch", body="the account password is hunter2", who="Sam", ts=1.0)
    store.put("note:1", "note", title="password ideas", body="use a manager", ts=1.0)
    out = asyncio.run(shared_tools._local_search({"query": "password"}))
    assert "Sam" not in out and "Lunch" not in out
    assert "password ideas" in out
