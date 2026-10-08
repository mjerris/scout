"""macOS voice-processing backend: the `native/voiceio` Swift helper runs one
AVAudioEngine with voice processing (AUVoiceProcessingIO echo cancellation and
noise suppression) for both playback and capture; this module talks to it over
its stdin/stdout.

Wire format, both directions: `[1 byte type][u32 little-endian length][payload]`.

Python -> helper: `C` config JSON, `P` play (u32 id, u32 sample rate, float32
mono samples), `S` stop playback, `Q` quit.
Helper -> Python: `M` mic audio (16 kHz mono int16, any chunk size), `D` done
(u32 id, u8 1 = played fully / 0 = stopped), `E` event JSON
(`{"event": "ready" | "device_changed" | "failed" | "stats", ...}`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .audio_io import EventHandler, FrameAssembler, PlayHandle

log = logging.getLogger(__name__)

HELPER_DIR = Path(__file__).resolve().parents[2] / "native" / "voiceio"
HELPER_BIN = HELPER_DIR / "build" / "voiceio"

CONFIG, PLAY, STOP, QUIT = b"C", b"P", b"S", b"Q"
MIC, DONE, EVENT = b"M", b"D", b"E"

_HEADER = struct.Struct("<cI")
_PLAY_HEAD = struct.Struct("<II")
_DONE = struct.Struct("<IB")
HEADER_SIZE = _HEADER.size


def encode_frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _HEADER.pack(kind, len(payload)) + payload


def decode_header(header: bytes) -> tuple[bytes, int]:
    kind, length = _HEADER.unpack(header)
    return kind, length


def encode_play(play_id: int, sample_rate: int, samples: np.ndarray) -> bytes:
    data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
    return encode_frame(PLAY, _PLAY_HEAD.pack(play_id, sample_rate) + data)


def decode_done(payload: bytes) -> tuple[int, bool]:
    play_id, flag = _DONE.unpack(payload)
    return play_id, flag == 1


def decode_event(payload: bytes) -> tuple[str, dict[str, Any]]:
    obj = json.loads(payload)
    if not isinstance(obj, dict):
        raise ValueError(f"event is not an object: {obj!r}")
    name = str(obj.pop("event", ""))
    return name, obj


def helper_available() -> bool:
    """True when the helper is built (scripts/build-voiceio.sh) and newer than every Swift source."""
    try:
        built = HELPER_BIN.stat().st_mtime
    except OSError:
        return False
    sources = [p for p in HELPER_DIR.rglob("*.swift") if "build" not in p.relative_to(HELPER_DIR).parts]
    return bool(sources) and all(p.stat().st_mtime <= built for p in sources)


class AppleAudioIO:
    name = "apple"

    def __init__(
        self,
        input_device: str = "",
        output_device: str = "",
        *,
        voice_processing: bool = True,
        agc: bool = True,
        helper_cmd: Sequence[str] | None = None,
        ready_timeout: float = 10.0,
        close_timeout: float = 2.0,
    ) -> None:
        self.input_device, self.output_device = input_device, output_device
        self.voice_processing, self.agc = voice_processing, agc
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)
        self._cmd = list(helper_cmd) if helper_cmd is not None else [str(HELPER_BIN)]
        self._ready_timeout, self._close_timeout = ready_timeout, close_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: EventHandler | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._ready: asyncio.Future[dict[str, Any]] | None = None
        self._assembler = FrameAssembler()
        self._pending: dict[int, asyncio.Future[bool]] = {}
        self._next_id = 1
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._last_frame = time.monotonic()
        self._closing = False
        self._dead = False
        self._failed_reported = False
        self.ready_info: dict[str, Any] = {}
        self._helper_stats: dict[str, Any] = {}
        self._counts = {"mic_chunks": 0, "mic_frames": 0, "dropped_frames": 0, "played": 0, "stopped": 0}
        self._events = 0

    # -- lifecycle -----------------------------------------------------------

    async def start(self, loop: asyncio.AbstractEventLoop, on_event: EventHandler) -> None:
        """Spawn the helper and wait for its `ready` event. Raises RuntimeError
        if it fails to start (no `failed` event is sent for that case)."""
        self._loop, self._on_event = loop, on_event
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise RuntimeError(
                f"cannot start {self._cmd[0]} (build it with scripts/build-voiceio.sh): {e}"
            ) from e
        proc = self._proc
        if proc.stdout is None or proc.stderr is None:  # not possible with PIPE; keeps the types exact
            raise RuntimeError("voiceio helper started without pipes")
        self._ready = loop.create_future()
        self._tasks = [
            loop.create_task(self._read_stdout(proc, proc.stdout)),
            loop.create_task(self._read_stderr(proc.stderr)),
        ]
        config = {
            "input_device": self.input_device,
            "output_device": self.output_device,
            "voice_processing": self.voice_processing,
            "agc": self.agc,
        }
        self._send(encode_frame(CONFIG, json.dumps(config).encode()))
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self._ready_timeout)
        except (TimeoutError, RuntimeError) as e:
            await self.close()
            if isinstance(e, TimeoutError):
                raise RuntimeError(f"voiceio helper did not become ready in {self._ready_timeout:g} s") from e
            raise
        self._last_frame = time.monotonic()
        log.info("voiceio ready: %s", self.ready_info)

    async def close(self) -> None:
        """Quit the helper (killing it if it doesn't exit promptly) and resolve
        every pending handle False. Safe to call more than once."""
        self._closing = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._send(encode_frame(QUIT))
            if proc.stdin is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), self._close_timeout)
            except TimeoutError:
                log.warning("voiceio helper did not quit; killing it")
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self._resolve_all(False)

    # -- playback ------------------------------------------------------------

    def play(self, samples: np.ndarray, sample_rate: int) -> PlayHandle:
        if self._loop is None:
            raise RuntimeError("AppleAudioIO.play() before start()")
        mono = np.asarray(samples, dtype=np.float32).reshape(-1)
        play_id = self._next_id
        self._next_id = (self._next_id % 0xFFFFFFFF) + 1
        fut: asyncio.Future[bool] = self._loop.create_future()
        handle = PlayHandle(id=play_id, seconds=mono.size / sample_rate, done=fut)
        if mono.size == 0:
            fut.set_result(True)
        elif self._dead or self._closing:
            fut.set_result(False)
        else:
            self._pending[play_id] = fut
            if not self._send(encode_play(play_id, int(sample_rate), mono)):
                self._finish(play_id, False)
        return handle

    def stop_playback(self) -> None:
        if not self._dead:
            self._send(encode_frame(STOP))
        self._resolve_all(False)

    @property
    def playing(self) -> bool:
        return bool(self._pending)

    # -- diagnostics ---------------------------------------------------------

    def seconds_since_frame(self) -> float:
        return time.monotonic() - self._last_frame

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            **self._counts,
            "pending": len(self._pending),
            "events": self._events,
            "helper_pid": self._proc.pid if self._proc else None,
            "helper_alive": self._proc is not None and not self._dead and self._proc.returncode is None,
            "voice_processing": self.voice_processing,
            "devices": {k: self.ready_info.get(k) for k in ("input_device", "output_device")},
            "helper": dict(self._helper_stats),
        }

    # -- internals -----------------------------------------------------------

    def _send(self, data: bytes) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return False
        try:
            proc.stdin.write(data)
        except (OSError, RuntimeError) as e:
            log.warning("voiceio helper write failed: %s", e)
            return False
        return True

    def _finish(self, play_id: int, played: bool) -> None:
        fut = self._pending.pop(play_id, None)
        if fut is None or fut.done():
            return
        self._counts["played" if played else "stopped"] += 1
        fut.set_result(played)

    def _resolve_all(self, played: bool) -> None:
        for play_id in list(self._pending):
            self._finish(play_id, played)

    def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(name, data)
        except Exception:
            log.exception("on_event(%r) handler raised", name)

    def _handle(self, kind: bytes, payload: bytes) -> None:
        if kind == MIC:
            self._counts["mic_chunks"] += 1
            self._last_frame = time.monotonic()
            for frame in self._assembler.push(payload):
                self._counts["mic_frames"] += 1
                if self.frames.full():
                    self.frames.get_nowait()
                    self._counts["dropped_frames"] += 1
                self.frames.put_nowait(frame)
        elif kind == DONE:
            play_id, played = decode_done(payload)
            self._finish(play_id, played)
        elif kind == EVENT:
            name, data = decode_event(payload)
            self._events += 1
            self._on_helper_event(name, data)
        else:
            log.warning("voiceio: unknown frame type %r (%d bytes)", kind, len(payload))

    def _on_helper_event(self, name: str, data: dict[str, Any]) -> None:
        ready = self._ready
        if name == "stats":
            self._helper_stats = data
        elif name == "ready":
            # Set here, not when start() resumes: later events may already be queued behind it.
            self.ready_info = data
            if ready is not None and not ready.done():
                ready.set_result(data)
        elif name == "device_changed":
            self.ready_info = {**self.ready_info, **data}
            log.info("voiceio device changed: %s", data)
        elif name == "failed":
            log.error("voiceio failed: %s", data.get("error"))
            self._failed_reported = True
            if ready is not None and not ready.done():
                ready.set_exception(RuntimeError(f"voiceio helper failed: {data.get('error')}"))
                return
        self._emit(name, data)

    async def _read_stdout(self, proc: asyncio.subprocess.Process, stdout: asyncio.StreamReader) -> None:
        try:
            while True:
                kind, length = decode_header(await stdout.readexactly(HEADER_SIZE))
                payload = await stdout.readexactly(length) if length else b""
                try:
                    self._handle(kind, payload)
                except (ValueError, struct.error) as e:
                    log.warning("voiceio: bad %r frame: %s", kind, e)
        except asyncio.IncompleteReadError:
            pass
        await self._helper_gone(proc)

    async def _read_stderr(self, stderr: asyncio.StreamReader) -> None:
        while line := await stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self._stderr_tail.append(text)
            log.info("%s", text)

    async def _helper_gone(self, proc: asyncio.subprocess.Process) -> None:
        """stdout reached EOF: the helper exited (or is about to)."""
        self._dead = True
        code: int | None = None
        with contextlib.suppress(TimeoutError):
            code = await asyncio.wait_for(proc.wait(), 2.0)
        # Let the stderr reader catch the helper's last words for the error message.
        stderr_task = self._tasks[1] if len(self._tasks) > 1 else None
        if stderr_task is not None and not stderr_task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(stderr_task), 1.0)
        self._resolve_all(False)
        if self._closing:
            return
        tail = " | ".join(list(self._stderr_tail)[-3:])
        error = f"voiceio helper exited (code {code})" + (f": {tail}" if tail else "")
        ready = self._ready
        if ready is not None and not ready.done():
            ready.set_exception(RuntimeError(error))
            return
        if not self._failed_reported:
            self._failed_reported = True
            log.error("%s", error)
            self._emit("failed", {"error": error})
