"""Local text-to-speech with Kokoro (ONNX), pipelined sentence by sentence."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro

log = logging.getLogger(__name__)

_SENTENCE = re.compile(r"(?<=[.!?;:])\s+(?=\S)")


def _tone(freqs: list[float], sr: int, dur: float = 0.09) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.sin(np.pi * t / dur) ** 2
    return np.concatenate([0.25 * env * np.sin(2 * np.pi * f * t) for f in freqs]).astype(np.float32)


class Speaker:
    """speak() queues text; a synth worker and a playback worker run in parallel
    so the next sentence is ready while the current one plays."""

    def __init__(self, model: str, voices: str, voice: str, speed: float, device: int | None,
                 echo_tail_ms: int, chimes: bool):
        self.kokoro = Kokoro(model, voices)
        self.voice, self.speed, self.device = voice, speed, device
        self.echo_tail = echo_tail_ms / 1000
        self.chimes = chimes
        self.sr = 24000
        self._text: asyncio.Queue[tuple[int, str | np.ndarray]] = asyncio.Queue()
        self._audio: asyncio.Queue[tuple[int, np.ndarray]] = asyncio.Queue()
        self._gen = 0  # bumped by stop() to discard queued work
        self._pending = 0  # items queued or in flight
        self._playing = False
        self._last_end = 0.0
        self._idle = asyncio.Event()
        self._idle.set()
        self._synth_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._play_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="play")
        self._tasks: list[asyncio.Task] = []
        self._said: deque[tuple[float, str]] = deque(maxlen=50)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._synth_loop()), asyncio.create_task(self._play_loop())]

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def is_echo(self) -> bool:
        """True while speaking or within the echo tail after speaking."""
        return self._playing or self.busy or time.monotonic() - self._last_end < self.echo_tail

    def _enqueue(self, item: str | np.ndarray) -> None:
        self._pending += 1
        self._idle.clear()
        self._text.put_nowait((self._gen, item))

    def recent_speech(self, seconds: float = 60.0) -> str:
        cutoff = time.monotonic() - seconds
        return " ".join(t for ts, t in self._said if ts >= cutoff)

    def speak(self, text: str) -> None:
        self._said.append((time.monotonic(), text))
        for sentence in _SENTENCE.split(text.strip()):
            if sentence.strip():
                self._enqueue(sentence.strip())

    def chime(self, kind: str) -> None:
        if not self.chimes:
            return
        freqs = {"wake": [660, 880], "ack": [880], "done": [880, 660], "error": [300, 220]}[kind]
        self._enqueue(_tone(freqs, self.sr))

    def stop(self) -> None:
        self._gen += 1
        sd.stop()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def _done_one(self) -> None:
        self._pending -= 1
        if self._pending <= 0:
            self._pending = 0
            self._idle.set()

    async def _synth_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            gen, item = await self._text.get()
            if gen != self._gen:
                self._done_one()
                continue
            if isinstance(item, np.ndarray):
                samples = item
            else:
                try:
                    samples, sr = await loop.run_in_executor(
                        self._synth_pool, lambda: self.kokoro.create(item, voice=self.voice, speed=self.speed)
                    )
                    self.sr = sr
                except Exception:
                    log.exception("TTS failed for %r", item)
                    self._done_one()
                    continue
            self._audio.put_nowait((gen, samples))

    def _play_blocking(self, samples: np.ndarray) -> None:
        sd.play(samples, self.sr, device=self.device)
        sd.wait()

    async def _play_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            gen, samples = await self._audio.get()
            try:
                if gen == self._gen:
                    self._playing = True
                    await loop.run_in_executor(self._play_pool, self._play_blocking, samples)
            except Exception:
                log.exception("playback failed")
            finally:
                self._playing = False
                self._last_end = time.monotonic()
                self._done_one()
