"""Scout's behavior rules (behavior.py): the store, what's injected, and the tier 0
phrases that make one. Synthetic data only."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scout import behavior
from scout.behavior import Behavior, Rule


def store(tmp_path: Path, seed: bool = False) -> Behavior:
    return Behavior(tmp_path / "memory" / "behavior.md", seed=seed)


# --- the file -------------------------------------------------------------------------


def test_a_rule_line_round_trips_with_its_fields() -> None:
    line = (
        "- Read ticket numbers digit by digit · from a fix · added 2026-10-15 · confirmed 2026-10-16"
        " · enforced: speech.speak_ids"
    )
    r = behavior.parse_line(line)
    assert r == Rule(
        "Read ticket numbers digit by digit", "from a fix", "2026-10-15", "2026-10-16", "speech.speak_ids"
    )
    assert behavior.render_line(r) == line


def test_a_bare_line_is_a_rule_and_other_lines_are_not() -> None:
    assert behavior.parse_line("- Call me Mike") == Rule("Call me Mike", "", "", "", "")
    assert behavior.parse_line("  - an indented bullet") is None
    assert behavior.parse_line("# heading") is None
    assert behavior.parse_line("-") is None
    told = behavior.parse_line(
        "- Call me Mike · told · added 2026-10-01 · turns: journal:2026-10-01T09:00:00"
    )
    assert told is not None and told.turns == ("journal:2026-10-01T09:00:00",)


def test_add_keeps_the_file_readable_and_an_edit_by_hand_is_read_next_time(tmp_path: Path) -> None:
    b = store(tmp_path)
    r, new = b.add("always say the weather in Fahrenheit.", origin="told")
    assert new and r.text == "Always say the weather in Fahrenheit" and r.added == r.confirmed
    text = b.path.read_text()
    assert text.startswith("# How Scout should behave")
    assert "- Always say the weather in Fahrenheit · told · added " in text
    b.path.write_text(text + "- Call me Mike · set by Mike\n# a note the user wrote\n")
    assert [x.text for x in b.rules()] == ["Always say the weather in Fahrenheit", "Call me Mike"]
    b.add("Never read emoji aloud")
    out = b.path.read_text()
    assert "# a note the user wrote" in out and "- Call me Mike · set by Mike" in out  # kept as written


def test_the_same_rule_again_confirms_it_instead_of_adding_a_copy(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.path.parent.mkdir(parents=True)
    b.path.write_text(
        "- Always say the weather in Fahrenheit · told · added 2026-01-02 · confirmed 2026-01-02\n"
    )
    r, new = b.add("always say the weather in fahrenheit")
    assert not new and len(b.rules()) == 1
    assert r.added == "2026-01-02" and r.confirmed == dt.date.today().isoformat()


def test_drop_takes_rules_with_every_word_and_never_an_enforced_one(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.add("Always say the weather in Fahrenheit")
    b.add("Read ticket numbers digit by digit", origin="from a fix", enforced="speech.speak_ids")
    assert [r.text for r in b.matching("weather")] == ["Always say the weather in Fahrenheit"]
    assert b.drop("ticket numbers") == []  # enforced in code: it stays listed
    assert [r.text for r in b.drop("weather fahrenheit")] == ["Always say the weather in Fahrenheit"]
    assert [r.text for r in b.rules()] == ["Read ticket numbers digit by digit"]


def test_rule_ids_are_stable_and_marked_behavior() -> None:
    a, b = behavior.rule_id("Call me Mike"), behavior.rule_id("call me mike.")
    assert a == b and a.startswith("behavior:")


def test_a_missing_file_is_seeded_with_the_fixes_enforced_in_code(tmp_path: Path) -> None:
    b = store(tmp_path, seed=True)
    rules = b.rules()
    assert len(rules) == len(behavior.SEED) >= 5
    assert all(r.enforced and r.origin == "from a fix" for r in rules)
    texts = " ".join(r.text.lower() for r in rules)
    for words in ("last hour", "digit by digit", "phone numbers", "slack", "calls"):
        assert words in texts, words
    assert b.prompt_block() == ""  # enforced rules are listed, not injected


# --- what's injected ------------------------------------------------------------------


def test_prompt_block_lists_rules_but_not_enforced_ones(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.add("Call me Mike")
    b.add("Read ticket numbers digit by digit", enforced="speech.speak_ids")
    block = b.prompt_block()
    assert "- Call me Mike" in block and "ticket" not in block
    assert block.startswith("# The user's rules for how you behave")
    assert len(block) < 400  # compact: it goes into every session


def test_past_the_cap_the_newest_are_used_and_the_prompt_says_so(tmp_path: Path) -> None:
    b = store(tmp_path)
    lines = [
        f"- Rule number {i} about thing{i} · told · added 2026-01-01 · confirmed 2026-01-{1 + i % 28:02d}"
        for i in range(45)
    ]
    b.path.parent.mkdir(parents=True)
    b.path.write_text("\n".join(lines) + "\n")
    shown, hidden = b.injected()
    assert len(shown) == behavior.MAX_INJECTED and hidden == 5
    assert "5 more rules aren't shown" in b.prompt_block()
    _, new = b.add("Never say goodbye twice")
    assert new and "review my rules" in b.over_cap_note()


def test_prompt_block_reads_the_file_fresh(tmp_path: Path) -> None:
    b = store(tmp_path)
    assert b.prompt_block() == ""
    sig = b.signature()
    b.path.parent.mkdir(parents=True)
    b.path.write_text("- Call me Mike\n")
    assert b.signature() != sig and "Call me Mike" in b.prompt_block()


def test_a_broken_file_never_breaks_a_session(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.path.parent.mkdir(parents=True)
    b.path.write_bytes(b"- caf\xe9 rules\n\x00\n")
    assert isinstance(b.prompt_block(), str)


def test_documents_are_indexed_as_behavior(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.add("Call me Mike")
    b.add("Read ticket numbers digit by digit", enforced="speech.speak_ids")
    docs = behavior.documents(b.path.read_text(), 0.0)
    assert {d["id"] for d in docs} == {
        behavior.rule_id("Call me Mike"),
        behavior.rule_id("Read ticket numbers digit by digit"),
    }
    enforced = next(d for d in docs if "ticket" in d["title"])
    assert enforced["meta"]["enforced"] == "speech.speak_ids"


# --- tier 0: "from now on ..." ----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "rule"),
    [
        ("From now on, call me Mike.", "Call me Mike"),
        ("Scout, from now on read ticket numbers digit by digit", "Read ticket numbers digit by digit"),
        ("call me Mike from now on", "Call me Mike"),
        ("Going forward, give me the weather in Celsius", "Give me the weather in Celsius"),
        ("Always read phone numbers in groups", "Always read phone numbers in groups"),
        ("hey scout, please always say the temperature first", "Always say the temperature first"),
        ("Never read emoji out loud", "Never read emoji out loud"),
        ("Remember to always tell me when a meeting moved", "Always tell me when a meeting moved"),
        (
            "make sure you always mention the location of a meeting",
            "Always mention the location of a meeting",
        ),
        ("Stop saying great question", "Don't say great question"),
        ("stop apologizing so much", "Don't apologize so much"),
        ("Don't read me the whole email anymore", "Don't read me the whole email"),
        ("Don't ever call me buddy", "Don't ever call me buddy"),
        ("do not summarize Slack threads again", "Don't summarize Slack threads"),
        ("remember not to read the signature of an email", "Don't read the signature of an email"),
        ("Can you always read the time in 24 hour format?", "Always read the time in 24 hour format"),
    ],
)
def test_told_finds_a_standing_rule(said: str, rule: str) -> None:
    assert behavior.told(said) == rule


@pytest.mark.parametrize(
    "said",
    [
        "don't forget the milk",
        "don't forget the milk again",
        "Don't forget to call mom",
        "don't remind me about that",
        "don't do that again",
        "don't worry about it",
        "never mind",
        "Nevermind",
        "Never heard of it",
        "always the same thing",
        "I always forget my keys",
        "do you always do that?",
        "why do you always say that",
        "what did I tell you to always do",
        "stop",
        "stop the timer",
        "stop playing music",
        "Scout, stop",
        "always allow adding reminders",
        "always ask me before sending email",
        "stop asking me about reminders",
        "don't ask me before adding events",
        "remember that my dentist is Dr. Lee",
        "remember to call Sam",
        "from now on",
        "never",
        "don't",
    ],
)
def test_told_leaves_questions_one_offs_and_other_commands(said: str) -> None:
    assert behavior.told(said) is None


def test_says_rule_tells_a_rule_turn_from_others() -> None:
    assert behavior.says_rule("from now on call me Mike")
    assert not behavior.says_rule("what are my rules")


def test_review_request_phrases() -> None:
    for said in ("review my rules", "Scout, let's review my rules", "go over my rules"):
        assert behavior.is_review(said), said
    for said in ("what are my rules", "review my notes"):
        assert not behavior.is_review(said), said
    assert behavior.is_listing("what are my rules") and behavior.is_listing("list my rules")


# --- upkeep -----------------------------------------------------------------------------


def test_review_finds_duplicates_unused_and_enforced(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.path.parent.mkdir(parents=True)
    b.path.write_text(
        "- Always say the weather in Fahrenheit · told · added 2026-01-01 · confirmed 2026-01-01\n"
        "- Say the weather in Fahrenheit always · told · added 2026-02-01 · confirmed 2026-02-01\n"
        "- Never mention the trailer hitch · told · added 2026-01-01 · confirmed 2026-01-01\n"
        "- Call me Mike · told · added 2026-10-10 · confirmed 2026-10-10\n"
        "- Read ticket numbers digit by digit · from a fix · added 2026-10-15 · enforced: speech.speak_ids\n"
    )
    talk = ["what's the weather", "it's 50 degrees Fahrenheit", "hi Mike"]
    rv = behavior.review(b.rules(), talk, today=dt.date(2026, 10, 15))
    assert [(a.text, d.text) for a, d in rv.duplicates] == [
        ("Always say the weather in Fahrenheit", "Say the weather in Fahrenheit always")
    ]
    assert [r.text for r in rv.unused] == ["Never mention the trailer hitch"]  # old and never came up
    assert [r.text for r in rv.enforced] == ["Read ticket numbers digit by digit"]


def test_merge_keeps_one_and_confirms_it(tmp_path: Path) -> None:
    b = store(tmp_path)
    a, _ = b.add("Always say the weather in Fahrenheit")
    d, _ = b.add("Say the weather in Fahrenheit always")
    b.merge(a.id, d.id)
    assert [r.text for r in b.rules()] == ["Always say the weather in Fahrenheit"]


def test_listing_text_names_origins_and_enforced(tmp_path: Path) -> None:
    b = store(tmp_path)
    b.add("Call me Mike")
    b.add("Read ticket numbers digit by digit", origin="from a fix", enforced="speech.speak_ids")
    text = behavior.listing_text(b)
    assert "Call me Mike (told" in text and "enforced in code: speech.speak_ids" in text
    assert behavior.rule_id("Call me Mike") in text  # its ref, for links


def test_spoken_listing(tmp_path: Path) -> None:
    b = store(tmp_path)
    assert behavior.spoken_listing(b) == "You haven't set any rules for me yet."
    b.add("Call me Mike")
    b.add("Read ticket numbers digit by digit", enforced="speech.speak_ids")
    said = behavior.spoken_listing(b)
    assert said.startswith("You have 2 rules") and "call you Mike" in said and "1 is kept by my code" in said
