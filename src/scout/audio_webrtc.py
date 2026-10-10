"""WebRTC backend: AEC3 + noise suppression on one full-duplex PortAudio stream.

One `sounddevice.Stream` (input and output together) runs at 48 kHz in 10 ms
blocks. Each callback takes the next 10 ms of queued playback (silence when
there is none) and writes it to the speaker, then passes the microphone block
and that exact output block to the echo canceller: the reference is
sample-aligned with what was played, and the canceller only has to learn the
fixed acoustic and device delay. The cleaned block is decimated to 16 kHz and
handed to the asyncio loop, which assembles 30 ms frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from typing import Any

import numpy as np

from .aec import EchoCanceller, Resampler, resample_offline
from .audio_io import EventHandler, FrameAssembler, PlayHandle
from .audio_plain import (
    RESTART_ATTEMPTS,
    STALL_SECONDS,
    DeviceWatcher,
    SoundAPI,
    SoundDevice,
    Stream,
    speaker_channels,
)

log = logging.getLogger(__name__)

RATE = 48000
BLOCK = RATE // 100  # 10 ms


class PlaybackRing:
    """Queued output audio (48 kHz int16), each piece tagged with its handle id.

    `read_into` runs in the audio callback: it fills one block, zero-pads when
    the queue runs dry, and returns the ids whose last sample it just wrote.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segs: deque[list[Any]] = deque()  # [id, samples, position]
        self.samples = 0  # queued samples not yet output

    def push(self, hid: int, samples: np.ndarray) -> None:
        with self._lock:
            self._segs.append([hid, samples, 0])
            self.samples += samples.size

    def read_into(self, out: np.ndarray) -> list[int] | None:
        finished: list[int] | None = None
        n, filled = out.size, 0
        with self._lock:
            while filled < n and self._segs:
                seg = self._segs[0]
                data, pos = seg[1], seg[2]
                take = min(n - filled, data.size - pos)
                out[filled : filled + take] = data[pos : pos + take]
                filled += take
                seg[2] = pos + take
                if seg[2] >= data.size:
                    self._segs.popleft()
                    if finished is None:
                        finished = []
                    finished.append(seg[0])
            self.samples -= filled
        if filled < n:
            out[filled:] = 0
        return finished

    def clear(self) -> list[int]:
        with self._lock:
            ids = [seg[0] for seg in self._segs]
            self._segs.clear()
            self.samples = 0
        return ids

    @property
    def active(self) -> bool:
        return bool(self._segs)


def _total_latency(latency: Any) -> tuple[float, float]:
    if isinstance(latency, (tuple, list)):
        return float(latency[0]), float(latency[1])
    return float(latency), float(latency)


