"""Phrase corpus: how real and adversarial utterances are read by plain code
before anything reaches Claude. Cases live in tests/data/phrases.json as
{"function", "input", "expected", "note"?}."""

import json
from pathlib import Path
from typing import Any

import pytest

from scout import gate, speech
from scout.pronounce import Pronouncer

CASES: list[dict[str, Any]] = json.loads((Path(__file__).parent / "data" / "phrases.json").read_text())
P = Pronouncer(None)
NAMES = ["claude", "claud", "klaud"]  # the default wake names
MAX_POSITION = 3


def run(function: str, inp: Any) -> Any:
    match function:
        case "strip_wake":  # as the assistant does it: STT fixes first
            return speech.strip_wake(P.stt(inp), NAMES, MAX_POSITION)
        case "is_stop":
            return speech.is_stop(inp)
        case "is_stop_utterance":
            return speech.is_stop_utterance(inp, NAMES, MAX_POSITION)
        case "is_reset":
            return speech.is_reset(inp)
        case "parse_yes_no":
            return speech.parse_yes_no(inp)
        case "parse_answer":
            return speech.parse_answer(inp)
        case "split_wait":
            return list(speech.split_wait(inp))
        case "strip_own_speech":
            return speech.strip_own_speech(*inp)
        case "to_speech":
            return speech.to_speech(inp)
        case "pronounce_tts":
            return P.tts(inp)
        case "pronounce_stt":
            return P.stt(inp)
        case "gate.junk":
            return gate.junk(inp)
    raise ValueError(function)


@pytest.mark.parametrize(
    "case", CASES, ids=[f"{c['function']}:{json.dumps(c['input'], ensure_ascii=False)}" for c in CASES]
)
def test_phrase(case: dict[str, Any]) -> None:
    assert run(case["function"], case["input"]) == case["expected"], case.get("note", "")


def test_corpus_covers_every_function() -> None:
    assert {c["function"] for c in CASES} == {
        "strip_wake",
        "is_stop",
        "is_stop_utterance",
        "is_reset",
        "parse_yes_no",
        "parse_answer",
        "split_wait",
        "strip_own_speech",
        "to_speech",
        "pronounce_tts",
        "pronounce_stt",
        "gate.junk",
    }
