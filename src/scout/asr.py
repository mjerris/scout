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

# MLX keeps freed GPU buffers in a cache for reuse, unbounded by default: with Whisper and
# the tier 1 model allocating differently sized buffers per request, the app grew from
# 4.9 to 6.4 GB in 25 minutes. Past this limit freed buffers go back to the system.
MLX_CACHE_LIMIT = 256 * 1024 * 1024


def limit_mlx_cache(limit: int = MLX_CACHE_LIMIT) -> None:
    """Cap MLX's buffer cache (process-wide; Whisper and the tier 1 model share it)."""
    import mlx.core as mx

    mx.set_cache_limit(limit)


class Transcriber:
    def __init__(
        self, model: str, language: str, max_compression_ratio: float = 2.4, prompt: str = ""
    ) -> None:
        self.max_compression_ratio = max_compression_ratio
        self.prompt = prompt or None  # default initial_prompt for transcribe()
        self.model = model
        self.language = language or None
        limit_mlx_cache()
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
        # Keep every segment and report the worst compression ratio: the gate then
        # rejects a transcript with a runaway loop in it as a whole, instead of
        # silently stitching together what's left (which could drop a retraction).
        segs = list(result.get("segments", []))
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
        prompt = prompt if prompt is not None else self.prompt
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
