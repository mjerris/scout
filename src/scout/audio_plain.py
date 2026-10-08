"""Plain backend: separate input and output streams, no echo cancellation.

This is the behaviour the assistant had before the voice layer: a 16 kHz
PortAudio input stream delivering 30 ms frames, and playback through
`sounddevice.play` one clip at a time from a worker thread.

Also home to what the PortAudio backends share: the `SoundDevice` adapter
(the one seam tests replace), device resolution, and `DeviceWatcher`, which
notices device changes PortAudio itself never reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import select
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np

from .audio_io import FRAME_SAMPLES, SAMPLE_RATE, EventHandler, PlayHandle

log = logging.getLogger(__name__)

STALL_SECONDS = 3.0  # no audio callbacks for this long means the device is gone
RESTART_ATTEMPTS = 3


class Stream(Protocol):
    """The parts of a sounddevice stream the backends use."""

    @property
    def latency(self) -> Any: ...

    @property
    def device(self) -> Any: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class SoundAPI(Protocol):
    """What the backends need from PortAudio. `SoundDevice` is the real one; tests pass a fake."""

    def input_stream(self, **kwargs: Any) -> Stream: ...

    def duplex_stream(self, **kwargs: Any) -> Stream: ...

    def play(self, samples: np.ndarray, sample_rate: int, device: int | None) -> None: ...

    def wait(self) -> None: ...

    def stop(self) -> None: ...

    def reinit(self) -> None: ...

    def resolve(self, spec: str, kind: str) -> int | None: ...

    def device_name(self, device: int | None, kind: str) -> str: ...


class SoundDevice:
    """Thin adapter over the `sounddevice` module."""

    def __init__(self) -> None:
        import sounddevice as sd

        self.sd = sd

    def input_stream(self, **kwargs: Any) -> Stream:
        stream: Stream = self.sd.RawInputStream(**kwargs)
        return stream

    def duplex_stream(self, **kwargs: Any) -> Stream:
        stream: Stream = self.sd.Stream(**kwargs)
        return stream

    def play(self, samples: np.ndarray, sample_rate: int, device: int | None) -> None:
        self.sd.play(samples, sample_rate, device=device)

    def wait(self) -> None:
        self.sd.wait()

    def stop(self) -> None:
        self.sd.stop()

    def reinit(self) -> None:
        """Re-enumerate devices. PortAudio only reads the device list at
        initialisation; this closes every stream this process has open."""
        self.sd._terminate()
        self.sd._initialize()

    def resolve(self, spec: str, kind: str) -> int | None:
        return resolve_device(spec, kind, self.sd.query_devices())

    def device_name(self, device: int | None, kind: str) -> str:
        return str(self.sd.query_devices(device, kind)["name"])


def resolve_device(spec: str, kind: str, devices: Any) -> int | None:
    """Turn a config device spec (index or name substring) into a device index."""
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    for i, dev in enumerate(devices):
        if spec.lower() in dev["name"].lower() and dev[f"max_{kind}_channels"] > 0:
            return i
    raise ValueError(f"no {kind} device matching {spec!r}; run with --list-devices")


def _probe(sd: SoundAPI, input_spec: str, output_spec: str) -> dict[str, Any]:
    try:
        sd.reinit()
        return {
            "input": sd.device_name(sd.resolve(input_spec, "input"), "input"),
            "output": sd.device_name(sd.resolve(output_spec, "output"), "output"),
        }
    except Exception as e:
        return {"error": str(e)}


def _probe_main() -> None:
    """Child process for DeviceWatcher: print the devices our specs resolve to,
    re-enumerating every `interval` seconds, until stdin closes."""
    input_spec, output_spec, interval = sys.argv[1], sys.argv[2], float(sys.argv[3])
    sd = SoundDevice()
    while True:
        print(json.dumps(_probe(sd, input_spec, output_spec)), flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], interval)
        if ready and not os.read(sys.stdin.fileno(), 1024):
            return


class DeviceWatcher:
    """Reports when the devices the configured specs resolve to change.

    PortAudio enumerates devices once, at initialisation, and never reports
    changes; re-initialising it would close our streams. So a small child
    process re-enumerates every few seconds and prints what it finds, and
    `on_change` is called with the new result whenever it differs.
    """

    def __init__(
        self, input_spec: str, output_spec: str, on_change: Callable[[dict[str, Any]], None], interval: float
    ) -> None:
        self.input_spec, self.output_spec = input_spec, output_spec
        self.on_change = on_change
        self.interval = interval
        self.current: dict[str, Any] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        code = "from scout.audio_plain import _probe_main; _probe_main()"
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            self.input_spec,
            self.output_spec,
            str(self.interval),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._task = asyncio.create_task(self._read(self._proc))

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None:
            return
        while line := await proc.stdout.readline():
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if self.current is not None and found != self.current:
                self.on_change(found)
            self.current = found
        if self._proc is not None:  # not closing
            log.warning(
                "device watcher exited (code %s); device changes will not be noticed", proc.returncode
            )

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class PlainAudioIO:
    name = "plain"
    retry_delay = 1.0  # seconds before the first reopen retry; grows per attempt
    stall_seconds = STALL_SECONDS
    monitor_interval = 1.0

    def __init__(
        self,
        input_device: str = "",
        output_device: str = "",
        *,
        sound: SoundAPI | None = None,
        watch_devices: bool = True,
        poll_interval: float = 5.0,
    ) -> None:
        self.input_device, self.output_device = input_device, output_device
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)
        self._sd: SoundAPI = sound if sound is not None else SoundDevice()
        self._watch = watch_devices
        self._poll_interval = poll_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: EventHandler = lambda _kind, _info: None
        self._stream: Stream | None = None
        self._in_dev: int | None = None
        self._out_dev: int | None = None
        self._names = ("", "")
        self._last_frame = time.monotonic()
        self._callbacks = 0
        self._status_flags = 0
        # Playback: a queue served by one worker thread; `_gen` bumps on stop.
        self._lock = threading.Lock()
        self._gen = 0
        self._next_id = 0
        self._handles: dict[int, PlayHandle] = {}
        self._queue: queue.Queue[tuple[int, np.ndarray, int, int] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._failures = 0  # playback errors in a row
        self._played = 0
        self._stopped = 0
        self._restarts = 0
        self._restarting = False
        self._last_error = ""
        self._watcher: DeviceWatcher | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self, loop: asyncio.AbstractEventLoop, on_event: EventHandler) -> None:
        self._loop, self._on_event = loop, on_event
        self._open()
        self._worker = threading.Thread(target=self._play_worker, name="plain-play", daemon=True)
        self._worker.start()
        self._monitor = loop.create_task(self._monitor_loop())
        if self._watch:
            self._watcher = DeviceWatcher(
                self.input_device, self.output_device, self._devices_changed, self._poll_interval
            )
            await self._watcher.start()

    def _open(self) -> None:
        self._in_dev = self._sd.resolve(self.input_device, "input")
        self._out_dev = self._sd.resolve(self.output_device, "output")
        stream = self._sd.input_stream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="int16",
            device=self._in_dev,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream
        self._last_frame = time.monotonic()
        self._names = (
            self._sd.device_name(self._in_dev, "input"),
            self._sd.device_name(self._out_dev, "output"),
        )
        log.info("audio (plain): in %s, out %s", *self._names)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                log.debug("closing input stream: %s", e)

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        self._callbacks += 1
        if status:
            self._status_flags += 1
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._put, bytes(indata))

    def _put(self, frame: bytes) -> None:
        self._last_frame = time.monotonic()
        if self.frames.full():
            self.frames.get_nowait()
        self.frames.put_nowait(frame)

    # -- playback

    def play(self, samples: np.ndarray, sample_rate: int) -> PlayHandle:
        if self._loop is None:
            raise RuntimeError("start() the audio backend before playing")
        self._next_id += 1
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        handle = PlayHandle(self._next_id, data.size / sample_rate, self._loop.create_future())
        self._handles[handle.id] = handle
        self._queue.put((handle.id, data, sample_rate, self._gen))
        return handle

    def stop_playback(self) -> None:
        with self._lock:
            self._gen += 1
            if self._handles:
                self._sd.stop()
        for hid in list(self._handles):
            self._resolve(hid, False)

    @property
    def playing(self) -> bool:
        return bool(self._handles)

    def _resolve(self, hid: int, played: bool) -> None:
        handle = self._handles.pop(hid, None)
        if handle is not None and not handle.done.done():
            handle.done.set_result(played)
            if played:
                self._played += 1
            else:
                self._stopped += 1

    def _play_worker(self) -> None:
        while (item := self._queue.get()) is not None:
            hid, samples, sr, gen = item
            played = False
            try:
                with self._lock:  # stop_playback can't slip in between this check and play()
                    started = gen == self._gen
                    if started:
                        self._sd.play(samples, sr, self._out_dev)
                if started:
                    self._sd.wait()
                    with self._lock:
                        played = gen == self._gen
                self._failures = 0
            except Exception as e:
                self._failures += 1
                self._last_error = f"playback: {e}"
                if self._failures == 3:
                    self._call(self._schedule_restart, "playback failing")
            self._call(self._resolve, hid, played)

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    # -- device changes

    def _devices_changed(self, found: dict[str, Any]) -> None:
        log.info("audio devices changed: %s", found)
        self._schedule_restart("devices changed")

    def _schedule_restart(self, reason: str) -> None:
        if not self._restarting and not self._closed and self._loop is not None:
            self._restarting = True
            self._loop.create_task(self._restart(reason))

    async def _monitor_loop(self) -> None:
        """Restart when PortAudio stops calling back (a device vanished). Counts
        callbacks rather than delivered frames, so a busy event loop is not a stall."""
        seen, since = self._callbacks, time.monotonic()
        while True:
            await asyncio.sleep(self.monitor_interval)
            if self._callbacks != seen or self._stream is None or self._restarting:
                seen, since = self._callbacks, time.monotonic()
            elif time.monotonic() - since > self.stall_seconds:
                since = time.monotonic()
                self._schedule_restart("no audio from the input device")

    async def _restart(self, reason: str) -> None:
        try:
            error = ""
            for attempt in range(RESTART_ATTEMPTS):
                if self._closed:
                    return
                with self._lock:
                    self._close_stream()
                    try:
                        self._sd.reinit()
                        self._open()
                    except Exception as e:
                        error = str(e)
                        self._close_stream()
                    else:
                        error = ""
                if not error:
                    break
                await asyncio.sleep(self.retry_delay * (attempt + 1))
            if error:
                self._last_error = error
                self._on_event("failed", {"error": f"cannot reopen audio devices: {error}"})
                return
            self._restarts += 1
            self._on_event(
                "device_changed", {"input": self._names[0], "output": self._names[1], "reason": reason}
            )
        finally:
            self._restarting = False

    # -- the rest of the contract

    def seconds_since_frame(self) -> float:
        return time.monotonic() - self._last_frame

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "input": self._names[0],
            "output": self._names[1],
            "callbacks": self._callbacks,
            "overruns": self._status_flags,
            "played": self._played,
            "stopped": self._stopped,
            "playback_failures": self._failures,
            "restarts": self._restarts,
            "last_error": self._last_error,
        }

    async def close(self) -> None:
        self._closed = True
        if self._watcher is not None:
            await self._watcher.close()
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
        self.stop_playback()
        self._queue.put(None)
        if self._worker is not None:
            await asyncio.get_running_loop().run_in_executor(None, self._worker.join, 2.0)
        self._close_stream()
