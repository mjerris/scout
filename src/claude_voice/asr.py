"""Local speech recognition with MLX Whisper (Apple Silicon GPU)."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import mlx_whisper
import numpy as np

from . import gate
from .gate import Transcript

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model: str, language: str):
        self.model = model
        self.language = language or None
        # MLX work stays on one thread.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")

    def _run(self, audio: np.ndarray, prompt: str | None) -> Transcript:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=self.language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            temperature=0.0,
            verbose=None,
        )
        # Segments that are runaway repetition loops are dropped outright.
        segs = [s for s in result.get("segments", []) if s.get("compression_ratio", 0) <= 2.4]
        text = gate.clean(" ".join(s["text"].strip() for s in segs))
        if not segs or not text:
            return Transcript("")
        weights = [max(len(s.get("tokens", [])), 1) for s in segs]
        return Transcript(
            text=text,
            avg_logprob=sum(s.get("avg_logprob", 0) * w for s, w in zip(segs, weights, strict=True))
            / sum(weights),
            no_speech_prob=max(s.get("no_speech_prob", 0) for s in segs),
            compression_ratio=max(s.get("compression_ratio", 0) for s in segs),
        )

    async def transcribe(self, pcm: bytes, prompt: str | None = None) -> Transcript:
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        return await asyncio.get_running_loop().run_in_executor(self._pool, self._run, audio, prompt)

    async def warmup(self) -> None:
        # Use the cached snapshot when present so startup needs no network.
        try:
            from huggingface_hub import snapshot_download

            self.model = snapshot_download(self.model, local_files_only=True)
        except Exception:
            log.info("ASR model %s not cached; downloading", self.model)
        log.info("loading ASR model %s", self.model)
        await self.transcribe(np.zeros(16000, np.int16).tobytes())
        log.info("ASR ready")
