"""End-of-turn detection: when has the user finished speaking?

Two parts, both local and both plain inference (no tokens):
- `SpeechDetector`: frame-level voice activity, Silero VAD (ONNX) on 30 ms
  16 kHz frames; replaces WebRTC VAD, which mistakes noise for speech.
- `TurnDetector`: after a short pause, Pipecat's smart-turn-v3 model (ONNX,
  BSD-2) looks at the last few seconds of audio and says whether the speaker
  sounds finished (falling intonation, complete phrase) or is mid-thought.

`Segmenter` (in audio.py) uses them like this:
- speech starts when the detector sees enough speech frames;
- after `min_pause_ms` of silence it asks the TurnDetector; if the turn is
  complete (probability >= threshold) the utterance ends right away;
- otherwise it keeps listening until `max_pause_ms` of silence, then ends anyway.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class SpeechDetector(Protocol):
    def speech_prob(self, frame: bytes) -> float:
        """Probability (0-1) that this 30 ms 16 kHz int16 frame contains speech."""
        ...

    def reset(self) -> None:
        """Forget state between utterances."""
        ...


class TurnDetector(Protocol):
    def complete_prob(self, audio: np.ndarray) -> float:
        """Probability (0-1) that the speaker has finished their turn, given up to
        the last 8 s of 16 kHz float32 mono audio ending at the current pause."""
        ...
