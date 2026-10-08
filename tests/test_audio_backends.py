"""The PortAudio backends and their DSP, with no real devices.

- EchoCanceller on a simulated room (offline, through the real WebRTC APM).
- Resampler accuracy and anti-aliasing.
- WebRTCAudioIO / PlainAudioIO playback bookkeeping, frames and device
  restarts against a fake `SoundAPI` passed in through the `sound` argument.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from scout.aec import EchoCanceller, Resampler, resample_offline
from scout.audio_io import FRAME_BYTES, PlayHandle
from scout.audio_plain import PlainAudioIO
from scout.audio_webrtc import BLOCK, WebRTCAudioIO

SR = 48000

# -- signals ------------------------------------------------------------------


def speechlike(seconds: float, f0: float, seed: int) -> np.ndarray:
    """Voiced harmonics with a wandering pitch, noise bursts, syllables and pauses."""
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    t = np.arange(n) / SR
    pitch = f0 * (1 + 0.15 * np.sin(2 * np.pi * 0.7 * t + seed) + 0.05 * np.sin(2 * np.pi * 3.1 * t))
    phase = 2 * np.pi * np.cumsum(pitch) / SR
    voiced = sum((1 / h) * np.sin(h * phase) for h in range(1, 25))
    syllables = np.clip(np.sin(2 * np.pi * 3.7 * t + rng.uniform(0, 6)), 0, None) ** 0.7
    pauses = (np.sin(2 * np.pi * 0.31 * t + seed) > -0.6).astype(float)
    hiss = rng.standard_normal(n) * (np.sin(2 * np.pi * 2.3 * t) > 0.6)
    y = (0.15 * voiced + 0.15 * hiss) * syllables * pauses
    return np.asarray(0.3 * y / np.abs(y).max(), dtype=np.float32)


def room(ref: np.ndarray, delay_ms: float = 40, gain: float = 0.5) -> np.ndarray:
    """Speaker-to-mic path: a delayed, attenuated direct path, a few reflections,
    and gentle low-pass smoothing."""
    d = int(SR * delay_ms / 1000)
    h = np.zeros(d + 2400)
    h[d] = gain
    for offset, g in ((240, 0.2), (700, -0.1), (1500, 0.05)):
        h[d + offset] = g * gain
    h = np.convolve(h, 0.3 * 0.7 ** np.arange(40))
    return np.asarray(np.convolve(ref, h)[: ref.size], dtype=np.float32)


def to_i16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x * 32767), -32768, 32767).astype(np.int16)


def cancel(mic: np.ndarray, ref: np.ndarray, delay_ms: float = 40) -> tuple[np.ndarray, EchoCanceller]:
    aec = EchoCanceller(SR)
    aec.set_delay_ms(delay_ms)
    m, r = to_i16(mic), to_i16(ref)
    out = np.zeros(mic.size, np.float32)
    for i in range(0, mic.size - BLOCK + 1, BLOCK):
        out[i : i + BLOCK] = aec.process(m[i : i + BLOCK], r[i : i + BLOCK]) / 32767.0
    aec.close()
    return out, aec


def db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-20))


def xcorr_peak(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    """Peak normalised cross-correlation magnitude over all lags, and the lag of `a` behind `b`."""
    a64, b64 = a.astype(np.float64), b.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(a.size + b.size)))
    c = np.fft.irfft(np.fft.rfft(a64, n) * np.conj(np.fft.rfft(b64, n)), n)
    i = int(np.abs(c).argmax())
    return float(np.abs(c[i]) / np.sqrt(np.dot(a64, a64) * np.dot(b64, b64))), i if i < n // 2 else i - n


def envelope_db(x: np.ndarray) -> np.ndarray:
    frames = x[: x.size // BLOCK * BLOCK].astype(np.float64).reshape(-1, BLOCK)
    return np.asarray(10 * np.log10(np.mean(frames**2, axis=1) + 1e-10))


# -- echo canceller -------------------------------------------------------------


@pytest.fixture(scope="module")
def room_run() -> dict[str, Any]:
    """20 s of far-end speech through a simulated room; near-end talk from 14 to 19 s."""
    far = speechlike(20, 120, 3)
    echo = room(far)
    near = np.zeros_like(far)
    near[14 * SR : 19 * SR] = speechlike(5, 210, 7)
    noise = (np.random.default_rng(1).standard_normal(far.size) * 10 ** (-65 / 20)).astype(np.float32)
    out, aec = cancel(echo + near + noise, far)
    near_only, _ = cancel(near + noise, np.zeros_like(far))
    return {"far": far, "echo": echo, "near": near, "out": out, "near_only": near_only, "aec": aec}


def test_echo_removed_after_convergence(room_run: dict[str, Any]) -> None:
    echo, out = room_run["echo"], room_run["out"]
    span = slice(6 * SR, 13 * SR)  # echo only, after several seconds to converge
    suppression = db(echo[span]) - db(out[span])
    print(f"echo {db(echo[span]):.1f} dB, residual {db(out[span]):.1f} dB: {suppression:.1f} dB removed")
    assert suppression >= 15
    erle = room_run["aec"].erle_db
    print(f"running ERLE estimate {erle:.1f} dB")
    assert erle is not None
    assert erle >= 15


def test_near_end_speech_survives_double_talk(room_run: dict[str, Any]) -> None:
    span = slice(14 * SR + SR // 2, 19 * SR)
    near, out, near_only = room_run["near"][span], room_run["out"][span], room_run["near_only"][span]
    level_diff = db(out) - db(near)
    # Waveform match against the same processing without echo (high-pass, noise
    # suppression and the band-split filters all reshape the signal).
    waveform, _ = xcorr_peak(out, near_only)
    # Loudness contour (10 ms power) against the clean near-end talker, after
    # removing the processor's own delay (~15 ms of band-split filtering and NS).
    _, lag = xcorr_peak(np.abs(out), np.abs(near))
    env_out, env_near = envelope_db(out[lag:]), envelope_db(near[: near.size - lag])
    contour = float(np.corrcoef(10 ** (env_out / 10), 10 ** (env_near / 10))[0, 1])
    # AEC3's suppressor ducks the near end somewhat while the far end talks;
    # no half-second stretch may lose it.
    half = SR // 2
    worst = min(
        db(out[i + lag : i + lag + half]) - db(near[i : i + half])
        for i in range(0, 4 * SR, half)
        if db(near[i : i + half]) > -60
    )
    print(
        f"double talk: level {level_diff:+.1f} dB (worst 0.5 s {worst:+.1f} dB), "
        f"waveform corr {waveform:.2f}, power contour corr {contour:.2f}"
    )
    assert abs(level_diff) <= 5
    assert worst >= -10
    assert waveform >= 0.6
    assert contour >= 0.6


# -- resampling -------------------------------------------------------------------


def tone(freq: float, rate: int, seconds: float = 1.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return np.asarray(amp * np.sin(2 * np.pi * freq * t), dtype=np.float32)


def decimate_stream(x: np.ndarray, chunk: int = BLOCK) -> np.ndarray:
    r = Resampler(SR, 16000)
    return np.concatenate([r.process(x[i : i + chunk]) for i in range(0, x.size, chunk)])


@pytest.mark.parametrize("freq", [300.0, 1000.0, 4000.0, 6500.0])
def test_decimator_keeps_speech_band_tones(freq: float) -> None:
    y = decimate_stream(tone(freq, SR))[400:]  # skip the filter's start-up
    spectrum = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    peak = np.fft.rfftfreq(y.size, 1 / 16000)[spectrum.argmax()]
    assert abs(peak - freq) < 5
    assert abs(db(y) - db(tone(freq, SR))) < 1.0


@pytest.mark.parametrize("freq", [9000.0, 10000.0, 12000.0, 15000.0])
def test_decimator_rejects_tones_above_8k(freq: float) -> None:
    # Without filtering a 10 kHz tone would alias to 6 kHz at full strength.
    y = decimate_stream(tone(freq, SR))[400:]
    assert db(y) - db(tone(freq, SR)) < -60


def test_decimator_state_carries_across_odd_chunks() -> None:
    x = tone(1234.0, SR, 0.5) + tone(300.0, SR, 0.5)
    whole = decimate_stream(x, chunk=x.size)
    pieces = decimate_stream(x, chunk=317)
    assert whole.size == pieces.size == x.size // 3
    assert np.abs(whole - pieces).max() < 1e-5


@pytest.mark.parametrize("src", [24000, 22050, 16000])
def test_playback_upsampling_matches_ideal_tone(src: int) -> None:
    y = resample_offline(tone(440.0, src), src, SR)
    ideal = tone(440.0, SR)
    assert y.size == ideal.size
    # Away from the clip edges the output matches a tone generated at 48k.
    assert np.abs(y[2000:-2000] - ideal[2000:-2000]).max() < 2e-3


def test_upsampling_does_not_image() -> None:
    # 24k -> 48k: a 5 kHz tone must not leave an image at 19 kHz.
    y = resample_offline(tone(5000.0, 24000), 24000, SR)[2000:-2000]
    spectrum = np.abs(np.fft.rfft(y * np.hanning(y.size)))
    freqs = np.fft.rfftfreq(y.size, 1 / SR)
    image = spectrum[(freqs > 18500) & (freqs < 19500)].max()
    assert 20 * np.log10(image / spectrum.max()) < -60


# -- fake PortAudio ----------------------------------------------------------------


class FakeStatus:
    def __init__(self, input_overflow: bool = False, output_underflow: bool = False) -> None:
        self.input_overflow, self.output_underflow = input_overflow, output_underflow

    def __bool__(self) -> bool:
        return self.input_overflow or self.output_underflow


class FakeStream:
    def __init__(self, duplex: bool, kwargs: dict[str, Any]) -> None:
        self.duplex = duplex
        self.kwargs = kwargs
        self.callback: Callable[..., None] = kwargs["callback"]
        self.latency = (0.004, 0.006)
        self.device = kwargs["device"]
        self.started = self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def tick(self, mic: np.ndarray | None = None, status: Any = 0) -> np.ndarray:
        """Run one duplex callback; returns what was written to the speaker."""
        block = self.kwargs["blocksize"]
        indata = np.zeros((block, 1), np.int16) if mic is None else mic.reshape(block, 1)
        out_ch = self.kwargs["channels"][1] if isinstance(self.kwargs.get("channels"), tuple) else 1
        outdata = np.full((block, out_ch), 12345, np.int16)  # garbage the callback must overwrite
        self.callback(indata, outdata, block, None, status)
        self.last_out = outdata.copy()
        return outdata[:, 0].copy()


class FakeSound:
    """Stands in for SoundDevice. Plain playback blocks in wait() until the test
    calls finish() (the clip ended) or the backend calls stop()."""

    def __init__(self) -> None:
        self.streams: list[FakeStream] = []
        self.played: list[tuple[np.ndarray, int, int | None]] = []
        self.reinits = 0
        self.fail_opens = 0
        self._done = threading.Event()
        self.playing = threading.Event()
        self.stops = 0
        self.speaker_channels = 1  # set 2 to be a stereo (or HDMI) output

    def _open(self, duplex: bool, kwargs: dict[str, Any]) -> FakeStream:
        if self.fail_opens:
            self.fail_opens -= 1
            raise OSError("device unavailable")
        stream = FakeStream(duplex, kwargs)
        self.streams.append(stream)
        return stream

    def input_stream(self, **kwargs: Any) -> FakeStream:
        return self._open(False, kwargs)

    def duplex_stream(self, **kwargs: Any) -> FakeStream:
        return self._open(True, kwargs)

    def play(self, samples: np.ndarray, sample_rate: int, device: int | None) -> None:
        self._done.clear()
        self.played.append((samples, sample_rate, device))
        self.playing.set()

    def wait(self) -> None:
        self._done.wait(5)
        self.playing.clear()

    def finish(self) -> None:
        self._done.set()

    def stop(self) -> None:
        self.stops += 1
        self._done.set()

    def reinit(self) -> None:
        self.reinits += 1

    def resolve(self, spec: str, kind: str) -> int | None:
        return int(spec) if spec else None

    def device_name(self, device: int | None, kind: str) -> str:
        return f"fake {kind} {device}"

    def output_channels(self, device: int | None) -> int:
        return self.speaker_channels


Events = list[tuple[str, dict[str, Any]]]


async def settle(seconds: float = 0.0) -> None:
    await asyncio.sleep(seconds)
    await asyncio.sleep(0)


async def start_webrtc() -> tuple[WebRTCAudioIO, FakeSound, Events]:
    sound = FakeSound()
    io = WebRTCAudioIO("3", "", sound=sound, watch_devices=False)
    events: Events = []
    await io.start(asyncio.get_running_loop(), lambda kind, info: events.append((kind, info)))
    return io, sound, events


# -- WebRTCAudioIO -----------------------------------------------------------------


def test_webrtc_opens_one_duplex_stream_at_48k() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        (stream,) = sound.streams
        assert stream.duplex and stream.started
        assert stream.kwargs["samplerate"] == SR and stream.kwargs["blocksize"] == BLOCK
        assert stream.kwargs["device"] == (3, None)
        assert io.stats()["delay_ms"] == 10  # input + output latency
        await io.close()
        assert stream.closed

    asyncio.run(go())


def test_webrtc_plays_mono_on_both_channels_of_a_stereo_output() -> None:
    """An HDMI TV or stereo speakers: opened as one channel, only the left one plays."""

    async def go() -> None:
        sound = FakeSound()
        sound.speaker_channels = 2
        io = WebRTCAudioIO("3", "", sound=sound, watch_devices=False)
        await io.start(asyncio.get_running_loop(), lambda kind, info: None)
        stream = sound.streams[0]
        assert stream.kwargs["channels"] == (1, 2)
        io.play(tone(440.0, 24000, 0.05, amp=0.25), 24000)
        stream.tick()
        left, right = stream.last_out[:, 0], stream.last_out[:, 1]
        assert left.any() and np.array_equal(left, right)
        await io.close()

    asyncio.run(go())


def test_webrtc_plays_queued_audio_and_resolves_true() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        stream = sound.streams[0]
        clip = tone(440.0, 24000, 0.1, amp=0.25)  # 100 ms -> 4800 samples at 48k
        first, second = io.play(clip, 24000), io.play(clip, 24000)
        assert io.playing and first.seconds == pytest.approx(0.1)
        written = np.concatenate([stream.tick() for _ in range(25)])
        await settle(0.02)  # past the 6 ms output latency
        assert first.done.result() is True and second.done.result() is True
        expected = to_i16(resample_offline(clip, 24000, SR))
        assert np.array_equal(written[:4800], expected)
        assert np.array_equal(written[4800:9600], expected)
        assert not written[9600:].any()  # silence once the queue is empty
        assert not io.playing
        assert io.stats()["played"] == 2
        await io.close()

    asyncio.run(go())


def test_webrtc_done_waits_for_the_last_sample() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        stream = sound.streams[0]
        handle = io.play(np.full(BLOCK * 3, 0.1, np.float32), SR)
        for _ in range(2):
            stream.tick()
        await settle(0.02)
        assert not handle.done.done() and io.playing
        stream.tick()
        await settle(0.02)
        assert handle.done.result() is True
        await io.close()

    asyncio.run(go())


def test_webrtc_stop_silences_and_resolves_pending_false() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        stream = sound.streams[0]
        handles: list[PlayHandle] = [io.play(np.full(SR, 0.2, np.float32), SR) for _ in range(3)]
        assert stream.tick().any()
        io.stop_playback()
        assert all(h.done.result() is False for h in handles)
        assert not io.playing
        assert not stream.tick().any()  # the very next block is silent
        later = io.play(np.full(BLOCK, 0.2, np.float32), SR)  # playback works after a stop
        assert stream.tick().any()
        await settle(0.02)
        assert later.done.result() is True
        assert io.stats()["stopped"] == 3
        await io.close()

    asyncio.run(go())


def test_webrtc_delivers_16k_30ms_frames() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        stream = sound.streams[0]
        mic = to_i16(tone(1000.0, SR, 1.0, amp=0.3))
        for i in range(0, mic.size, BLOCK):
            stream.tick(mic[i : i + BLOCK])
        await settle()
        frames = [io.frames.get_nowait() for _ in range(io.frames.qsize())]
        assert len(frames) == 33  # 100 blocks of 10 ms -> 33 whole 30 ms frames
        assert all(len(f) == FRAME_BYTES for f in frames)
        pcm = np.frombuffer(b"".join(frames[5:]), np.int16).astype(np.float32) / 32768
        spectrum = np.abs(np.fft.rfft(pcm * np.hanning(pcm.size)))
        assert abs(np.fft.rfftfreq(pcm.size, 1 / 16000)[spectrum.argmax()] - 1000) < 20
        assert io.seconds_since_frame() < 1
        await io.close()

    asyncio.run(go())


def test_webrtc_counts_overruns() -> None:
    async def go() -> None:
        io, sound, _ = await start_webrtc()
        stream = sound.streams[0]
        stream.tick(status=FakeStatus(input_overflow=True))
        stream.tick(status=FakeStatus(output_underflow=True))
        stream.tick()
        stats = io.stats()
        assert stats["callbacks"] == 3 and stats["overruns"] == 2
        assert stats["input_overflows"] == 1 and stats["output_underflows"] == 1
        assert stats["callback_errors"] == 0
        await io.close()

    asyncio.run(go())


def test_webrtc_device_change_reopens_and_keeps_playing() -> None:
    async def go() -> None:
        io, sound, events = await start_webrtc()
        handle = io.play(np.full(BLOCK * 4, 0.2, np.float32), SR)
        sound.streams[0].tick()
        io._devices_changed({"input": "new mic", "output": "new speaker"})
        await settle(0.01)
        assert sound.reinits == 1 and len(sound.streams) == 2
        assert sound.streams[0].closed and sound.streams[1].started
        assert events == [
            (
                "device_changed",
                {"input": "fake input 3", "output": "fake output None", "reason": "devices changed"},
            )
        ]
        for _ in range(3):
            assert sound.streams[1].tick().any()
        await settle(0.02)
        assert handle.done.result() is True
        await io.close()

    asyncio.run(go())


def test_webrtc_restarts_when_callbacks_stop() -> None:
    async def go() -> None:
        sound = FakeSound()
        io = WebRTCAudioIO(sound=sound, watch_devices=False)
        io.stall_seconds, io.monitor_interval = 0.05, 0.01
        events: Events = []
        await io.start(asyncio.get_running_loop(), lambda kind, info: events.append((kind, info)))
        for _ in range(5):  # a live stream is left alone
            sound.streams[-1].tick()
            await asyncio.sleep(0.02)
        assert len(sound.streams) == 1
        await wait_for(lambda: len(sound.streams) == 2)  # no callbacks now: reopened
        await settle()
        assert events[0][0] == "device_changed" and events[0][1]["reason"] == "no audio from the stream"
        await io.close()

    asyncio.run(go())


def test_webrtc_reports_failed_when_it_cannot_reopen() -> None:
    async def go() -> None:
        io, sound, events = await start_webrtc()
        io.retry_delay = 0.001
        sound.fail_opens = 3
        io._devices_changed({})
        await settle(0.05)
        assert [kind for kind, _ in events] == ["failed"]
        assert "device unavailable" in events[0][1]["error"]
        await io.close()

    asyncio.run(go())


# -- PlainAudioIO ------------------------------------------------------------------


async def start_plain() -> tuple[PlainAudioIO, FakeSound, Events]:
    sound = FakeSound()
    io = PlainAudioIO("", "2", sound=sound, watch_devices=False)
    events: Events = []
    await io.start(asyncio.get_running_loop(), lambda kind, info: events.append((kind, info)))
    return io, sound, events


async def wait_for(cond: Callable[[], bool]) -> None:
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def test_plain_input_is_a_16k_stream_of_30ms_frames() -> None:
    async def go() -> None:
        io, sound, _ = await start_plain()
        (stream,) = sound.streams
        assert not stream.duplex
        assert stream.kwargs["samplerate"] == 16000 and stream.kwargs["blocksize"] == 480
        frame = np.arange(480, dtype=np.int16).tobytes()
        stream.callback(frame, 480, None, 0)
        await settle()
        assert io.frames.get_nowait() == frame
        await io.close()

    asyncio.run(go())


def test_plain_plays_in_order_and_resolves() -> None:
    async def go() -> None:
        io, sound, _ = await start_plain()
        a = io.play(tone(440.0, 24000, 0.2), 24000)
        b = io.play(tone(660.0, 24000, 0.2), 24000)
        assert io.playing
        await wait_for(lambda: len(sound.played) == 1)
        samples, rate, device = sound.played[0]
        assert rate == 24000 and device == 2 and samples.size == 4800  # played as given
        sound.finish()
        assert await a.done is True
        await wait_for(lambda: len(sound.played) == 2)
        sound.finish()
        assert await b.done is True
        assert not io.playing
        await io.close()

    asyncio.run(go())


def test_plain_stop_resolves_current_and_queued_false() -> None:
    async def go() -> None:
        io, sound, _ = await start_plain()
        handles = [io.play(tone(440.0, 24000, 0.2), 24000) for _ in range(3)]
        await wait_for(lambda: len(sound.played) == 1)
        io.stop_playback()
        assert sound.stops == 1
        assert [h.done.result() for h in handles] == [False, False, False]
        assert not io.playing
        await asyncio.sleep(0.05)
        assert len(sound.played) == 1  # the queued clips were dropped, not played
        after = io.play(tone(440.0, 24000, 0.1), 24000)
        await wait_for(lambda: len(sound.played) == 2)
        sound.finish()
        assert await after.done is True
        await io.close()

    asyncio.run(go())


def test_plain_device_change_reopens_input() -> None:
    async def go() -> None:
        io, sound, events = await start_plain()
        io._devices_changed({"input": "x"})
        await settle(0.01)
        assert sound.reinits == 1 and len(sound.streams) == 2 and sound.streams[0].closed
        assert events[0][0] == "device_changed"
        await io.close()

    asyncio.run(go())
