"""Wake word, stop/reset, yes/no and echo stripping, using transcripts seen in real use."""

import pytest

from scout.config import WakeConfig
from scout.speech import is_reset, is_stop, parse_yes_no, strip_own_speech, strip_wake, to_speech

W = WakeConfig()


@pytest.mark.parametrize(
    "heard, cmd",
    [
        ("Hey Scout, what time is it?", "what time is it?"),
        ("Hey, Scout.", ""),
        ("A Scout run echo hello.", "run echo hello."),
        ("He Scout, run echo hello.", "run echo hello."),
        ("Okay Scout stop", "stop"),
        ("so anyway scout can you", "can you"),
        ("I was talking to my friend about scouting", None),
        ("A conversation with a dog named Scout.", None),
        ("Hey Claude, what time is it?", None),  # the old name no longer wakes it by default
    ],
)
def test_strip_wake(heard: str, cmd: str | None) -> None:
    assert strip_wake(heard, W.names, W.max_position, W.greeted_names) == cmd


@pytest.mark.parametrize(
    "heard, cmd",
    [
        ("Hey Scott, what's on my calendar today?", "what's on my calendar today?"),  # heard live
        ("Okay Scott set a timer", "set a timer"),
        ("Hey Scott.", ""),
        ("Scott said the build failed.", None),  # about someone named Scott
        ("Scott, can you pass the salt?", None),
        ("I told Scott hey about it", None),
    ],
)
def test_greeted_names_wake_only_after_a_greeting(heard: str, cmd: str | None) -> None:
    assert strip_wake(heard, W.names, W.max_position, W.greeted_names) == cmd


def test_stop_and_reset() -> None:
    assert is_stop("stop") and is_stop("Stomp") and is_stop("never mind")
    assert not is_stop("stop the build after the tests finish please")
    assert is_reset("new conversation") and not is_reset(
        "tell me about the new conversation feature in detail"
    )


@pytest.mark.parametrize(
    "text, answer",
    [
        ("Yes please", True),
        ("Yeah go ahead", True),
        ("Okay.", True),
        ("no, don't do that", False),
        ("nope", False),
        ("hmm what?", None),
    ],
)
def test_yes_no(text: str, answer: bool | None) -> None:
    assert parse_yes_no(text) is answer


@pytest.mark.parametrize(
    "heard, spoken, rest",
    [
        ("It's still 9, 24 p.m. Eastern. Echo hello.", "It's still 9:24 p.m. Eastern.", "echo hello"),
        ("Run command. Yes.", "Run command?", "yes"),
        (
            "It's running under the mic at SignalWire. Call my count.",
            "It's running under mike at SignalWire dot com, Claude Max account.",
            "",
        ),
        ("I ran it and it printed hello.", "I ran it, and it printed hello.", ""),
    ],
)
def test_strip_own_speech(heard: str, spoken: str, rest: str) -> None:
    assert strip_own_speech(heard, spoken) == rest


def test_to_speech_flattens_markdown() -> None:
    out = to_speech(
        "## Result\n**Done!** Here's `foo`:\n```py\nprint(1)\n```\n- one\n- two\nSee [docs](https://x.y)"
    )
    assert "#" not in out and "*" not in out and "`" not in out and "https" not in out
    assert "code omitted" in out and "docs" in out


@pytest.mark.parametrize(
    "text, stop",
    [
        ("stop", True),
        ("Stop talking.", True),
        ("that's enough", True),
        ("never mind please", True),
        ("Claude, stop", True),
        ("cancel the timer", False),
        ("no, cancel it", False),
        ("start the stopwatch", False),
        ("make it quieter", False),
    ],
)
def test_is_stop_whole_utterance(text: str, stop: bool) -> None:
    assert is_stop(text) is stop


@pytest.mark.parametrize(
    "text, reset",
    [
        ("new conversation", True),
        ("let's start over", True),
        ("reset please", True),
        ("reset my router", False),
        ("preset the oven", False),
        ("tell me about the new conversation feature", False),
    ],
)
def test_is_reset_whole_utterance(text: str, reset: bool) -> None:
    assert is_reset(text) is reset


