"""Wake word, stop/reset, yes/no and echo stripping, using transcripts seen in real use."""

import pytest

from claude_voice.config import WakeConfig
from claude_voice.speech import is_reset, is_stop, parse_yes_no, strip_own_speech, strip_wake, to_speech

W = WakeConfig()


@pytest.mark.parametrize(
    "heard, cmd",
    [
        ("Hey Claude, what time is it?", "what time is it?"),
        ("Hey, Claude.", ""),
        ("A Claude run echo hello.", "run echo hello."),
        ("He Claude, run echo hello.", "run echo hello."),
        ("Okay Claude stop", "stop"),
        ("so anyway claude can you", "can you"),
        ("I was talking to my friend about clouds", None),
        ("A conversation with an assistant named Claude.", None),
    ],
)
def test_strip_wake(heard: str, cmd: str | None) -> None:
    assert strip_wake(heard, W.names, W.max_position) == cmd


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
