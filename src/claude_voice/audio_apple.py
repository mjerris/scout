"""macOS voice-processing backend via the native/voiceio helper (to be implemented by the apple lane)."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from .audio_io import EventHandler, PlayHandle


class AppleAudioIO:
    name = "apple"

    def __init__(self, input_device: str = "", output_device: str = "") -> None:
        self.input_device, self.output_device = input_device, output_device
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)

    async def start(self, loop: asyncio.AbstractEventLoop, on_event: EventHandler) -> None:
        raise NotImplementedError

    def play(self, samples: np.ndarray, sample_rate: int) -> PlayHandle:
        raise NotImplementedError

    def stop_playback(self) -> None:
        raise NotImplementedError

    @property
    def playing(self) -> bool:
        raise NotImplementedError

    def seconds_since_frame(self) -> float:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def helper_available() -> bool:
    """True when the native helper has been built (scripts/build-voiceio.sh)."""
    return False
