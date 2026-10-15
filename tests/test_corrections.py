"""The nightly pass that drafts behavior rules from the day's corrections (corrections.py),
with a fake local model and synthetic journal days."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any

from scout import behavior, corrections, journal
from scout.corrections import Candidates, Turn

DAY = dt.date(2026, 10, 14)


def at(hh: int, mm: int, ss: int = 0) -> float:
    return dt.datetime(DAY.year, DAY.month, DAY.day, hh, mm, ss).timestamp()


def write_day(root: Path, turns: list[tuple[float, str, str]]) -> None:
    for ts, q, a in turns:
        journal.append(root, journal.Entry(ts, "room", q, a), keep_private=False)


TICKETS = [
    (
        at(9, 0),
        "what's the status of ticket 61723",
        "Ticket sixty-one thousand seven hundred twenty-three is open.",
    ),
    (at(9, 1), "no, read the ticket number digit by digit", "Ticket 6 1 7 2 3 is open."),
    (at(9, 30), "what about ticket 4412", "Ticket four thousand four hundred twelve is waiting on Pat."),
    (
        at(9, 31),
        "I already told you, say ticket numbers one digit at a time",
        "Ticket 4 4 1 2 is waiting on Pat.",
    ),
    (at(11, 0), "what's the weather", "It's 61 degrees and sunny."),
]


class FakeModel:
    """Tier 1's LocalModel as the pass uses it: complete(system, user, ...)."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, system: str, user: str, max_tokens: int = 120, timeout_s: float = 20.0, prefix: str = ""
    ) -> str:
        self.calls.append((system, user))
        return self.out


def run[T](coro: Any) -> Any:
    return asyncio.run(coro)


def test_day_turns_reads_the_journal_day(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)
    turns = corrections.day_turns(tmp_path, DAY)
    assert [t.q for t in turns][:2] == [TICKETS[0][1], TICKETS[1][1]]
    assert turns[0].a.startswith("Ticket sixty-one") and turns[0].id.startswith("journal:2026-10-14T09:00")


def test_signals_find_corrections_repeats_and_barge_ins() -> None:
    t = [
        Turn("j1", at(9, 0), "what's on my calendar today", "You have three meetings."),
        Turn("j2", at(9, 1), "no, that's wrong, I have two", "Sorry, two meetings."),
        Turn("j3", at(10, 0), "how long is the drive to Burlington", "About two hours."),
        Turn("j4", at(10, 2), "how long is the drive to Burlington Vermont", "Two hours ten."),
        Turn("j5", at(12, 0), "tell me about the quarterly plan", "The plan has four parts..."),
        Turn("j6", at(12, 0, 20), "just the dates", "October and January."),
        Turn("j7", at(13, 0), "what's the weather", "Sunny."),
        Turn("j8", at(13, 5), "thanks", "You're welcome."),
    ]
    found = corrections.signals(t, barge_ins=[at(12, 0, 10)])
    assert [(s.turn.id, s.before.id if s.before else None, s.why) for s in found] == [
        ("j2", "j1", "correction"),
        ("j4", "j3", "repeat"),
        ("j6", "j5", "barge-in"),
    ]


def test_a_quiet_day_costs_no_model_call(tmp_path: Path) -> None:
    write_day(tmp_path, [(at(8, 0), "what's the weather", "Sunny."), (at(8, 5), "brief me", "Good morning.")])
    model = FakeModel("NONE")
    got = run(corrections.nightly(model, DAY, tmp_path, cands=Candidates(tmp_path / "c.json")))
    assert got == [] and model.calls == []