def test_strip_own_speech_keeps_words_before_our_speech() -> None:
    heard = "Claude what's the weather your timer is done"
    assert strip_own_speech(heard, "Your timer is done.") == "claude what's the weather"


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("sure, no problem", True),
        ("yes, always, don't ask me again", "always"),
        ("yes, don't ask again", "always"),  # an explicit "don't ask again" is permanent
        ("no, don't", False),
        ("no problem, but don't do it", False),
    ],
)
def test_agreeing_phrases_with_no_words(text: str, answer: bool | str) -> None:
    from scout.speech import parse_answer

    assert parse_answer(text) == answer


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("I'm not sure", None),
        ("that's not okay", False),
        ("not yes", False),
        ("never", False),
        ("maybe", None),
        ("always ask me first", None),
        ("yes, but always ask", True),
        ("always", "always"),
        ("always allow that", "always"),
        ("yes always", "always"),
        ("no, always", False),
    ],
)
def test_answers_are_conservative(text: str, answer: bool | str | None) -> None:
    from scout.speech import parse_answer

    assert parse_answer(text) == answer


@pytest.mark.parametrize(
    ("text", "waiting"),
    [
        ("tell them not to wait", False),
        ("don't wait", False),
        ("hold on to that file", False),
        ("tell them to hold on", False),
        ("Okay, wait", True),
        ("wait", True),
        ("Run the tests. Hold on!", True),
        ("Open Netflix and hang on", True),
        ("what about, give me a sec.", True),
    ],
)
def test_wait_detection_is_not_fooled(text: str, waiting: bool) -> None:
    from scout.speech import split_wait

    assert split_wait(text)[1] is waiting


def test_wake_names_are_case_insensitive() -> None:
    assert strip_wake("Hey Jarvis, lights", ["Claude", "Jarvis"], 3) == "lights"


def test_echo_removal_matches_spelled_numbers_to_digits() -> None:
    # Real case: Whisper wrote our "a hundred" as "100", so our whole question leaked through.
    spoken = (
        "That's about a hundred fixes, plus a test file of nearly five hundred real phrases. "
        "Six hundred fifty tests pass. Approve?"
    )
    heard = "That's about 100 fixes, plus a test file of nearly 500 real phrases. 650 tests pass. Approve? Approve."
    assert strip_own_speech(heard, spoken) == "approve"
    assert strip_own_speech("I need 5 minutes", "How long?") == "I need 5 minutes"


def test_greeted_name_stop() -> None:
    from scout.speech import is_stop_utterance

    assert is_stop_utterance("Hey Scott, stop.", W.names, W.max_position, W.greeted_names)
    assert not is_stop_utterance("Scott, stop.", W.names, W.max_position, W.greeted_names)


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        (
            "Catherine says she'll be home on 09/30/2026.",
            "Catherine says she'll be home on September 30th.",
        ),  # heard live
        ("Due 2026-10-09.", "Due tomorrow."),
        ("The meeting is on 2026-10-13.", "The meeting is on Tuesday."),
        ("It shipped on October 7, 2026.", "It shipped yesterday."),
        ("Renewal on 03/01/2027.", "Renewal on March 1st, 2027."),
        ("Invoice from 10/02/2026.", "Invoice from last Friday."),
        ("Call 555-0100 at 10:30.", "Call 555-0100 at 10:30."),  # not dates
        (
            "Version 2026-10-08T15:00:00 is ISO with a time.",
            "Version 2026-10-08T15:00:00 is ISO with a time.",
        ),
    ],
)
def test_dates_are_spoken_naturally(text: str, spoken: str) -> None:
    import datetime as dt

    from scout.speech import speak_dates

    assert speak_dates(text, dt.date(2026, 10, 8)) == spoken


@pytest.mark.parametrize(
    ("sender", "name"),
    [
        ("Rover.com <rover@e.rover.com>", "Rover"),
        ("e.rover.com", "Rover"),
        ('"Pat Lee" <pat@acme.com>', "Pat Lee"),
    ],
)
def test_web_address_senders_are_said_by_name(sender: str, name: str) -> None:
    from scout.summary import sender_name

    assert sender_name(sender) == name
