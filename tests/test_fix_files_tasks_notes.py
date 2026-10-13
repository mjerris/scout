"""Note and task file fixes: fields after blank lines or in capitals, context added by
paragraph, numbers never reused, odd line breaks kept, and atomic writes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from scout import notefiles, taskfiles


def test_fields_after_a_blank_line_and_in_capitals_are_read(tmp_path: Path) -> None:
    p = tmp_path / "0001-x.md"
    p.write_text("# Call the dealer\n\n- Status: done\n- OWNER: Pat\n\nAbout the awning.\n")
    title, fields, context = taskfiles.read(p)
    assert title == "Call the dealer"
    assert fields == {"status": "done", "owner": "Pat"}
    assert context == "About the awning."


def test_only_plain_keys_are_written(tmp_path: Path) -> None:
    text = taskfiles.render("T", {"Status": "open", "Due_Date": "2026-10-20", "bad key!": "x", "": "y"}, "")
    assert text == "# T\n- status: open\n- due_date: 2026-10-20\n"
    p = tmp_path / "0001-t.md"
    p.write_text(text)
    taskfiles.update(p, {"Owner": "Sam", "we-ird": "z"})
    _, fields, _ = taskfiles.read(p)
    assert fields == {"status": "open", "owner": "Sam", "due_date": "2026-10-20"}


def test_new_context_inside_old_context_is_still_added(tmp_path: Path) -> None:
    p = tmp_path / "0001-t.md"
    p.write_text(taskfiles.render("T", {"status": "open"}, "Pat wants the budget before Monday."))
    taskfiles.update(p, {}, "the budget")
    assert taskfiles.read(p)[2] == "Pat wants the budget before Monday.\n\nthe budget"
    taskfiles.update(p, {}, "the budget")  # the same paragraph again: not repeated
    assert taskfiles.read(p)[2].count("the budget") == 2


@pytest.mark.parametrize("mod", [notefiles, taskfiles])
def test_numbers_are_not_reused_after_the_highest_is_deleted(tmp_path: Path, mod: Any) -> None:
    def new(title: str) -> Path:
        if mod is notefiles:
            return notefiles.write(tmp_path, title, "body", 0)
        return taskfiles.write(tmp_path, title, {}, "")

    new("one")
    two = new("two")
    two.unlink()
    assert mod.number(new("three")) == 3


def test_odd_line_breaks_survive_a_round_trip(tmp_path: Path) -> None:
    body = "first\u2028still first\x85and\x0cthis"
    n = notefiles.write(tmp_path, "Odd", body, 0)
    assert notefiles.parse(n)[0]["body"] == body
    t = taskfiles.write(tmp_path, "Odd", {"owner": "me"}, body)
    assert taskfiles.read(t)[2] == body
    taskfiles.update(t, {"status": "done"})
    assert taskfiles.read(t)[2] == body


def test_files_are_written_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replaced: list[str] = []
    real = os.replace

    def spy(src: Any, dst: Any) -> None:
        replaced.append(Path(dst).name)
        real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    n = notefiles.write(tmp_path, "Note", "body", 0)
    t = taskfiles.write(tmp_path, "Task", {}, "")
    taskfiles.update(t, {"status": "done"})
    assert replaced.count(n.name) == 1 and replaced.count(t.name) == 2
    left = [p.name for d in (tmp_path / "notes", tmp_path / "tasks") for p in d.iterdir()]
    assert sorted(left) == sorted([n.name, t.name, ".next", ".next"])
