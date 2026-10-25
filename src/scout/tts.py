"""Local text-to-speech with Kokoro (ONNX), pipelined sentence by sentence."""

from __future__ import annotations

import asyncio
import collections
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
from kokoro_onnx import Kokoro

if TYPE_CHECKING:
    from .audio_io import AudioIO
    from .pronounce import Pronouncer

# A queued item: (text to synthesize, voice, original wording, progress tag, pause after
# in seconds) or a ready-made tone. The progress tag is None for real speech.
_Item = tuple[str, str | None, str, int | None, float] | np.ndarray

log = logging.getLogger(__name__)

_SENTENCE = re.compile(r"(?<=[.!?;:])\s+(?=\S)")
# Kokoro synthesizes a whole piece before any of it plays (measured: 0.33 s for 13
# characters, 0.55 s for 60, 1.36 s for 140). A long sentence said into silence is
# split at its first comma or dash past SPLIT_MIN characters, so playback starts sooner.
SPLIT_OVER = 60  # sentences longer than this are split
SPLIT_MIN = 40  # ...at a clause break at least this far in
_CLAUSE = re.compile(r"(?:,|\s[-\u2013\u2014])\s+(?=\S)")  # a comma, or a spaced dash
CLAUSE_PAUSE_S = 0.1  # the pause Kokoro itself puts at a comma between batches


