"""Utterance segmentation and echo marking over a synthetic frame stream."""

import asyncio
from collections.abc import Callable

import numpy as np

from scout.audio import FRAME_SAMPLES, Segmenter, Utterance, utterances


class FakeMic:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        for f in frames:
            self.frames.put_nowait(f)


def tone_frames(seconds: float, freq: float = 220.0, amp: float = 8000) -> list[bytes]:
    n = int(seconds * 1000 / 30)
    t = np.arange(FRAME_SAMPLES) / 16000
    out = []
    for i in range(n):
        x = (
            amp
            * np.sin(2 * np.pi * freq * (t + i * FRAME_SAMPLES / 16000))
            * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
        )
        out.append(x.astype(np.int16).tobytes())
    return out


def silence(seconds: float) -> list[bytes]:
    return [np.zeros(FRAME_SAMPLES, np.int16).tobytes()] * int(seconds * 1000 / 30)


def collect(frames: list[bytes], is_echo: Callable[[], bool]) -> list[Utterance]:
    async def go() -> list[Utterance]:
        out: asyncio.Queue[Utterance] = asyncio.Queue()
        mic = FakeMic(frames)
        task = asyncio.create_task(utterances(mic, Segmenter(1, 600, 200, 30), is_echo, out))
        await asyncio.sleep(0.3)
        task.cancel()
        return [out.get_nowait() for _ in range(out.qsize())]

    return asyncio.run(go())


def test_echo_marked_when_assistant_starts_talking_mid_clip() -> None:
    frames = silence(0.5) + tone_frames(2.0) + silence(1.0)
    calls = {"n": 0}

    def is_echo() -> bool:  # silent at the clip's onset, speaking a moment later
        calls["n"] += 1
        return calls["n"] > 15

    utts = collect(frames, is_echo)
    assert len(utts) == 1 and utts[0].echo


def test_clean_clip_not_echo() -> None:
    utts = collect(silence(0.5) + tone_frames(1.5) + silence(1.0), lambda: False)
    assert len(utts) == 1 and not utts[0].echo
    assert utts[0].stats.voiced_ms > 1000
