"""Local text-to-speech with Kokoro (ONNX), pipelined sentence by sentence."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np
import sounddevice as sd
from kokoro_onnx import Kokoro

if TYPE_CHECKING:
    from .pronounce import Pronouncer

# A queued item: (text to synthesize, voice, original wording) or a ready-made tone.
_Item = tuple[str, str | None, str] | np.ndarray

log = logging.getLogger(__name__)

_SENTENCE = re.compile(r"(?<=[.!?;:])\s+(?=\S)")


def _tone(freqs: Sequence[float], sr: int, dur: float = 0.09, gain: float = 0.25) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    env = np.sin(np.pi * t / dur) ** 2
    return np.concatenate([gain * env * np.sin(2 * np.pi * f * t) for f in freqs]).astype(np.float32)


class Speaker:
    """speak() queues text; a synth worker and a playback worker run in parallel
    so the next sentence is ready while the current one plays."""

    def __init__(
        self,
        model: str,
        voices: str,
        voice: str,
        speed: float,
        device: int | None,
        echo_tail_ms: int,
        chimes: bool,
        pronounce: Pronouncer | None = None,
    ) -> None:
        self.kokoro = Kokoro(model, voices)
        self.pronounce = pronounce
        self.voice, self.speed, self.device = voice, speed, device
        self.echo_tail = echo_tail_ms / 1000
        self.chimes = chimes
        self.sr = 24000
        self._text: asyncio.Queue[tuple[int, _Item]] = asyncio.Queue()
        self._audio: asyncio.Queue[tuple[int, np.ndarray, str | None]] = asyncio.Queue()
        self._gen = 0  # bumped by stop() to discard queued work
        self._pending = 0  # items queued or in flight
        self._playing = False
        self._last_end = 0.0
        self._idle = asyncio.Event()
        self._idle.set()
        self._synth_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._play_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="play")
        self._tasks: list[asyncio.Task[Any]] = []
        # [start, end, text] per sentence actually played; end is inf while playing.
        self._played: deque[list[Any]] = deque(maxlen=100)
        self._play_lock = threading.Lock()
        self.consecutive_failures = 0  # playback errors in a row (the watchdog restarts the app)
        # Web-page reply audio has its own thread, so it never delays the mini's speech.
        self._web_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-web")

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._synth_loop()), asyncio.create_task(self._play_loop())]

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def is_echo(self) -> bool:
        """True while speaking or within the echo tail after speaking."""
        return self._playing or self.busy or time.monotonic() - self._last_end < self.echo_tail

    def _enqueue(self, item: _Item) -> None:
        self._pending += 1
        self._idle.clear()
        self._text.put_nowait((self._gen, item))

    def recent_speech(self, since: float) -> str:
        """What was played from `since` on, in its original wording (what Whisper
        will write when it hears it), not the respelled TTS input."""
        return " ".join(text for start, end, text in self._played if end >= since - 0.5)

    def overlap(self, start: float, end: float) -> float:
        """Seconds of our own speech that played between start and end."""
        now = time.monotonic()
        return float(
            sum(
                max(0.0, min(e if e != float("inf") else now, end) - max(s, start))
                for s, e, _ in self._played
            )
        )

    def last_speech_end(self, since: float) -> float | None:
        """When the speech overlapping a clip that began at `since` ended (now if still playing)."""
        ends = [end for start, end, text in self._played if end >= since - 0.5]
        if not ends:
            return None
        return float(min(max(ends), time.monotonic()))

    def speak(self, text: str, voice: str | None = None) -> None:
        for sentence in _SENTENCE.split(text.strip()):
            sentence = sentence.strip()
            if sentence:
                spoken = self.pronounce.tts(sentence) if self.pronounce is not None else sentence
                self._enqueue((spoken, voice, sentence))

    def chime(self, kind: str) -> None:
        if not self.chimes:
            return
        if kind == "tick":  # quiet "still working" cue
            self._enqueue(_tone([520], self.sr, dur=0.05, gain=0.06))
            return
        freqs = {"wake": [660, 880], "ack": [880], "done": [880, 660], "error": [300, 220]}[kind]
        self._enqueue(_tone(freqs, self.sr))

    def stop(self) -> None:
        with self._play_lock:
            self._gen += 1
            sd.stop()

    async def synth_wav(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to WAV bytes without playing it (for the web page)."""
        import io

        import soundfile as sf

        if self.pronounce is not None:
            text = self.pronounce.tts(text)
        samples, sr = await asyncio.get_running_loop().run_in_executor(
            self._web_pool, lambda: self.kokoro.create(text, voice=voice or self.voice, speed=self.speed)
        )
        buf = io.BytesIO()
        sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    def voices(self) -> list[str]:
        return sorted(self.kokoro.get_voices())

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
                samples, text = item, None
            else:
                text, voice, original = item
                try:
                    samples, sr = await loop.run_in_executor(
                        self._synth_pool, functools.partial(self._create, text, voice)
                    )
                    self.sr = sr
                except Exception:
                    log.exception("TTS failed for %r", item)
                    self._done_one()
                    continue
            self._audio.put_nowait((gen, samples, None if isinstance(item, np.ndarray) else original))

    def _create(self, text: str, voice: str | None) -> tuple[np.ndarray, int]:
        samples, sr = self.kokoro.create(text, voice=voice or self.voice, speed=self.speed)
        return samples, sr

    def _play_blocking(self, samples: np.ndarray, gen: int) -> None:
        with self._play_lock:  # stop() can't slip in between this check and play()
            if gen != self._gen:
                return
            sd.play(samples, self.sr, device=self.device)
        sd.wait()

    async def _play_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            gen, samples, text = await self._audio.get()
            entry = None
            try:
                if gen == self._gen:
                    self._playing = True
                    if text:
                        entry = [time.monotonic(), float("inf"), text]
                        self._played.append(entry)
                    await loop.run_in_executor(self._play_pool, self._play_blocking, samples, gen)
                    self.consecutive_failures = 0
            except Exception:
                self.consecutive_failures += 1
                log.exception("playback failed")
            finally:
                self._playing = False
                self._last_end = time.monotonic()
                if entry is not None:
                    entry[1] = self._last_end
                self._done_one()
