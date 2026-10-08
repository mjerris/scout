"""The voice layer: one audio engine that owns both the microphone and the
speakers, so the echo canceller has an exact, sample-aligned reference of what
we play, and the assistant can be interrupted mid-sentence (barge-in).

Backends implement `AudioIO`:
- "apple"  — macOS voice processing (AVAudioEngine + AUVoiceProcessingIO) in a
             small Swift helper process (`native/voiceio`), talking over stdio.
- "webrtc" — WebRTC AEC3 + noise suppression (livekit's AudioProcessingModule)
             on a single full-duplex PortAudio stream. Cross-platform.
- "plain"  — no echo cancellation: separate input and output streams (the old
             behaviour). Fallback and baseline for measurements.

Contract for every backend:
- `frames` delivers the CLEANED microphone signal (after echo cancellation and
  noise suppression where the backend has them) as 16 kHz mono int16 PCM in
  30 ms frames (480 samples = 960 bytes), the format the VAD and Whisper use.
- `play()` queues float32 mono samples at any sample rate (the backend
  resamples) and returns a PlayHandle immediately; `await handle.done` resolves
  True when the audio finished playing, False if it was stopped.
- `stop_playback()` silences output within one buffer (~10-20 ms) and resolves
  every pending handle with False.
- `playing` is True while any queued audio is being output.
- Device changes (unplug, default device switch) are handled inside the
  backend: it restarts its streams and calls `on_event("device_changed", ...)`.
  If it cannot recover it calls `on_event("failed", {"error": ...})`; the app
  then exits so launchd restarts it.
- Everything is driven from the asyncio loop passed to `start()`; callbacks
  from audio threads must hop to the loop with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2

EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass(eq=False)
class PlayHandle:
    """One queued piece of audio. `done` resolves True (played) or False (stopped)."""

    id: int
    seconds: float
    done: asyncio.Future[bool] = field(repr=False)


class AudioIO(Protocol):
    name: str
    frames: asyncio.Queue[bytes]

    async def start(self, loop: asyncio.AbstractEventLoop, on_event: EventHandler) -> None:
        """Open the devices and begin delivering mic frames."""
        ...

    def play(self, samples: np.ndarray, sample_rate: int) -> PlayHandle:
        """Queue float32 mono audio for playback; returns at once."""
        ...

    def stop_playback(self) -> None:
        """Silence output now; pending handles resolve False."""
        ...

    @property
    def playing(self) -> bool: ...

    def seconds_since_frame(self) -> float:
        """For the watchdog: time since the last mic frame arrived."""
        ...

    def stats(self) -> dict[str, Any]:
        """Backend diagnostics for logs and the measurement harness."""
        ...

    async def close(self) -> None: ...


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampling of float32 mono audio (good enough for
    speech playback and for 48k -> 16k after the canceller's own filtering)."""
    if src_rate == dst_rate or samples.size == 0:
        out: np.ndarray = samples.astype(np.float32, copy=False)
        return out
    n = round(samples.size * dst_rate / src_rate)
    x = np.linspace(0, samples.size - 1, n, dtype=np.float64)
    res: np.ndarray = np.interp(x, np.arange(samples.size), samples).astype(np.float32)
    return res


def to_int16_bytes(samples: np.ndarray) -> bytes:
    data: bytes = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return data


class FrameAssembler:
    """Collects arbitrary-length 16 kHz int16 chunks into exact 30 ms frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        out = []
        while len(self._buf) >= FRAME_BYTES:
            out.append(bytes(self._buf[:FRAME_BYTES]))
            del self._buf[:FRAME_BYTES]
        return out


def create(
    backend: str, input_device: str = "", output_device: str = "", *, noise_suppression: bool = False
) -> AudioIO:
    """Pick a backend. "auto" means webrtc when livekit is installed, else plain.

    WebRTC is the default on macOS too: measured on the Mac mini (OBSBOT mic,
    built-in speakers) it removed 27.7 dB of the assistant's own voice and left
    Whisper 2% of its words, against 15 dB / 6% for Apple voice processing
    (with its AGC off; with AGC on, 1 dB / 27%), which also adds ~100 ms of
    capture latency. "apple" stays available for comparison and other rooms."""
    if backend == "auto":
        try:
            import livekit.rtc  # noqa: F401

            backend = "webrtc"
        except ImportError:
            backend = "plain"
    if backend == "apple":
        from .audio_apple import AppleAudioIO

        return AppleAudioIO(input_device, output_device)
    if backend == "webrtc":
        from .audio_webrtc import WebRTCAudioIO

        return WebRTCAudioIO(input_device, output_device, noise_suppression=noise_suppression)
    if backend == "plain":
        from .audio_plain import PlainAudioIO

        return PlainAudioIO(input_device, output_device)
    raise ValueError(f"unknown audio.backend {backend!r}; use auto, apple, webrtc or plain")
