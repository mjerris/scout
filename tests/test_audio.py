"""Utterance segmentation and echo marking over a synthetic frame stream."""

import asyncio

import numpy as np

from claude_voice.audio import FRAME_SAMPLES, Segmenter, utterances


class FakeMic:
    def __init__(self, frames):
        self.frames = asyncio.Queue()
        for f in frames:
            self.frames.put_nowait(f)


def tone_frames(seconds, freq=220.0, amp=8000):
    n = int(seconds * 1000 / 30)
    t = np.arange(FRAME_SAMPLES) / 16000
    out = []
    for i in range(n):
        x = amp * np.sin(2 * np.pi * freq * (t + i * FRAME_SAMPLES / 16000)) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
        out.append(x.astype(np.int16).tobytes())
    return out


def silence(seconds):
    return [np.zeros(FRAME_SAMPLES, np.int16).tobytes()] * int(seconds * 1000 / 30)


def collect(frames, is_echo):
    async def go():
        out = asyncio.Queue()
        task = asyncio.create_task(utterances(FakeMic(frames), Segmenter(1, 600, 200, 30), is_echo, out))
        await asyncio.sleep(0.3)
        task.cancel()
        return [out.get_nowait() for _ in range(out.qsize())]
    return asyncio.run(go())


def test_echo_marked_when_assistant_starts_talking_mid_clip():
    frames = silence(0.5) + tone_frames(2.0) + silence(1.0)
    calls = {"n": 0}

    def is_echo():  # silent at the clip's onset, speaking a moment later
        calls["n"] += 1
        return calls["n"] > 15

    utts = collect(frames, is_echo)
    assert len(utts) == 1 and utts[0].echo


def test_clean_clip_not_echo():
    utts = collect(silence(0.5) + tone_frames(1.5) + silence(1.0), lambda: False)
    assert len(utts) == 1 and not utts[0].echo
    assert utts[0].stats.voiced_ms > 1000
