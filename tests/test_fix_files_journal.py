"""Journal fixes: a hand edit without a final newline, empty Q/A, private entries, and
times in the repeated hour when clocks fall back."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from scout import journal


def at(h: int, m: int, s: int = 0, day: int = 13) -> float:
    return dt.datetime(2026, 10, day, h, m, s).timestamp()


def test_an_entry_after_a_hand_edit_without_final_newline_is_kept(tmp_path: Path) -> None:
    p = journal.append(tmp_path, journal.Entry(at(8, 0), "room", "first", "one"), False)
    p.write_text(p.read_text().rstrip("\n") + "\nA line typed by hand")
    journal.append(tmp_path, journal.Entry(at(9, 0), "room", "second", "two"), False)
    assert [i["title"] for i in journal.parse(p)] == ["first", "second"]


def test_an_empty_question_or_answer_does_not_read_the_next_line(tmp_path: Path) -> None:
    p = journal.append(tmp_path, journal.Entry(at(8, 0), "room", "", "", [("weather", {})]), False)
    text = p.read_text()
    assert "**Q:** (none)\n**A:** (none)\n" in text
    p.write_text(
        "# Day\n\n## 08:00:00 · room\n**Q:**\n**A:**\ntools:\n\n## 09:00:00 · room\n**Q:** b\n**A:** c\n"
    )
    first, second = journal.parse(p)
    assert "**A:**" not in first["title"] and "**A:**" not in first["body"]
    assert "Scout: tools" not in first["body"]
    assert second["title"] == "b"


def test_a_private_entry_keeps_only_ids_in_the_tools_line(tmp_path: Path) -> None:
    e = journal.Entry(at(9, 0), "room", "Draft a reply to Pat", "Done.",
                      [("mail_draft", {"mail_id": "m-77", "body": "Secret plans for Friday", "count": 2}),
                       ("mail_search", {"query": "pat budget", "ids": ["a1", "a2"]})],
                      private=True)  # fmt: skip
    text = journal.append(tmp_path, e, keep_private=False).read_text()
    assert "Secret plans" not in text and "pat budget" not in text
    assert "mail_draft(mail_id=m-77, count=2)" in text
    assert 'mail_search(ids=["a1", "a2"])' in text
    kept = journal.append(tmp_path / "open", e, keep_private=True).read_text()
    assert "Secret plans" in kept


def test_private_entries_are_marked_in_the_heading_and_the_index(tmp_path: Path) -> None:
    e = journal.Entry(at(9, 0), "phone", "Any mail from Pat?", "Yes.", private=True)
    p = journal.append(tmp_path, e, keep_private=True)
    journal.append(tmp_path, journal.Entry(at(9, 5), "room", "Weather?", "Sunny."), False)
    assert "## 09:00:00 · phone · private\n" in p.read_text()
    first, second = journal.parse(p)
    assert first["who"] == "phone" and first["meta"]["private"] is True
    assert second["who"] == "room" and not second["meta"].get("private")


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("new_york")
def test_the_repeated_hour_when_clocks_fall_back(tmp_path: Path) -> None:
    edt = dt.datetime(2026, 11, 1, 1, 30).timestamp()
    est = dt.datetime(2026, 11, 1, 1, 30, fold=1).timestamp()
    assert est - edt == 3600
    for ts, q in ((edt, "before"), (est, "after")):
        journal.append(tmp_path, journal.Entry(ts, "room", q, "ok"), False)
    items = journal.parse(journal.path_for(tmp_path, edt))
    assert [(i["title"], i["ts"]) for i in items] == [("before", edt), ("after", est)]
