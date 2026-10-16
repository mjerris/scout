"""Send confirmations: a short read-back, answers said over it, and typed approval.

Live (2026-10-16): a session's slack_send for a ~2,000-character DM was read back for
far too long, and the user's "Yeah" and "Oh, yes" said over it were dropped as "said
before the question"; the send came back as not answered.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from test_routing import Rig, _shared, hear, make, run

from scout import brain, shared_tools, speech
from scout.config import Config

LONG = "Hey Joao, I went through the bot token setup this morning and wrote down what I found. " + " ".join(
    f"Point {i}: the workspace admin tools moved again and the scopes need a fresh look." for i in range(20)
)


# --- 1. the read-back is short -----------------------------------------------------------


def test_a_short_message_is_read_back_whole() -> None:
    assert speech.read_back("On my way") == "It says: On my way."
    assert speech.read_back("") == "It's empty."


def test_a_long_message_is_read_back_by_how_it_starts_and_its_length() -> None:
    said = speech.read_back(LONG)
    n = len(LONG.split())
    assert said.startswith("It starts: Hey Joao, I went through the bot token setup this morning and")
    assert said.endswith(f"It's about {round(n / 50) * 50} words.")
    assert len(said.split()) <= 25


def test_a_short_first_sentence_is_said_whole() -> None:
    text = "Running late today. " + "More words about the plan for later on. " * 10
    assert speech.read_back(text).startswith("It starts: Running late today. It's about")


def test_word_counts_are_said_in_round_numbers() -> None:
    assert speech.about_words(23) == "about 20"
    assert speech.about_words(342) == "about 350"
    assert speech.about_words(2345) == "about 2,300"


def test_a_long_email_is_not_read_back_in_full() -> None:
    spoken, detail = brain.describe_tool(
        "mail_send", {"to": ["sam@example.com"], "subject": "Bots", "body": LONG}
    )
    question, rest = brain.split_question(spoken)
    assert question == "Send email to sam@example.com, subject Bots?"
    assert rest.startswith("It starts: Hey Joao") and "words." in rest
    assert len(spoken) < 250
    assert LONG in detail  # the page still shows all of it


# --- 2. answers said over the read-back ----------------------------------------------------


def _reading(r: Rig, text: str) -> None:
    """The read-back is playing now."""
    now = time.monotonic()
    r.spk.played = [(now - 0.1, now + 30.0, text)]
    r.spk.busy = True


MAIL = {"to": ["sam@example.com"], "subject": "Bots", "body": LONG}


def _ask(r: Rig, args: dict[str, Any]) -> asyncio.Task[dict[str, Any]]:
    r.spk.idle_delay = 0.2  # each part takes this long to play
    return asyncio.create_task(r.a.run_shared_tool("proj#42", "mail_send", args))


def test_a_yes_said_over_the_read_back_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[dict[str, Any], list[str], int]:
        r = make()
        r.a.echo_cancelled = True
        task = _ask(r, MAIL)
        await asyncio.sleep(0.3)  # the question has been asked; the read-back is playing
        _reading(r, speech.read_back(LONG))
        await hear(r, "Yeah.", echo=True, started=time.monotonic())
        return await asyncio.wait_for(task, 2), r.spk.said, r.spk.stopped

    result, said, stopped = run(go())
    assert result == {"status": "ok", "text": "done"} and len(ran) == 1
    assert said[0] == "From proj: Send email to sam@example.com, subject Bots?"
    assert said[1].startswith("It starts: Hey Joao")
    assert stopped >= 1  # the rest of the read-back isn't read once answered


def test_a_no_said_over_the_read_back_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> dict[str, Any]:
        r = make()
        r.a.echo_cancelled = True
        task = _ask(r, MAIL)
        await asyncio.sleep(0.3)
        _reading(r, speech.read_back(LONG))
        await hear(r, "No, don't send it.", echo=True, started=time.monotonic())
        return await asyncio.wait_for(task, 2)

    assert run(go())["status"] == "declined" and ran == []


def test_our_own_words_over_the_read_back_do_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message itself says "yes": its echo must not approve it."""
    ran = _shared(monkeypatch, "mail_send")
    body = "Yes, sounds good. " + "I'll bring the cables and the spare router tomorrow morning. " * 3

    async def go() -> tuple[bool, list[str]]:
        r = make()
        r.a.echo_cancelled = True
        task = _ask(r, {**MAIL, "body": body})
        await asyncio.sleep(0.3)
        _reading(r, speech.read_back(body))
        await hear(r, "Yes, sounds good.", echo=True, started=time.monotonic())
        await asyncio.sleep(0.05)
        done = task.done()
        task.cancel()
        return done, r.spk.said

    done, said = run(go())
    assert not done and ran == []
    assert "Sorry, was that a yes or a no?" not in said