class WebRTCAudioIO:
    name = "webrtc"
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
        auto_gain_control: bool = False,
        noise_suppression: bool = True,
        latency: str | float = "low",
        output_delay_ms: float = 0.0,
    ) -> None:
        self.input_device, self.output_device = input_device, output_device
        # Delay the output device adds after the computer hands it audio, which
        # PortAudio can't see (a TV's sound processing: ~750 ms measured on a Samsung
        # over HDMI). It's added to the canceller's delay hint and to when playback
        # counts as finished.
        self._delay_out = max(0.0, output_delay_ms) / 1000
        self._extra_out = 0.0  # set per open: the delay belongs to the preferred device only
        # The preferred output's name (the first part of a "TV|default" spec): its delay
        # doesn't apply when playing on a fallback.
        self._delay_for = output_device.split("|", 1)[0].strip().lower()
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)
        self._sd: SoundAPI = sound if sound is not None else SoundDevice()
        self._watch = watch_devices
        self._poll_interval = poll_interval
        self._latency_setting = latency
        self._aec = EchoCanceller(
            RATE, noise_suppression=noise_suppression, auto_gain_control=auto_gain_control
        )
        self._decim = Resampler(RATE, 16000)
        self._assembler = FrameAssembler()
        self._ring = PlaybackRing()
        self._out_block = np.zeros(BLOCK, np.int16)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_event: EventHandler = lambda _kind, _info: None
        self._stream: Stream | None = None
        self._stream_lock = threading.Lock()
        self._names = ("", "")
        self._latency = (0.0, 0.0)
        self._handles: dict[int, PlayHandle] = {}
        self._next_id = 0
        self._audible_until = 0.0  # when the last finished piece leaves the speaker
        self._last_frame = time.monotonic()
        # Callback counters (written only by the audio thread).
        self._callbacks = 0
        self._overruns = 0
        self._input_overflows = 0
        self._output_underflows = 0
        self._odd_blocks = 0
        self._callback_errors = 0
        self._last_error = ""
        self._cpu_total = 0.0
        self._cpu_max = 0.0
        self._played = 0
        self._stopped = 0
        self._restarts = 0
        self._restarting = False
        self._watcher: DeviceWatcher | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle

    async def start(self, loop: asyncio.AbstractEventLoop, on_event: EventHandler) -> None:
        self._loop, self._on_event = loop, on_event
        self._open()
        self._monitor = loop.create_task(self._monitor_loop())
        if self._watch:
            self._watcher = DeviceWatcher(
                self.input_device, self.output_device, self._devices_changed, self._poll_interval
            )
            await self._watcher.start()

    def _open(self) -> None:
        in_dev = self._sd.resolve(self.input_device, "input")
        out_dev = self._sd.resolve(self.output_device, "output")
        stream = self._sd.duplex_stream(
            samplerate=RATE,
            blocksize=BLOCK,
            channels=(1, speaker_channels(self._sd, out_dev)),
            dtype="int16",
            device=(in_dev, out_dev),
            latency=self._latency_setting,
            callback=self._callback,
        )
        self._latency = _total_latency(stream.latency)
        out_name = self._sd.device_name(out_dev, "output").lower()
        self._extra_out = self._delay_out if (not self._delay_for or self._delay_for in out_name) else 0.0
        # The reference block is processed when it is written; its echo comes
        # back after the output and input latencies of the stream.
        self._aec.set_delay_ms(1000 * (sum(self._latency) + self._extra_out))
        self._last_frame = time.monotonic()
        stream.start()
        self._stream = stream
        self._names = (self._sd.device_name(in_dev, "input"), self._sd.device_name(out_dev, "output"))
        log.info(
            "audio (webrtc): in %s, out %s, latency %.0f/%.0f ms",
            *self._names,
            1000 * self._latency[0],
            1000 * self._latency[1],
        )

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                log.debug("closing duplex stream: %s", e)

    async def close(self) -> None:
        self._closed = True
        if self._watcher is not None:
            await self._watcher.close()
        if self._monitor is not None:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
        self.stop_playback()
        with self._stream_lock:
            self._close_stream()
        self._aec.close()

    # -- the audio callback (PortAudio's thread)

    def _callback(self, indata: Any, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        t0 = time.perf_counter()
        self._callbacks += 1
        if status:
            self._overruns += 1
            if getattr(status, "input_overflow", False):
                self._input_overflows += 1
            if getattr(status, "output_underflow", False):
                self._output_underflows += 1
        finished: list[int] | None = None
        loop = self._loop
        try:
            if frames != BLOCK:
                self._odd_blocks += 1
                outdata.fill(0)
                return
            out = self._out_block
            finished = self._ring.read_into(out)
            outdata[:] = out[:, None]  # the same mono signal on every speaker channel
            cleaned = self._aec.process(indata[:, 0], out)
            pcm = np.clip(np.rint(self._decim.process(cleaned)), -32768, 32767).astype("<i2").tobytes()
            if loop is not None:
                loop.call_soon_threadsafe(self._deliver, pcm, finished)
        except Exception as e:
            self._callback_errors += 1
            self._last_error = f"callback: {e!r}"
            with contextlib.suppress(Exception):
                if finished is None:
                    outdata.fill(0)
                elif loop is not None:  # still report what finished playing
                    loop.call_soon_threadsafe(self._deliver, b"", finished)
        finally:
            dt = time.perf_counter() - t0
            self._cpu_total += dt
            self._cpu_max = max(self._cpu_max, dt)

    # -- loop side

    def _deliver(self, pcm: bytes, finished: list[int] | None) -> None:
        self._last_frame = time.monotonic()
        for frame in self._assembler.push(pcm):
            if self.frames.full():
                self.frames.get_nowait()
            self.frames.put_nowait(frame)
        loop = self._loop
        if finished and loop is not None:
            # The last sample was just handed to the device; it is heard one
            # output latency later.
            delay = self._latency[1] + self._extra_out
            self._audible_until = time.monotonic() + delay
            for hid in finished:
                handle = self._handles.pop(hid, None)
                if handle is not None:
                    loop.call_later(delay, self._finish, handle, True)

    def _finish(self, handle: PlayHandle, played: bool) -> None:
        if not handle.done.done():
            handle.done.set_result(played)
            if played:
                self._played += 1
            else:
                self._stopped += 1

    def play(self, samples: np.ndarray, sample_rate: int) -> PlayHandle:
        if self._loop is None:
            raise RuntimeError("start() the audio backend before playing")
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        out = resample_offline(data, sample_rate, RATE)
        pcm = np.clip(np.rint(out * 32767.0), -32768, 32767).astype(np.int16)
        self._next_id += 1
        handle = PlayHandle(self._next_id, data.size / sample_rate, self._loop.create_future())
        if pcm.size == 0:
            handle.done.set_result(True)
            return handle
        self._handles[handle.id] = handle
        self._ring.push(handle.id, pcm)
        return handle

    def stop_playback(self) -> None:
        self._ring.clear()
        self._audible_until = 0.0
        for hid in list(self._handles):
            self._finish(self._handles.pop(hid), False)

    @property
    def playing(self) -> bool:
        return self._ring.active or bool(self._handles) or time.monotonic() < self._audible_until

    def seconds_since_frame(self) -> float:
        return time.monotonic() - self._last_frame

    def stats(self) -> dict[str, Any]:
        n = max(self._callbacks, 1)
        erle = self._aec.erle_db
        return {
            "backend": self.name,
            "input": self._names[0],
            "output": self._names[1],
            "callbacks": self._callbacks,
            "overruns": self._overruns,
            "input_overflows": self._input_overflows,
            "output_underflows": self._output_underflows,
            "odd_blocks": self._odd_blocks,
            "callback_errors": self._callback_errors,
            "latency_in_ms": round(1000 * self._latency[0], 1),
            "latency_out_ms": round(1000 * self._latency[1], 1),
            "delay_ms": self._aec.delay_ms,
            "erle_db": None if erle is None else round(erle, 1),
            "echo_learned_s": round(self._aec.learned_seconds, 1),
            "callback_us_avg": round(1e6 * self._cpu_total / n, 1),
            "callback_us_max": round(1e6 * self._cpu_max, 1),
            "queued_seconds": round(self._ring.samples / RATE, 3),
            "played": self._played,
            "stopped": self._stopped,
            "restarts": self._restarts,
            "last_error": self._last_error,
        }

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
                self._schedule_restart("no audio from the stream")

    async def _restart(self, reason: str) -> None:
        """Reopen the stream on the current devices. Queued playback carries on afterwards."""
        try:
            error = ""
            for attempt in range(RESTART_ATTEMPTS):
                if self._closed:
                    return
                with self._stream_lock:
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