def test_the_pass_drafts_a_rule_with_the_turns_it_came_from(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)
    model = FakeModel(
        '{"about": "how I read ticket numbers", "rule": "Always read ticket numbers digit by digit", "items": [1, 2]}\n'
    )
    cands = Candidates(tmp_path / "c.json")
    got = run(
        corrections.nightly(
            model, DAY, tmp_path, cands=cands, rules=behavior.Behavior(tmp_path / "b.md", seed=False)
        )
    )
    assert len(model.calls) == 1
    system, user = model.calls[0]
    assert "at most 3" in system and "ticket number digit by digit" in user
    assert [c.rule for c in got] == ["Always read ticket numbers digit by digit"]
    turns = got[0].turns
    assert len(turns) == 2 and all(x.startswith("journal:2026-10-14T09:") for x in turns)
    assert corrections.offer_text(got[0]) == (
        "Twice yesterday you corrected how I read ticket numbers. Should I always read ticket numbers digit by digit?"
    )
    assert [c.rule for c in cands.pending()] == ["Always read ticket numbers digit by digit"]
    assert cands.last_pass() == DAY.isoformat()


def test_at_most_three_and_bad_lines_are_dropped(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)
    lines = [
        '{"about": "a", "rule": "Always say one", "items": [1]}',
        "not json",
        '{"about": "b", "rule": "Always say two things", "items": [9]}',  # no such item
        '{"about": "c", "rule": "", "items": [1]}',
        '{"about": "d", "rule": "Always say three things", "items": [2]}',
        '{"about": "e", "rule": "Always say four things", "items": [1]}',
        '{"about": "f", "rule": "Always say five things", "items": [2]}',
        '{"about": "g", "rule": "Always say six things", "items": [1]}',
    ]
    got = run(corrections.nightly(FakeModel("\n".join(lines)), DAY, tmp_path, cands=Candidates(tmp_path / "c.json"),
                                  rules=behavior.Behavior(tmp_path / "b.md", seed=False)))  # fmt: skip
    assert [c.rule for c in got] == ["Always say one", "Always say three things", "Always say four things"]


def test_a_declined_or_kept_rule_is_not_offered_again(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)
    out = '{"about": "how I read ticket numbers", "rule": "Always read ticket numbers digit by digit", "items": [1, 2]}'
    cands = Candidates(tmp_path / "c.json")
    rules = behavior.Behavior(tmp_path / "b.md", seed=False)
    first = run(corrections.nightly(FakeModel(out), DAY, tmp_path, cands=cands, rules=rules))
    cands.resolve(first[0].id, accepted=False)
    assert cands.pending() == []
    again = run(corrections.nightly(FakeModel(out), DAY, tmp_path, cands=cands, rules=rules))
    assert again == []  # declined: never again
    rules.add("Call me Mike")
    kept = run(corrections.nightly(
        FakeModel('{"about": "my name", "rule": "Call me Mike", "items": [1]}'), DAY, tmp_path, cands=cands, rules=rules
    ))  # fmt: skip
    assert kept == []  # already a rule


def test_a_model_that_fails_leaves_nothing_and_no_crash(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)

    class Broken(FakeModel):
        async def complete(self, *a: Any, **k: Any) -> str:
            raise TimeoutError

    assert run(corrections.nightly(Broken(""), DAY, tmp_path, cands=Candidates(tmp_path / "c.json"))) == []


def test_old_pending_candidates_expire(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    old = (dt.date.today() - dt.timedelta(days=corrections.OFFER_DAYS + 1)).isoformat()
    p.write_text(json.dumps({"candidates": [
        {"id": "x", "rule": "Always say one thing", "about": "a", "turns": [], "day": old, "status": "pending"}
    ]}))  # fmt: skip
    assert Candidates(p).pending() == []


def test_review_due_once_a_month(tmp_path: Path) -> None:
    c = Candidates(tmp_path / "c.json")
    assert c.review_due(dt.date(2026, 10, 15))
    c.reviewed(dt.date(2026, 10, 15))
    assert not c.review_due(dt.date(2026, 10, 31)) and c.review_due(dt.date(2026, 11, 1))


def test_recent_talk_reads_the_last_days(tmp_path: Path) -> None:
    write_day(tmp_path, TICKETS)
    talk = corrections.recent_talk(tmp_path, DAY + dt.timedelta(days=1), days=3)
    assert "what's the weather" in talk and any("Ticket 6 1 7 2 3" in t for t in talk)
