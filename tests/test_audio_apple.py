"""AppleAudioIO: the wire protocol, the backend against a fake helper, and the
real Swift helper's --self-test (no audio devices, no microphone permission)."""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from scout import audio_apple
from scout.audio_apple import (
    AppleAudioIO,
    decode_done,
    decode_event,
    decode_header,
    encode_frame,
    encode_play,
    helper_available,
)
from scout.audio_io import FRAME_BYTES

FAKE = Path(__file__).with_name("fake_voiceio.py")
ROOT = Path(__file__).resolve().parents[1]
Events = list[tuple[str, dict[str, Any]]]


def fake(mode: str = "normal", **kwargs: Any) -> AppleAudioIO:
    return AppleAudioIO(helper_cmd=[sys.executable, str(FAKE), mode], **kwargs)


def run(body: Callable[[AppleAudioIO, Events], Awaitable[None]], io: AppleAudioIO) -> Events:
    events: Events = []

    async def go() -> None:
        await io.start(asyncio.get_running_loop(), lambda name, data: events.append((name, data)))
        try:
            await body(io, events)
        finally:
            await io.close()

    asyncio.run(asyncio.wait_for(go(), 30))
    return events


async def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def tone(seconds: float, rate: int = 24000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    out: np.ndarray = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return out


# -- protocol ------------------------------------------------------------------


def test_frame_round_trip() -> None:
    frame = encode_frame(b"E", b'{"event": "ready"}')
    assert frame[:5] == b"E" + struct.pack("<I", 18)
    assert decode_header(frame[:5]) == (b"E", 18)
    assert encode_frame(b"Q") == b"Q\x00\x00\x00\x00"


def test_play_frame_layout() -> None:
    samples = np.array([0.5, -0.25, 1.0], dtype=np.float64)
    frame = encode_play(7, 24000, samples)
    kind, length = decode_header(frame[:5])
    assert (kind, length) == (b"P", 8 + 12)
    assert struct.unpack_from("<II", frame, 5) == (7, 24000)
    assert np.frombuffer(frame[13:], dtype="<f4").tolist() == [0.5, -0.25, 1.0]


def test_done_and_event_decoding() -> None:
    assert decode_done(struct.pack("<IB", 42, 1)) == (42, True)
    assert decode_done(struct.pack("<IB", 43, 0)) == (43, False)
    assert decode_event(b'{"event": "stats", "mic_chunks": 3}') == ("stats", {"mic_chunks": 3})
    with pytest.raises(ValueError, match="not an object"):
        decode_event(b"[1, 2]")


def test_helper_available_tracks_source_mtimes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "Sources" / "main.swift"
    src.parent.mkdir()
    src.write_text("// swift")
    binary = tmp_path / "build" / "voiceio"
    monkeypatch.setattr(audio_apple, "HELPER_DIR", tmp_path)
    monkeypatch.setattr(audio_apple, "HELPER_BIN", binary)
    assert not helper_available()  # not built
    binary.parent.mkdir()
    binary.write_text("bin")
    os.utime(src, (1000, 1000))
    os.utime(binary, (2000, 2000))
    assert helper_available()
    os.utime(src, (3000, 3000))  # source edited after the build
    assert not helper_available()


# -- AppleAudioIO against the fake helper ------------------------------------------


def test_mic_frames_are_exact_30ms() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        await wait_for(lambda: io.frames.qsize() >= 5)
        frames = [io.frames.get_nowait() for _ in range(5)]
        assert all(len(f) == FRAME_BYTES for f in frames)
        # The fake sends a running counter; reassembly must keep it contiguous.
        samples = np.frombuffer(b"".join(frames), dtype="<i2")
        assert samples.tolist() == list(range(samples.size))
        assert io.seconds_since_frame() < 1.0

    io = fake()
    events = run(body, io)
    assert events[0][0] == "ready"
    assert events[0][1]["input_device"] == "Fake Mic"
    assert events[0][1]["config"] == {
        "input_device": "",
        "output_device": "",
        "voice_processing": True,
        "agc": False,
    }
    assert io.ready_info["output_device"] == "Fake Speaker"


def test_config_carries_devices_and_options() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        pass

    io = AppleAudioIO(
        "USB", "Speakers", voice_processing=False, agc=False, helper_cmd=[sys.executable, str(FAKE)]
    )
    events = run(body, io)
    assert events[0][1]["config"] == {
        "input_device": "USB",
        "output_device": "Speakers",
        "voice_processing": False,
        "agc": False,
    }


def test_play_resolves_true_when_done() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        handle = io.play(tone(0.1), 24000)
        assert handle.seconds == pytest.approx(0.1)
        assert io.playing
        assert await asyncio.wait_for(handle.done, 5) is True
        assert not io.playing
        empty = io.play(np.zeros(0, dtype=np.float32), 24000)
        assert await empty.done is True

    io = fake()
    run(body, io)
    assert io.stats()["played"] == 1


def test_stop_playback_resolves_pending_false() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        first = io.play(tone(2.0), 24000)
        second = io.play(tone(2.0), 16000)
        assert first.id != second.id
        await asyncio.sleep(0.05)
        io.stop_playback()
        assert not io.playing
        assert await first.done is False
        assert await second.done is False
        # The helper's own D 0 reports for those ids arrive later and are ignored;
        # playback still works afterwards.
        again = io.play(tone(0.05), 24000)
        assert await asyncio.wait_for(again.done, 5) is True

    io = fake()
    run(body, io)
    stats = io.stats()
    assert (stats["played"], stats["stopped"]) == (1, 2)


def test_events_are_forwarded_and_stats_kept() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        await wait_for(lambda: any(name == "device_changed" for name, _ in events))

    io = fake("device-change")
    events = run(body, io)
    names = [name for name, _ in events]
    assert names[:3] == ["ready", "stats", "device_changed"]
    assert io.ready_info["input_device"] == "Other Mic"
    stats = io.stats()
    assert stats["backend"] == "apple"
    assert stats["helper"] == {"mic_chunks": 0}
    assert stats["devices"] == {"input_device": "Other Mic", "output_device": "Fake Speaker"}


def test_helper_crash_reports_failed_once_and_resolves_handles() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        handle = io.play(tone(5.0), 24000)
        await wait_for(lambda: any(name == "failed" for name, _ in events))
        assert await handle.done is False
        late = io.play(tone(0.1), 24000)
        assert await late.done is False
        assert not io.stats()["helper_alive"]

    events = run(body, fake("crash"))
    failed = [data for name, data in events if name == "failed"]
    assert len(failed) == 1
    assert "code 3" in failed[0]["error"]
    assert "boom" in failed[0]["error"]


def test_helper_failed_event_is_forwarded_once() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        await wait_for(lambda: not io.stats()["helper_alive"])

    events = run(body, fake("fail-later"))
    assert [data for name, data in events if name == "failed"] == [{"error": "device gone for good"}]


def test_start_raises_when_helper_fails() -> None:
    events: Events = []

    async def go(io: AppleAudioIO) -> None:
        await io.start(asyncio.get_running_loop(), lambda name, data: events.append((name, data)))

    io = fake("fail")
    with pytest.raises(RuntimeError, match="fake failure"):
        asyncio.run(go(io))
    assert events == []
    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(go(fake("silent", ready_timeout=0.5)))
    with pytest.raises(RuntimeError, match="cannot start"):
        asyncio.run(go(AppleAudioIO(helper_cmd=[str(ROOT / "no-such-helper")])))


def test_close_quits_the_helper() -> None:
    pids: list[int] = []

    async def body(io: AppleAudioIO, events: Events) -> None:
        pids.append(io.stats()["helper_pid"])
        handle = io.play(tone(5.0), 24000)
        await io.close()
        assert await handle.done is False
        await io.close()  # idempotent

    io = fake()
    events = run(body, io)
    assert not any(name == "failed" for name, _ in events)
    assert io._proc is not None and io._proc.returncode == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_close_kills_a_helper_that_ignores_quit() -> None:
    async def body(io: AppleAudioIO, events: Events) -> None:
        pass

    io = fake("hang", close_timeout=0.3)
    events = run(body, io)
    assert io._proc is not None and io._proc.returncode is not None and io._proc.returncode < 0
    assert not any(name == "failed" for name, _ in events)


def test_play_before_start_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="before start"):
        AppleAudioIO().play(tone(0.1), 24000)