def test_talk_over_the_read_back_that_is_no_answer_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[bool, list[str]]:
        r = make()
        r.a.echo_cancelled = True
        task = _ask(r, MAIL)
        await asyncio.sleep(0.3)
        _reading(r, speech.read_back(LONG))
        await hear(r, "Where do I get the bot token?", echo=True, started=time.monotonic())
        await asyncio.sleep(0.05)
        done = task.done()
        task.cancel()
        return done, r.spk.said

    done, said = run(go())
    assert not done and ran == []
    assert "Sorry, was that a yes or a no?" not in said  # not asked again over the read-back


def test_a_yes_before_the_recipient_was_named_still_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> bool:
        r = make()
        r.a.echo_cancelled = True
        task = _ask(r, MAIL)
        await asyncio.sleep(0.05)  # still saying "From proj: Send email to ..."
        await hear(r, "Yes.", started=time.monotonic())
        await asyncio.sleep(0.05)
        done = task.done()
        task.cancel()
        return done

    assert run(go()) is False and ran == []


# --- 4. typed approval ---------------------------------------------------------------------


def _typed(r: Rig, on: bool) -> None:
    r.a.cfg.confirm.typed = on


def test_typed_approval_is_off_by_default() -> None:
    assert Config().confirm.typed is False


def test_typed_approval_sends_without_the_read_back(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        _typed(r, True)
        result = await r.a.run_shared_tool("proj#42", "mail_send", {**MAIL, "approved": "typed"})
        return result, r.spk.said

    with caplog.at_level("INFO"):
        result, said = run(go())
    assert result == {"status": "ok", "text": "done"}
    assert ran == [MAIL]  # the approval isn't passed on to the tool
    assert said == ["Sent to sam@example.com."]
    assert any("approved by typing" in m for m in caplog.messages)


@pytest.mark.parametrize(
    ("name", "args", "line"),
    [
        ("slack_send", {"draft": "s1"}, "Sent to Sam Roe."),
        ("message_send", {"draft": "m1"}, "Sent to Pat Doe."),
        ("call_start", {"call": "c1"}, "Calling Pat Doe."),
    ],
)
def test_typed_approval_for_each_send_tool(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any], line: str
) -> None:
    ran = _shared(monkeypatch, name)
    who = line.removeprefix("Sent to ").removeprefix("Calling ").rstrip(".")
    monkeypatch.setattr(shared_tools, "recipient", lambda n, a: who)

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        _typed(r, True)
        return await r.a.run_shared_tool("proj#42", name, {**args, "approved": "typed"}), r.spk.said

    result, said = run(go())
    assert result["status"] == "ok" and ran == [args] and said == [line]


def test_without_the_config_typed_approval_still_asks_by_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        r.spk.idle_delay = 0.05
        task = asyncio.create_task(r.a.run_shared_tool("proj#42", "mail_send", {**MAIL, "approved": "typed"}))
        await asyncio.sleep(0.12)
        await hear(r, "no", started=time.monotonic())
        return await asyncio.wait_for(task, 2), r.spk.said

    result, said = run(go())
    assert result["status"] == "declined" and ran == []
    assert said[0].startswith("From proj: Send email to sam@example.com")


def test_typed_approval_is_only_for_the_send_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "calendar_create_event")
    event = {"title": "Dentist", "start": "2026-10-09T15:00", "approved": "typed"}

    async def go() -> dict[str, Any]:
        r = make()
        _typed(r, True)
        return await r.a.run_shared_tool("proj#42", "calendar_create_event", event)

    result = run(go())
    assert result["status"] == "error" and "approved" in result["error"] and ran == []


def test_typed_approval_is_not_for_the_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.assistant import PHONE_AGENT

    ran = _shared(monkeypatch, "mail_send")

    async def go() -> dict[str, Any]:
        r = make()
        _typed(r, True)
        return await r.a.run_shared_tool(PHONE_AGENT, "mail_send", {**MAIL, "approved": "typed"})

    assert run(go())["status"] == "error" and ran == []


def test_only_typed_is_an_approval_value(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go() -> dict[str, Any]:
        r = make()
        _typed(r, True)
        return await r.a.run_shared_tool("proj#42", "mail_send", {**MAIL, "approved": "yes"})

    assert run(go())["status"] == "error" and ran == []


def test_the_tool_descriptions_say_when_typed_approval_may_be_used() -> None:
    from scout import mcp_server

    for name in shared_tools.TYPED_TOOLS:
        doc = mcp_server._doc(name)
        assert 'approved: "typed"' in doc
        assert "explicitly approved this exact" in doc
    assert 'approved: "typed"' not in mcp_server._doc("calendar_create_event")
    # The room's own copy (voice in the room) doesn't offer it.
    assert "typed" not in shared_tools.BY_NAME["slack_send"].description
