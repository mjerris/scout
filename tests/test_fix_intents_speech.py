"""Review fixes: a spoken confirmation that asks for a change or a delay is not a yes."""

import pytest

from scout.speech import parse_answer, parse_yes_no


@pytest.mark.parametrize(
    "text",
    [
        "Correct the typo first.",
        "Right, fix the date.",
        "Okay, change it to 3 pm.",
        "Fine, but make it shorter.",
        "Yes, but change the subject first.",
        "Yes, but only to Alice.",
        "Sure, after lunch.",
        "Yeah, send it Monday.",
        "Go back.",
        "yes but change the time",
        "correct the subject first",
        "only after lunch",
        "go after the meeting",
    ],
)
def test_a_reply_that_changes_or_delays_the_action_is_not_approval(text: str) -> None:
    assert parse_answer(text) is not True
    assert parse_yes_no(text) is not True


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("yes", True),
        ("yes please", True),
        ("go ahead", True),
        ("do it", True),
        ("yes always", "always"),
        ("Yes, but always ask.", True),
        ("Yes, run the tests.", True),
    ],
)
def test_plain_approvals_still_count(text: str, answer: bool | str) -> None:
    assert parse_answer(text) == answer
