"""Always-on local voice front-end for a Claude Code agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from typing import TYPE_CHECKING, Any

# onnxruntime (Kokoro, Silero, smart-turn) starts a telemetry uploader to Microsoft
# on import, and its teardown races that thread at exit (intermittent abort, rc 134:
# "recursive_mutex lock failed"). Opting out must happen before the first import.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

from . import config as config_mod

if TYPE_CHECKING:
    from .audio_io import AudioIO
    from .tts import Speaker


def _setup_logging(verbose: bool) -> None:
    logs = config_mod.DATA / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(logs / "scout.log", maxBytes=5_000_000, backupCount=3)
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
    from . import messages_mac
    from .assistant import Assistant
    from . import audio_io
    from .audio import utterances
    from .tts import Speaker

    log = logging.getLogger("scout")
    loop = asyncio.get_running_loop()

    for p in (cfg.tts.model, cfg.tts.voices):
        if not cfg.path(p).exists():
            raise SystemExit(f"missing {p}; run scripts/fetch-models.sh")

    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    mic = {"name": ""}

    def on_audio_event(kind: str, data: dict[str, Any]) -> None:
        log.info("voice layer %s: %s", kind, data)
        events.put_nowait((kind, data))
        if kind == "device_changed" and data.get("input") and data["input"] != mic["name"]:
            before, mic["name"] = mic["name"], str(data["input"])
            if before:  # say it: otherwise a switch to a worse mic just looks like deafness
                speaker.speak(_mic_change(before, mic["name"], cfg.audio.input_device))

    input_spec = (
        "|".join([cfg.audio.input_device, *cfg.audio.input_fallback])
        if cfg.audio.input_fallback
        else cfg.audio.input_device
    )
    await _wait_for_microphone(input_spec)
    output_spec = (
        "|".join([cfg.audio.output_device, *cfg.audio.output_fallback])
        if cfg.audio.output_device and cfg.audio.output_fallback
        else cfg.audio.output_device
    )
    io = audio_io.create(
        cfg.audio.backend,
        input_spec,
        output_spec,
        noise_suppression=cfg.audio.noise_suppression,
        output_delay_ms=cfg.audio.output_delay_ms,
    )
    await io.start(loop, on_audio_event)
    log.info("voice layer: %s", io.name)
    speaker = Speaker(
        str(cfg.path(cfg.tts.model)),
        str(cfg.path(cfg.tts.voices)),
        cfg.tts.voice,
        cfg.tts.speed,
        io,
        cfg.audio.echo_tail_ms,
        cfg.tts.chimes,
    )
    speaker.start()
    messages_mac.HELPER.announce = speaker.speak  # "Got it" once Full Disk Access is on
    asr = Transcriber(cfg.asr.model, cfg.asr.language, cfg.gate.max_compression_ratio, cfg.asr.prompt)
    await asr.warmup()

    assistant = Assistant(cfg, asr, speaker, echo_cancelled=io.name != "plain")
    assistant.echo_stats = io.stats

    async def warm() -> None:
        try:
            await assistant.brain.warm()
        except Exception:
            logging.getLogger("scout").exception(
                "warming the Claude session failed; it connects on first use"
            )

    assistant._spawn(warm())
    assistant._spawn(assistant.start_tier1())
    speaker.pronounce = assistant.pronounce
    runner = await web.start(cfg.web, assistant) if cfg.web.enabled else None
    seg = _segmenter(cfg)

    stop = asyncio.Event()

    def on_signal() -> None:
        if stop.is_set():  # a second Ctrl-C / SIGTERM: don't wait for a clean shutdown
            log.warning("forced exit")
            os._exit(1)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)

    tasks = [
        asyncio.create_task(
            utterances(io, seg, speaker.is_echo, assistant.utterances, assistant.on_speech_onset), name="mic"
        ),
        asyncio.create_task(assistant.run(), name="assistant"),
        asyncio.create_task(_watch(io, speaker, events), name="watchdog"),
    ]
    log.info('ready — say "Hey Scout, …"')
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
    try:
        await asyncio.wait_for(io.close(), 3)
    except Exception:
        log.exception("voice layer shutdown")
    if failed is not None:
        raise SystemExit(1)


def microphone_present(spec: str = "", devices: Any = None, default: int | None = None) -> bool:
    """Does the input spec (with its fallbacks) resolve to a microphone right now?"""
    from .audio_plain import resolve_device

    if devices is None:
        import sounddevice as sd

        sd._terminate()  # PortAudio only reads the device list when it starts
        sd._initialize()
        devices = sd.query_devices()
        d = sd.default.device[0]
        default = d if isinstance(d, int) else None
    if not any(dev["max_input_channels"] > 0 for dev in devices):
        return False
    try:
        idx = resolve_device(spec, "input", devices, default)
    except ValueError:
        return False
    return idx is None or devices[idx]["max_input_channels"] > 0


def _mic_change(before: str, now: str, preferred: str) -> str:
    def short(name: str) -> str:
        return name.replace(" Microphone", "").strip()

    if preferred and preferred.lower() in now.lower():
        return f"The {short(now)} microphone is back."
    return f"I lost the {short(before)} microphone, so I'm listening on the {short(now)} one."


async def _wait_for_microphone(spec: str, poll_s: float = 5.0) -> None:
    """Without a mic, wait for one instead of crashing (launchd would restart the app
    every 10 s forever: seen when the OBSBOT camera was unplugged or asleep)."""
    log = logging.getLogger("scout")
    if microphone_present(spec):
        return
    log.warning("no microphone%s; waiting for one to appear", f" matching {spec!r}" if spec else "")
    while not microphone_present(spec):
        await asyncio.sleep(poll_s)
    log.info("microphone found")


async def _watch(io: AudioIO, speaker: Speaker, events: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
    """Return (ending the app, so launchd restarts it) when the mic stops
    delivering frames, playback keeps failing, or the voice layer gives up."""
    log = logging.getLogger("scout")
    while True:
        try:
            kind, data = await asyncio.wait_for(events.get(), 2)
            if kind == "failed":
                log.error("voice layer failed: %s", data)
                return
            continue
        except TimeoutError:
            pass
        if io.seconds_since_frame() > 10:
            st = io.stats()
            why = st.get("last_error") or "no error reported"
            log.error(
                "no audio from the microphone for 10 s (%s; callback errors: %s)",
                why,
                st.get("callback_errors"),
            )
            return
        if speaker.consecutive_failures >= 5:
            log.error("audio playback failed 5 times in a row")
            return


def _segmenter(cfg: config_mod.Config) -> Any:
    """Smart end-of-turn detection when its models are present, else silence-based."""
    from .audio import Segmenter

    a = cfg.audio
    if a.turn_detection == "smart":
        from .turn_models import SILERO_MODEL, SMART_TURN_MODEL, EndOfTurnSegmenter, SileroVAD, SmartTurn

        vad, turn = cfg.path(SILERO_MODEL), cfg.path(SMART_TURN_MODEL)
        if vad.exists() and turn.exists():
            return EndOfTurnSegmenter(
                SileroVAD(vad),
                SmartTurn(turn),
                min_speech_ms=a.min_speech_ms,
                max_utterance_s=a.max_utterance_s,
                min_pause_ms=a.min_pause_ms,
                max_pause_ms=a.max_pause_ms,
                turn_threshold=a.turn_threshold,
            )
        logging.getLogger("scout").warning(
            "smart end-of-turn models missing; using silence-based detection (run scripts/fetch-models.sh)"
        )
    elif a.turn_detection != "simple":
        raise ValueError(f"audio.turn_detection must be smart or simple, not {a.turn_detection!r}")
    return Segmenter(a.vad_aggressiveness, a.silence_ms, a.min_speech_ms, a.max_utterance_s)


def main() -> None:
    ap = argparse.ArgumentParser(prog="scout", description=__doc__)
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
