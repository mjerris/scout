"""Review fixes in tier 1: action phrasings the guard missed, and local email
summaries reached through mail_search / mail_recent stay private."""

import asyncio
from typing import Any

import pytest
from test_privacy import FakeModel
from test_routing import make

from scout import privacy, tier1


@pytest.mark.parametrize(
    "request_text",
    [
        "Email Bob that I'm running late",
        "Clear my calendar tomorrow afternoon",
        "Let Sarah know I'm free at 3",
        "Unsubscribe me from the Rover emails",
        "RSVP yes to the party on my calendar",
        "Block off Friday afternoon on my calendar",
        "Forwarding the email from Bob to Alice",
    ],
)
def test_action_phrasings_go_to_claude(request_text: str) -> None:
    assert tier1.guard(request_text)


@pytest.mark.parametrize(
    "request_text",
    ["email from Bob", "any email about the trip", "messages from Sam today", "anything from Sam"],
)
def test_lookups_still_reach_the_model(request_text: str) -> None:
    assert tier1.guard(request_text) is None


@pytest.mark.parametrize(
    "decision",
    [
        {"tool": "mail_search", "args": {"query": "Sam"}},
        {"tool": "mail_recent", "args": {"unread_only": True}},
    ],
)
def test_strict_mode_marks_search_summaries_private(
    decision: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Decider:
        ready = True

        async def decide(self, request: str, now: Any) -> dict[str, Any] | None:
            return dict(decision)

    async def summarize(args: dict[str, Any], now: Any, summaries: Any) -> str:
        return "Today, Sam asks if you are free for lunch Friday at noon."

    monkeypatch.setattr(tier1, "summarize", summarize)

    async def go() -> tuple[Any, str]:
        r: Any = make()
        r.a.privacy = privacy.configure("strict", FakeModel())
        r.a.tier1 = Decider()
        try:
            await r.a._answer_locally("anything from Sam")
            return r.a.recent_local[-1], await r.a._with_context("and what else")
        finally:
            privacy.configure("balanced")

    last, context = asyncio.run(go())
    assert last[3] is True  # private: withheld from Claude in strict mode
    assert last[2] not in context
