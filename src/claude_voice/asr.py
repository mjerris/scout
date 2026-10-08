"""Local speech recognition with MLX Whisper (Apple Silicon GPU)."""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import mlx_whisper
import numpy as np

log = logging.getLogger(__name__)

# Phrases Whisper tends to hallucinate on noise or silence.
_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye",
    "subtitles by the amaraorg community", "please subscribe", "so", "uh", "um",
}


class Transcriber:
    def __init__(self, model: str, language: str):
        self.model = model
        self.language = language or None
        # MLX work stays on one thread.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z ]", "", s.lower()).strip()

    def _run(self, audio: np.ndarray, prompt: str | None) -> str:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=self.language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            temperature=0.0,
            verbose=None,
        )
        parts = [
            s["text"]
            for s in result.get("segments", [])
            if not (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0)
        ]
        text = " ".join(p.strip() for p in parts).strip()
        norm = self._norm(text)
        # On noise Whisper may also echo its own prompt back.
        if norm in _HALLUCINATIONS or (prompt and norm == self._norm(prompt)):
            return ""
        return text

    async def transcribe(self, pcm: bytes, prompt: str | None = None) -> str:
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
