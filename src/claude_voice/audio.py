"""Microphone capture and VAD-based utterance segmentation."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import webrtcvad

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def resolve_device(spec: str, kind: str) -> int | None:
    """Turn a config device spec (index or name substring) into a device index."""
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    for i, dev in enumerate(sd.query_devices()):
        if spec.lower() in dev["name"].lower() and dev[f"max_{kind}_channels"] > 0:
            return i
    raise ValueError(f"no {kind} device matching {spec!r}; run with --list-devices")


@dataclass
class Utterance:
    pcm: bytes  # 16 kHz mono int16
    started: float  # time.monotonic() when speech was detected
    echo: bool  # speech began while (or just after) the assistant was talking


class Microphone:
    """Pushes 30 ms int16 frames from the input device onto an asyncio queue."""

    def __init__(self, device: int | None):
        self.device = device
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)
        self._stream: sd.RawInputStream | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        def callback(indata, frames, time_info, status):
            if status:
                log.debug("input status: %s", status)
            loop.call_soon_threadsafe(self._put, bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=callback,
        )
        self._stream.start()
        name = sd.query_devices(self._stream.device)["name"]
        log.info("microphone: %s", name)

    def _put(self, frame: bytes) -> None:
        if self.frames.full():
            self.frames.get_nowait()
        self.frames.put_nowait(frame)

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()


class Segmenter:
    """Groups frames into utterances: speech onset → trailing silence."""

    def __init__(self, aggressiveness: int, silence_ms: int, min_speech_ms: int, max_s: float):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.silence_frames = silence_ms // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.ring: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=10)
        self.reset()

    def reset(self) -> None:
        self.ring.clear()
        self.buf: list[bytes] = []
        self.triggered = False
        self.voiced = 0
        self.trailing = 0

    def feed(self, frame: bytes) -> bytes | None | bool:
        """Returns True at speech onset, the utterance bytes at its end, else None."""
        speech = self.vad.is_speech(frame, SAMPLE_RATE)
        if not self.triggered:
            self.ring.append((frame, speech))
            if sum(s for _, s in self.ring) >= 0.7 * self.ring.maxlen:
                self.triggered = True
                self.buf = [f for f, _ in self.ring]
                self.voiced = sum(s for _, s in self.ring)
                self.trailing = 0
                self.ring.clear()
                return True
            return None
        self.buf.append(frame)
        if speech:
            self.voiced += 1
            self.trailing = 0
        else:
            self.trailing += 1
        if self.trailing >= self.silence_frames or len(self.buf) >= self.max_frames:
            pcm, voiced = b"".join(self.buf), self.voiced
            self.reset()
            return pcm if voiced >= self.min_speech_frames else None
        return None


async def utterances(mic: Microphone, seg: Segmenter, is_echo, out: asyncio.Queue[Utterance]):
    """Consume mic frames forever, emitting Utterances. is_echo() reports TTS activity."""
    started, echo = 0.0, False
    silent_since = time.monotonic()
    warned = False
    while True:
        frame = await mic.frames.get()
        if not warned:
            if np.frombuffer(frame, np.int16).any():
                silent_since = time.monotonic()
            elif time.monotonic() - silent_since > 10:
                log.warning(
                    "microphone has delivered pure silence for 10s; check System Settings → "
                    "Privacy & Security → Microphone for the app running this process"
                )
                warned = True
        res = seg.feed(frame)
        if res is True:
            started, echo = time.monotonic(), is_echo()
        elif isinstance(res, bytes):
            await out.put(Utterance(res, started, echo))
