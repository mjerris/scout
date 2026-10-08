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

from .gate import AudioStats

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


def frame_db(frame: bytes) -> float:
    s = np.frombuffer(frame, np.int16).astype(np.float32)
    return 10 * np.log10(float(np.mean(s * s)) / 32768.0**2 + 1e-12)


@dataclass
class Utterance:
    pcm: bytes  # 16 kHz mono int16
    started: float  # time.monotonic() when speech was detected
    echo: bool  # speech began while (or just after) the assistant was talking
    stats: AudioStats
    ended: float = 0.0  # time.monotonic() when the clip ended


def analyze(pcm: bytes, aggressiveness: int = 2) -> AudioStats:
    """Voiced duration, voiced level and noise floor of a 16 kHz int16 clip."""
    vad = webrtcvad.Vad(aggressiveness)
    n = FRAME_SAMPLES * 2
    voiced, quiet = [], []
    for i in range(0, len(pcm) - n + 1, n):
        f = pcm[i:i + n]
        (voiced if vad.is_speech(f, SAMPLE_RATE) else quiet).append(frame_db(f))
    level = 10 * np.log10(np.mean([10 ** (d / 10) for d in voiced]) + 1e-12) if voiced else -100.0
    floor = float(np.percentile(quiet, 20)) if quiet else level - 30
    return AudioStats(len(voiced) * FRAME_MS, float(level), floor)


async def decode_to_pcm(data: bytes) -> bytes:
    """Any audio container (webm/opus, mp4/aac, wav...) to 16 kHz mono int16 via ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(data), 30)
    if proc.returncode != 0:
        raise ValueError(f"could not decode audio: {err.decode(errors='replace')[:200]}")
    return out


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
        self.voiced_energy = 0.0  # summed mean-square power of voiced frames
        self.trailing = 0

    @staticmethod
    def _power(frame: bytes) -> float:
        s = np.frombuffer(frame, np.int16).astype(np.float32) / 32768.0
        return float(np.mean(s * s))

    def feed(self, frame: bytes) -> bytes | None | bool:
        """Returns True at speech onset, the utterance bytes at its end, else None."""
        speech = self.vad.is_speech(frame, SAMPLE_RATE)
        if not self.triggered:
            self.ring.append((frame, speech))
            if sum(s for _, s in self.ring) >= 0.7 * self.ring.maxlen:
                self.triggered = True
                self.buf = [f for f, _ in self.ring]
                self.voiced = sum(s for _, s in self.ring)
                self.voiced_energy = sum(self._power(f) for f, s in self.ring if s)
                self.trailing = 0
                self.ring.clear()
                return True
            return None
        self.buf.append(frame)
        if speech:
            self.voiced += 1
            self.voiced_energy += self._power(frame)
            self.trailing = 0
        else:
            self.trailing += 1
        if self.trailing >= self.silence_frames or len(self.buf) >= self.max_frames:
            pcm, voiced = b"".join(self.buf), self.voiced
            self.last_voiced_ms = voiced * FRAME_MS
            self.last_level_db = 10 * np.log10(self.voiced_energy / max(voiced, 1) + 1e-12)
            self.reset()
            return pcm if voiced >= self.min_speech_frames else None
        return None


async def utterances(mic: Microphone, seg: Segmenter, is_echo, out: asyncio.Queue[Utterance]):
    """Consume mic frames forever, emitting Utterances. is_echo() reports TTS activity."""
    started, echo = 0.0, False
    silent_since = time.monotonic()
    warned = False
    floor_db: float | None = None  # slow-moving ambient level between utterances
    onset_floor = -60.0
    while True:
        frame = await mic.frames.get()
        if not seg.triggered:
            db = frame_db(frame)
            # Track the floor quickly downward, slowly upward, so speech
            # onsets barely move it.
            if floor_db is None:
                floor_db = db
            else:
                floor_db += (db - floor_db) * (0.2 if db < floor_db else 0.01)
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
        if seg.triggered and not echo:
            # The assistant may start talking after the user does: if it plays
            # at any point during the clip, the clip holds its voice too.
            echo = is_echo()
        if res is True:
            started, echo = time.monotonic(), is_echo()
            onset_floor = floor_db if floor_db is not None else -60.0
        elif isinstance(res, bytes):
            stats = AudioStats(seg.last_voiced_ms, seg.last_level_db, onset_floor)
            await out.put(Utterance(res, started, echo, stats, time.monotonic()))
