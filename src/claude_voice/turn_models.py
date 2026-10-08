"""Placeholder for the end-of-turn lane (Silero VAD + smart-turn); replaced on merge."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class EndOfTurnSegmenter:
    @classmethod
    def from_models(cls, models: Path, **kwargs: Any) -> EndOfTurnSegmenter:
        raise FileNotFoundError("end-of-turn models not implemented yet")