def split_first(sentence: str) -> list[str]:
    """A long sentence as [first clause, rest], or unchanged. Only at a comma or a dash
    followed by a space (not "1,000"), at least SPLIT_MIN characters in, with at least a
    few words left after it."""
    if len(sentence) <= SPLIT_OVER:
        return [sentence]
    for m in _CLAUSE.finditer(sentence, SPLIT_MIN):
        head, rest = sentence[: m.end()].rstrip(), sentence[m.end() :].strip()
        if len(rest) >= 15:
            return [head, rest]
        break
    return [sentence]


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
        io: AudioIO,
        echo_tail_ms: int,
        chimes: bool,
        pronounce: Pronouncer | None = None,
    ) -> None:
        self.kokoro = Kokoro(model, voices)
        self.pronounce = pronounce
        self.voice, self.speed = voice, speed
        self.io = io  # the voice layer: one engine for mic and speakers
        self.echo_tail = echo_tail_ms / 1000
        self.chimes = chimes
        self.sr = 24000
        self._text: asyncio.Queue[tuple[int, _Item]] = asyncio.Queue()
        # (generation, samples, original wording or None for a tone, progress tag)
        self._audio: asyncio.Queue[tuple[int, np.ndarray, str | None, int | None]] = asyncio.Queue()
        self._gen = 0  # bumped by stop() to discard queued work
        self._pending = 0  # items queued or in flight
        self._kinds: collections.deque[bool] = collections.deque()  # per queued item: is it speech?
        self._speech = 0  # speech items queued or in flight (chimes and ticks don't count)
        self._progress = 0  # bumped by real speech: progress lines queued before it are dropped
        self._playing = False
        self._in_flight = 0  # pieces handed to the voice layer and not yet heard to the end
        self._last_end = 0.0
        self._idle = asyncio.Event()
        self._idle.set()
        self._synth_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
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

    @property
    def speaking(self) -> bool:
        """Words queued or playing, not just a chime or a tick (barge-in is about words)."""
        return self._speech > 0

    def _enqueue(self, item: _Item) -> None:
        is_speech = not isinstance(item, np.ndarray)
        self._kinds.append(is_speech)
        self._speech += is_speech
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
        self._progress += 1  # a progress line still waiting to play is out of date now
        self._queue_text(text, voice, None)

    def speak_progress(self, text: str) -> None:
        """A "One moment."-style line: dropped if real speech is queued before it plays."""
        self._queue_text(text, None, self._progress)

    def _queue_text(self, text: str, voice: str | None, tag: int | None) -> None:
        sentences = [s.strip() for s in _SENTENCE.split(text.strip()) if s.strip()]
        if sentences and not self.busy:  # said into silence: start the audio sooner
            sentences[:1] = split_first(sentences[0])
        for i, sentence in enumerate(sentences):
            # A split sentence keeps its comma's pause (Kokoro trims a piece's silence).
            pause = CLAUSE_PAUSE_S if sentence[-1:] in ",-\u2013\u2014" and i + 1 < len(sentences) else 0.0
            spoken = self.pronounce.tts(sentence) if self.pronounce is not None else sentence
            self._enqueue((spoken, voice, sentence, tag, pause))

    def _stale(self, tag: int | None) -> bool:
        """A progress line overtaken by real speech."""
        return tag is not None and tag != self._progress

    def first_played_since(self, since: float) -> float | None:
        """When the first sentence that started playing at or after `since` started."""
        starts = [start for start, _, _ in self._played if start >= since]
        return min(starts) if starts else None

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
            self.io.stop_playback()
        self.duck(False)

    DUCK_GAIN = 0.2  # about -14 dB: quiet enough to talk over, loud enough to resume

    def duck(self, on: bool) -> None:
        """Lower (or restore) playback volume, where the audio backend can."""
        set_gain = getattr(self.io, "set_gain", None)
        if set_gain is not None:
            set_gain(self.DUCK_GAIN if on else 1.0)

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
        if self._kinds and self._kinds.popleft():
            self._speech = max(0, self._speech - 1)
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
            tag: int | None = None
            if isinstance(item, np.ndarray):
                samples, original = item, None
            else:
                text, voice, original, tag, pause = item
                if self._stale(tag):
                    self._done_one()
                    continue
                try:
                    samples, sr = await loop.run_in_executor(
                        self._synth_pool, functools.partial(self._create, text, voice, pause)
                    )
                    self.sr = sr
                except Exception:
                    log.exception("TTS failed for %r", item)
                    self._done_one()
                    continue
            self._audio.put_nowait((gen, samples, original, tag))

    def _create(self, text: str, voice: str | None, pause: float = 0.0) -> tuple[np.ndarray, int]:
        samples, sr = self.kokoro.create(text, voice=voice or self.voice, speed=self.speed)
        if pause > 0:
            samples = np.concatenate([samples, np.zeros(int(pause * sr), samples.dtype)])
        return samples, sr

    async def _play_loop(self) -> None:
        # The next piece is queued behind the one playing, once that one is queued itself.
        # Waiting for each to be heard first left the TV silent for its whole output delay
        # (~0.7 s) between sentences, and at the comma of a split first sentence.
        ahead: asyncio.Future[bool] | None = None  # when the piece before this one has gone out
        while True:
            gen, samples, text, tag = await self._audio.get()
            if tag is not None and ahead is not None:
                # A progress line isn't queued ahead: real speech may still overtake it.
                await asyncio.wait([ahead])
                ahead = None
            entry: list[Any] | None = None
            handle = None
            try:
                if gen == self._gen and not self._stale(tag):
                    self._playing = True
                    if text:
                        entry = [time.monotonic(), float("inf"), text]
                        self._played.append(entry)
                    with self._play_lock:  # stop() can't slip in between the check and play()
                        handle = self.io.play(samples, self.sr) if gen == self._gen else None
                    self.consecutive_failures = 0
            except Exception:
                self.consecutive_failures += 1
                log.exception("playback failed")
            if handle is None:
                self._finished(entry, counted=False)
                continue
            self._in_flight += 1
            if entry is not None and ahead is not None and not ahead.done():
                # Queued behind the piece still going out: it starts when that one ends.
                ahead.add_done_callback(functools.partial(self._started, entry))
            handle.done.add_done_callback(functools.partial(self._heard, entry))
            if ahead is not None:
                await asyncio.wait([ahead])
            ahead = handle.sent if handle.sent is not None else handle.done

    @staticmethod
    def _started(entry: list[Any], _sent: asyncio.Future[bool]) -> None:
        entry[0] = time.monotonic()

    def _heard(self, entry: list[Any] | None, _done: asyncio.Future[bool]) -> None:
        self._finished(entry)

    def _finished(self, entry: list[Any] | None, counted: bool = True) -> None:
        """One piece heard to the end (or stopped, or never played)."""
        if counted:
            self._in_flight -= 1
        self._playing = self._in_flight > 0
        self._last_end = time.monotonic()
        if entry is not None:
            entry[1] = self._last_end
        self._done_one()
