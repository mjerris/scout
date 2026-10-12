"""Per-action trust: everything that changes something asks; one action at a time can
be promoted (a "yes, always", or by voice) and taken back; email never; every decision
is logged; a one-time offer after five yeses in a row."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from scout import local_intents, trust


def test_promote_demote_and_never(tmp_path: Path) -> None:
    t = trust.Trust(tmp_path / "trust.json")
    assert t.tier("mcp__voice_app__reminder_add") == "ask"
    assert t.promote("reminder_add").startswith("Okay, I'll go ahead with adding reminders without asking")
    assert trust.Trust(tmp_path / "trust.json").tier("mcp__voice_app__reminder_add") == "auto"  # saved
    assert t.promote("mail_send") == "Sending email always asks; that one can't be turned off."
    assert t.tier("mail_send") == "ask"
    assert t.demote("reminder_add") == "Okay, I'll ask before adding reminders again."
    assert t.tier("reminder_add") == "ask"
    assert [e["how"] for e in t.log] == ["promoted", "demoted"]


def test_offer_after_five_yeses_in_a_row_once(tmp_path: Path) -> None:
    t = trust.Trust(tmp_path / "trust.json")
    offers = [t.record("reminder_add", "asked", ok) for ok in (True, True, False, True, True, True, True)]
    assert offers[:-1] == [None] * 6  # a no resets the streak
    offers.append(t.record("reminder_add", "asked", True))
    assert offers[-1] is not None and "5 times in a row" in offers[-1]
    assert t.record("reminder_add", "asked", True) is None  # said once
    assert t.record("mail_send", "asked", True) is None  # can't be promoted: no offer


@pytest.mark.parametrize(
    ("said", "reply", "tier"),
    [
        ("always allow adding reminders", "Okay, I'll go ahead with adding reminders without asking", "auto"),
        (
            "don't ask me before adding calendar events",
            "Okay, I'll go ahead with adding calendar events",
            "auto",
        ),
        ("stop asking about sending email", "Sending email always asks", "ask"),
    ],
)
def test_spoken_trust_commands(said: str, reply: str, tier: str) -> None:
    ctx = local_intents.Context(timers=None)
    out = asyncio.run(local_intents.answer(said, ctx))
    assert out is not None and out.startswith(reply)
    name = trust.action_for(said.split("before ")[-1].split("about ")[-1].split("allow ")[-1])
    assert name is not None and trust.default().tier(name) == tier


def test_asking_again_and_status() -> None:
    ctx = local_intents.Context(timers=None)
    asyncio.run(local_intents.answer("always allow marking reminders done", ctx))
    assert asyncio.run(local_intents.answer("what do you do without asking", ctx)) == (
        "I go ahead without asking for marking reminders done. Everything else asks first."
    )
    assert asyncio.run(local_intents.answer("ask me before marking reminders done", ctx)) == (
        "Okay, I'll ask before marking reminders done again."
    )


def test_a_promoted_action_runs_without_asking_for_other_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_routing import make, run

    from scout import shared_tools

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        asked: list[str] = []

        async def confirm(*a: Any, **k: Any) -> bool:
            asked.append(a[0])
            return True

        monkeypatch.setattr(r.a, "_confirm", confirm)
        ran: list[dict[str, Any]] = []

        async def act(args: dict[str, Any]) -> str:
            ran.append(args)
            return "Added."

        import dataclasses

        tool = dataclasses.replace(shared_tools.BY_NAME["reminder_add"], run=act)
        monkeypatch.setitem(shared_tools.BY_NAME, "reminder_add", tool)
        trust.default().promote("reminder_add")
        out = await r.a.run_shared_tool("desk#1", "reminder_add", {"title": "Milk"})
        return out, asked

    out, asked = run(go())
    assert out == {"status": "ok", "text": "Added."} and asked == []
    assert trust.default().log[-1]["how"] == "auto" and trust.default().log[-1]["by"] == "desk#1"
