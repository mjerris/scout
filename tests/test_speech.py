"""Wake word, stop/reset, yes/no and echo stripping, using transcripts seen in real use."""

import pytest

from claude_voice.config import WakeConfig
from claude_voice.speech import is_reset, is_stop, parse_yes_no, strip_own_speech, strip_wake, to_speech

W = WakeConfig()


@pytest.mark.parametrize("heard, cmd", [
    ("Hey Claude, what time is it?", "what time is it?"),
    ("Hey, Claude.", ""),
    ("A Claude run echo hello.", "run echo hello."),
    ("He Claude, run echo hello.", "run echo hello."),
    ("Okay Claude stop", "stop"),
    ("so anyway claude can you", "can you"),
    ("I was talking to my friend about clouds", None),
    ("A conversation with an assistant named Claude.", None),
])
def test_strip_wake(heard, cmd):
    assert strip_wake(heard, W.names, W.max_position) == cmd


def test_stop_and_reset():
    assert is_stop("stop") and is_stop("Stomp") and is_stop("never mind")
    assert not is_stop("stop the build after the tests finish please")
    assert is_reset("new conversation") and not is_reset("tell me about the new conversation feature in detail")


@pytest.mark.parametrize("text, answer", [
    ("Yes please", True), ("Yeah go ahead", True), ("Okay.", True),
    ("no, don't do that", False), ("nope", False), ("hmm what?", None),
])
def test_yes_no(text, answer):
    assert parse_yes_no(text) is answer


@pytest.mark.parametrize("heard, spoken, rest", [
    ("It's still 9, 24 p.m. Eastern. Echo hello.", "It's still 9:24 p.m. Eastern.", "echo hello"),
    ("Run command. Yes.", "Run command?", "yes"),
    ("It's running under the mic at SignalWire. Call my count.",
     "It's running under mike at SignalWire dot com, Claude Max account.", ""),
    ("I ran it and it printed hello.", "I ran it, and it printed hello.", ""),
])
def test_strip_own_speech(heard, spoken, rest):
    assert strip_own_speech(heard, spoken) == rest


def test_to_speech_flattens_markdown():
    out = to_speech("## Result\n**Done!** Here's `foo`:\n```py\nprint(1)\n```\n- one\n- two\nSee [docs](https://x.y)")
    assert "#" not in out and "*" not in out and "`" not in out and "https" not in out
    assert "code omitted" in out and "docs" in out
