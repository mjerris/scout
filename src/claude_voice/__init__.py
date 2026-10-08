"""Always-on local voice front-end for a Claude Code agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from typing import TYPE_CHECKING

from . import config as config_mod

if TYPE_CHECKING:
    from .audio import Microphone
    from .tts import Speaker


def _setup_logging(verbose: bool) -> None:
    logs = config_mod.ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(logs / "claude-voice.log", maxBytes=5_000_000, backupCount=3)
    ]
    # Under launchd stdout is logs/launchd.out.log, which nothing rotates; the
    # rotating file above already has every line. Echo to the terminal only.
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)
        # phonemizer resets its own logger level on every call; drop its benign
        # word-count warnings here instead.
        h.addFilter(lambda r: r.name != "phonemizer" or r.levelno >= logging.ERROR)
        root.addHandler(h)
    for noisy in ("aiohttp", "httpx", "httpx2", "huggingface_hub", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def _amain(cfg: config_mod.Config) -> None:
    from . import web
    from .asr import Transcriber
    from .assistant import Assistant
    from .audio import Microphone, Segmenter, resolve_device, utterances
    from .tts import Speaker

    log = logging.getLogger("claude_voice")
    loop = asyncio.get_running_loop()

    for p in (cfg.tts.model, cfg.tts.voices):
        if not cfg.path(p).exists():
            raise SystemExit(f"missing {p}; run scripts/fetch-models.sh")

    speaker = Speaker(
        str(cfg.path(cfg.tts.model)),
        str(cfg.path(cfg.tts.voices)),
        cfg.tts.voice,
        cfg.tts.speed,
        resolve_device(cfg.audio.output_device, "output"),
        cfg.audio.echo_tail_ms,
        cfg.tts.chimes,
    )
    speaker.start()
    asr = Transcriber(cfg.asr.model, cfg.asr.language, cfg.gate.max_compression_ratio)
    await asr.warmup()

    assistant = Assistant(cfg, asr, speaker)
    speaker.pronounce = assistant.pronounce
    runner = await web.start(cfg.web, assistant) if cfg.web.enabled else None

    mic = Microphone(resolve_device(cfg.audio.input_device, "input"))
    mic.start(loop)
    seg = Segmenter(
        cfg.audio.vad_aggressiveness, cfg.audio.silence_ms, cfg.audio.min_speech_ms, cfg.audio.max_utterance_s
    )

    stop = asyncio.Event()

    def on_signal() -> None:
        if stop.is_set():  # a second Ctrl-C / SIGTERM: don't wait for a clean shutdown
            log.warning("forced exit")
            os._exit(1)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)

    tasks = [
        asyncio.create_task(utterances(mic, seg, speaker.is_echo, assistant.utterances), name="mic"),
        asyncio.create_task(assistant.run(), name="assistant"),
        asyncio.create_task(_watch(mic, speaker), name="watchdog"),
    ]
    log.info('ready — say "Hey Claude, …"')
    speaker.chime("done")
    stopper = asyncio.create_task(stop.wait(), name="signal")
    done, _ = await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
    failed = next((t for t in done if t is not stopper), None)
    if failed is not None:
        # The mic loop, the assistant or the watchdog ended: exit non-zero so
        # launchd's KeepAlive restarts us instead of leaving a deaf process.
        exc = failed.exception() if not failed.cancelled() else None
        log.error(
            "%s stopped (%s); exiting so launchd restarts the app", failed.get_name(), exc or "no error"
        )

    log.info("shutting down")
    mic.stop()
    speaker.stop()
    for t in [*tasks, stopper]:
        t.cancel()
    try:
        await asyncio.wait_for(assistant.shutdown(), 3)
    except Exception:
        log.exception("assistant shutdown")
    if runner:
        try:
            await asyncio.wait_for(runner.cleanup(), 3)
        except Exception:
            log.exception("web shutdown")
    if failed is not None:
        raise SystemExit(1)


async def _watch(mic: Microphone, speaker: Speaker) -> None:
    """Return (ending the app, so launchd restarts it) when the mic stops
    delivering frames or playback keeps failing, e.g. a device was unplugged."""
    while True:
        await asyncio.sleep(2)
        if mic.seconds_since_frame() > 10:
            logging.getLogger("claude_voice").error("no audio from the microphone for 10 s")
            return
        if speaker.consecutive_failures >= 5:
            logging.getLogger("claude_voice").error("audio playback failed 5 times in a row")
            return


def main() -> None:
    ap = argparse.ArgumentParser(prog="claude-voice", description=__doc__)
    ap.add_argument("--config", help="path to config.toml (default: project root)")
    ap.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return

    _setup_logging(args.verbose)
    from pathlib import Path

    cfg = config_mod.load(Path(args.config) if args.config else None)
    asyncio.run(_amain(cfg))
