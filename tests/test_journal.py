"""The markdown journal (journal.py) and facts.md: what's written, what's read back,
and that edits to the files reach search."""

import datetime as dt
import json
import time
from pathlib import Path

import pytest

from scout import index, journal, memory_service
from scout.memory import Memory, parse_facts, render_facts


def at(h: int, m: int, s: int = 0, day: int = 13) -> float:
    return dt.datetime(2026, 10, day, h, m, s).timestamp()


def test_an_entry_is_written_and_read_back(tmp_path: Path) -> None:
    e = journal.Entry(at(11, 28, 7), "phone", "What's on my calendar\ntomorrow?", "Standup at 9.",
                      [("mcp__scout__calendar_events", {"start": "2026-10-14", "end": "2026-10-15", "x": None})])  # fmt: skip
    p = journal.append(tmp_path, e, keep_private=False)
    assert p == tmp_path / "journal" / "2026" / "10" / "2026-10-13.md"
    text = p.read_text()
    assert text.startswith("# Tuesday, October 13, 2026\n\n## 11:28:07 · phone\n")
    assert "**Q:** What's on my calendar tomorrow?\n**A:** Standup at 9.\n" in text
    assert "tools: calendar_events(start=2026-10-14, end=2026-10-15)\n" in text
    [item] = journal.parse(p)
    assert item["id"] == "journal:2026-10-13T11:28:07"
    assert item["title"] == "What's on my calendar tomorrow?"
    assert item["body"] == (
        "What's on my calendar tomorrow?\nScout: Standup at 9.\ntools: calendar_events(start=2026-10-14, end=2026-10-15)"
    )
    assert item["who"] == "phone" and item["ts"] == at(11, 28, 7)


def test_an_answer_from_mail_is_kept_only_in_open_mode(tmp_path: Path) -> None:
    e = journal.Entry(at(9, 0), "room", "Any mail from Pat?", "Pat says the meeting moved to 3.",
                      [("mail_read", {"id": "1234"})], private=True)  # fmt: skip
    journal.append(tmp_path / "a", e, keep_private=False)
    journal.append(tmp_path / "b", e, keep_private=True)
    hidden = journal.path_for(tmp_path / "a", e.ts).read_text()
    assert "meeting moved" not in hidden and journal.PRIVATE_ANSWER in hidden
    assert "tools: mail_read(id=1234)" in hidden  # the reference stays
    assert "meeting moved" in journal.path_for(tmp_path / "b", e.ts).read_text()


def test_long_tool_arguments_are_cut() -> None:
    line = journal.tools_line([("web_search", {"query": "x" * 200})])
    assert line.startswith("web_search(query=x") and line.endswith("…)") and len(line) < 110


def test_same_second_entries_get_distinct_ids(tmp_path: Path) -> None:
    for q in ("one", "two"):
        journal.append(tmp_path, journal.Entry(at(8, 0), "room", q, "ok"), False)
    ids = [i["id"] for i in journal.parse(journal.path_for(tmp_path, at(8, 0)))]
    assert ids == ["journal:2026-10-13T08:00:00", "journal:2026-10-13T08:00:00-2"]


def test_a_hand_written_entry_without_seconds_parses(tmp_path: Path) -> None:
    p = tmp_path / "journal" / "2026" / "10" / "2026-10-12.md"
    p.parent.mkdir(parents=True)
    p.write_text("# Monday\n\n## 17:05 · note\n**Q:** Tire pressure?\n**A:** 80 psi rear.\n")
    [item] = journal.parse(p)
    assert item["id"] == "journal:2026-10-12T17:05:00" and item["who"] == "note"


def test_the_index_follows_the_files(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", None)
    ix.journal_root = tmp_path / "memory"
    ix.journal_add(at(10, 0), "room", "Where is the spare key?", "In the blue tackle box.")
    ix.journal_add(at(10, 5), "room", "What psi for the rear tires?", "80 psi cold.")
    assert [h.id for h in ix.search("tackle box", semantic=False)] == ["journal:2026-10-13T10:00:00"]
    p = journal.path_for(ix.journal_root, at(10, 0))
    # The user edits one answer and deletes the other entry by hand.
    text = p.read_text().replace("In the blue tackle box.", "In the glovebox now.")
    text = text[: text.index("## 10:05:00")]
    p.write_text(text)
    assert ix.sync_files() == 2
    assert [h.id for h in ix.search("glovebox", semantic=False)] == ["journal:2026-10-13T10:00:00"]
    assert ix.search("tackle box", semantic=False) == []
    assert ix.search("psi", semantic=False) == []
    assert ix.sync_files() == 0  # nothing changed since
    p.unlink()  # a whole day deleted
    assert ix.sync_files() == 1 and ix.count("turn") == 0


def test_journal_needs_a_root(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", None)
    with pytest.raises(RuntimeError):
        ix.journal_add(time.time(), "room", "q", "a")


def test_old_indexed_turns_move_into_the_journal(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "m.db", None)
    ix.journal_root = tmp_path / "memory"
    ix.put("turn:1.000", "turn", title="Is Starlink up?", body="Is Starlink up?\nScout: Yes, online.",
           ts=at(7, 30), meta={"by": "phone claude", "private": False})  # fmt: skip
    assert memory_service.migrate_turns(ix) == 1
    assert ix.get("turn:1.000") is None
    [h] = ix.search("starlink", semantic=False)
    assert h.id == "journal:2026-10-13T07:30:00" and h.who == "phone"
    assert "**A:** Yes, online." in journal.path_for(ix.journal_root, at(7, 30)).read_text()
    assert memory_service.migrate_turns(ix) == 0


def test_facts_round_trip_through_markdown() -> None:
    facts = [{"text": "Pat is my manager", "ts": at(9, 15)}, {"text": "The RV is 36 feet", "ts": 0}]
    text = render_facts(facts)
    assert "- Pat is my manager · 2026-10-13 09:15\n- The RV is 36 feet\n" in text
    back = parse_facts(text, default_ts=5.0)
    assert back == [{"text": "Pat is my manager", "ts": at(9, 15)}, {"text": "The RV is 36 feet", "ts": 5.0}]


def test_facts_md_edits_are_picked_up(tmp_path: Path) -> None:
    mem = Memory(tmp_path / "facts.md")
    mem.add("Pat is my manager")
    seen: list[int] = []
    mem.on_change = lambda: seen.append(len(mem.facts))
    p = tmp_path / "facts.md"
    p.write_text(p.read_text() + "- My dentist is Dr. Okafor\n")
    import os

    os.utime(p, (time.time() + 5, time.time() + 5))  # a later edit, as the user's editor would
    assert mem.matching("dentist") == ["My dentist is Dr. Okafor"]
    assert seen == [2]


def test_old_facts_json_moves_to_markdown(tmp_path: Path) -> None:
    old = tmp_path / "memory.json"
    old.write_text(json.dumps({"facts": [{"text": "Sam likes tea", "ts": at(8, 0)}]}))
    mem = Memory(tmp_path / "facts.md", legacy=old)
    assert [f["text"] for f in mem.facts] == ["Sam likes tea"]
    assert "- Sam likes tea · 2026-10-13 08:00" in (tmp_path / "facts.md").read_text()
    assert not old.exists() and (tmp_path / "memory.json.moved").exists()
