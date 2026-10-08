"""Measure how much of the assistant's own voice each voice-layer backend lets
through to the microphone (python -m scout.measure).

For each backend it plays the same replies at normal volume and records the
cleaned mic signal while they play. With no one talking in the room, anything
the mic delivers is leftover echo, so we report:
- level_db: loudness of the cleaned mic during playback (dBFS); lower is better,
- reduction_db: how much quieter that is than the plain (no canceller) backend,
- heard: the share of the played words Whisper still recovers from the mic
  recording (0% = the assistant's voice is gone).
Run it with the room quiet and the scout app stopped (it holds the devices).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

import numpy as np

from . import audio_io
from .config import load
from .speech import words
import contextlib

SENTENCES = [
    "It's seventy two degrees and sunny in Austin today, with a light breeze from the south.",
    "I found three files that match: the config, the readme, and the main script.",
    "Do you want me to run the tests now, or wait until you've finished editing?",
    "Your timer is done. The pasta should be ready, so go ahead and drain it.",
    "Here's the plan: first I'll read the logs, then I'll fix the bug and push it.",
]


def _db(samples: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(samples.astype(np.float64) ** 2) + 1e-12))


async def measure(backend: str, kokoro: Any, voice: str, transcribe: Any) -> dict[str, Any]:
    cfg = load()
    io = audio_io.create(backend, cfg.audio.input_device, cfg.audio.output_device)
    loop = asyncio.get_running_loop()
    await io.start(loop, lambda kind, data: None)
    await asyncio.sleep(1.0)  # let the engine (and any canceller) settle
    while not io.frames.empty():
        io.frames.get_nowait()
    levels, recalls = [], []
    try:
        for text in SENTENCES:
            samples, sr = kokoro.create(text, voice=voice, speed=1.0)
            handle = io.play(samples, sr)
            recorded = bytearray()
            deadline = time.monotonic() + handle.seconds + 0.6
            while time.monotonic() < deadline:
                with contextlib.suppress(TimeoutError):
                    recorded.extend(await asyncio.wait_for(io.frames.get(), 0.2))
            await handle.done
            mic = np.frombuffer(bytes(recorded), np.int16).astype(np.float32) / 32768.0
            levels.append(_db(mic))
            heard = set(words(await transcribe(bytes(recorded))))
            said = [w for w in words(text) if len(w) > 2]
            recalls.append(sum(w in heard for w in said) / max(len(said), 1))
            await asyncio.sleep(0.5)
        stats = io.stats()
    finally:
        await io.close()
    return {
        "backend": io.name,
        "level_db": round(float(np.mean(levels)), 1),
        "heard": round(float(np.mean(recalls)) * 100),
        "stats": stats,
    }


async def main(backends: list[str]) -> None:
    from kokoro_onnx import Kokoro

    from .asr import Transcriber

    cfg = load()
    kokoro = Kokoro(str(cfg.path(cfg.tts.model)), str(cfg.path(cfg.tts.voices)))
    asr = Transcriber(cfg.asr.model, cfg.asr.language, cfg.gate.max_compression_ratio)
    await asr.warmup()

    async def transcribe(pcm: bytes) -> str:
        return (await asr.transcribe(pcm)).text

    results = [await measure(b, kokoro, cfg.tts.voice, transcribe) for b in backends]
    base = next((r["level_db"] for r in results if r["backend"] == "plain"), None)
    print(f"{'backend':8} {'level dB':>9} {'reduction':>10} {'heard':>6}")
    for r in results:
        red = f"{base - r['level_db']:.1f} dB" if base is not None else "-"
        print(f"{r['backend']:8} {r['level_db']:9.1f} {red:>10} {r['heard']:5d}%")
    print(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backends", default="plain,webrtc,apple", help="comma-separated backends to compare")
    asyncio.run(main(ap.parse_args().backends.split(",")))
