import asyncio
from collections.abc import Callable

import numpy as np
import pytest

from scout.asr import Transcriber
from scout.gate import Transcript


def _fake_run(
    seen: list[str | None],
) -> Callable[[np.ndarray, str | None], Transcript]:  # records the prompt each transcription gets
    def run(audio: np.ndarray, prompt: str | None) -> Transcript:
        seen.append(prompt)
        return Transcript("")

    return run


def test_configured_prompt_is_the_default_and_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    asr = Transcriber("model", "en", prompt="Hey Claude.")
    seen: list[str | None] = []
    monkeypatch.setattr(asr, "_run", _fake_run(seen))
    pcm = np.zeros(1600, np.int16).tobytes()
    asyncio.run(asr.transcribe(pcm))
    asyncio.run(asr.transcribe(pcm, "something else"))
    assert seen == ["Hey Claude.", "something else"]


def test_no_prompt_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    asr = Transcriber("model", "en")
    seen: list[str | None] = []
    monkeypatch.setattr(asr, "_run", _fake_run(seen))
    asyncio.run(asr.transcribe(np.zeros(1600, np.int16).tobytes()))
    assert seen == [None]