# -- the real Swift helper, in --self-test mode (no audio devices) -------------------


@pytest.fixture(scope="module")
def built_helper() -> Path:
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("needs macOS with swiftc")

    async def build() -> tuple[int | None, bytes]:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            str(ROOT / "scripts" / "build-voiceio.sh"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out

    code, out = asyncio.run(build())
    assert code == 0, out.decode(errors="replace")
    return audio_apple.HELPER_BIN


def test_real_helper_self_test(built_helper: Path) -> None:
    assert helper_available()

    async def body(io: AppleAudioIO, events: Events) -> None:
        await wait_for(lambda: io.frames.qsize() >= 10)
        frame = np.frombuffer(io.frames.get_nowait(), dtype="<i2")
        # 0.1-amplitude 440 Hz tone converted 48 kHz stereo -> 16 kHz mono int16.
        assert 2500 < np.abs(frame).max() < 3700
        played = io.play(tone(0.2), 24000)
        assert await asyncio.wait_for(played.done, 5) is True
        long = io.play(tone(3.0), 22050)
        queued = io.play(tone(1.0), 48000)
        await asyncio.sleep(0.1)
        io.stop_playback()
        assert await long.done is False
        assert await queued.done is False
        await wait_for(lambda: io.stats()["helper"].get("stopped") == 2, timeout=5)

    io = AppleAudioIO("Built-in", helper_cmd=[str(built_helper), "--self-test"])
    events = run(body, io)
    ready = events[0][1]
    assert events[0][0] == "ready"
    assert ready["self_test"] is True
    assert ready["requested_input_device"] == "Built-in"
    assert ready["mic_format"] == {
        "sample_rate": 16000.0,
        "channels": 1,
        "format": "int16",
        "interleaved": True,
    }
    assert io._proc is not None and io._proc.returncode == 0
    helper = io.stats()["helper"]
    assert helper["played"] == 1
    assert helper["mic_convert_errors"] == 0
