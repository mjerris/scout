"""Echo-path calibration per device pair (echo_cal.py), on synthetic recordings with
known delays, and through the webrtc backend with a fake PortAudio stream."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from test_audio_backends import SR, FakeSound, room, speechlike

from scout import calls, echo_cal
from scout.aec import plan
from scout.audio_webrtc import WebRTCAudioIO
from scout.echo_cal import Calibrator, Estimate, estimate


@pytest.fixture
def where() -> Iterator[Path]:
    d = Path(__file__).resolve().parents[1] / ".tmp" / "echo-cal" / uuid.uuid4().hex
    d.mkdir(parents=True)
    yield d / "audio_devices.json"
    shutil.rmtree(d, ignore_errors=True)


def recording(delay_ms: float | None, seconds: float = 4, seed: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """(what was played, what the mic heard): the room's echo at `delay_ms`, or None for
    headphones (the user talking, no echo), with a little noise."""
    far = speechlike(seconds, 120, seed)
    noise = np.random.default_rng(seed).standard_normal(far.size).astype(np.float32) * 1e-3
    if delay_ms is None:
        return far, 0.5 * speechlike(seconds, 210, seed + 6) + noise
    return far, room(far, delay_ms=delay_ms, gain=0.3) + noise


# --- measuring ------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [0, 300, 780, 950])
def test_the_delay_is_measured_from_playback(delay: float) -> None:
    est = estimate(*recording(delay))
    assert est is not None and est.echo and est.confident
    assert est.delay_ms is not None and abs(est.delay_ms - delay) <= 2
    assert -14 < est.echo_db < -8  # the room's 0.3 gain, about -10 dB


def test_headphones_have_no_echo_path() -> None:
    est = estimate(*recording(None))
    assert est is not None and not est.echo and est.delay_ms is None


def test_too_little_playback_measures_nothing() -> None:
    far, mic = recording(300)
    far[SR // 2 :] = 0
    assert estimate(far, mic) is None


@pytest.mark.parametrize(
    ("delay", "decision"),
    [(0, (0, 0)), (80, (0, 80)), (300, (0, 300)), (780, (580, 200)), (950, (750, 200))],
)
def test_a_delay_line_only_past_what_aec3_finds_itself(delay: float, decision: tuple[int, int]) -> None:
    assert plan(delay) == decision


# --- deciding, keeping, re-checking -----------------------------------------------------


def est(delay: float | None, psr: float = 9.0) -> Estimate:
    return Estimate(delay, psr, 0.9 if delay is not None else 0.01, -10.0, delay is not None)


def test_three_agreeing_measurements_decide_and_are_kept(where: Path) -> None:
    cal = Calibrator(where)
    assert cal.select("OBSBOT", "SAMSUNG", 780.0) == 780.0 and cal.wanted
    assert cal.add(est(801)) is None and cal.add(est(812)) is None
    assert cal.add(est(5, psr=1.2)) is None  # no clear peak: not counted
    assert cal.add(est(805)) == 805
    stored = cal.pairs["OBSBOT -> SAMSUNG"]
    assert stored["decision"] == "delay_line" and stored["ref_delay_ms"] == 600 and not cal.wanted
    again = Calibrator(where)  # the next start
    assert again.select("OBSBOT", "SAMSUNG", 780.0) == 805 and not again.wanted


def test_a_short_path_is_left_to_aec3(where: Path) -> None:
    cal = Calibrator(where)
    cal.select("mic", "Mac mini Speakers", 80.0)
    for d in (95, 96, 95):
        cal.add(est(d))
    assert cal.pairs["mic -> Mac mini Speakers"]["decision"] == "aec3"
    assert cal.pairs["mic -> Mac mini Speakers"]["ref_delay_ms"] == 0


def test_headphones_are_recorded_as_no_echo_path(where: Path) -> None:
    cal = Calibrator(where)
    cal.select("AirPods", "AirPods", 150.0)
    for _ in range(3):
        assert cal.add(est(None)) is None  # nothing to change in the cancelling
    assert cal.pairs["AirPods -> AirPods"]["decision"] == "none"


def test_another_device_pair_is_measured_anew(where: Path) -> None:
    cal = Calibrator(where)
    cal.select("OBSBOT", "SAMSUNG", 780.0)
    for d in (800, 801, 802):
        cal.add(est(d))
    assert cal.select("OBSBOT", "Mac mini Speakers", 80.0) == 80.0 and cal.wanted
    assert cal.select("OBSBOT", "SAMSUNG", 780.0) == 801 and not cal.wanted


def test_drift_measures_again(where: Path) -> None:
    cal = Calibrator(where)
    cal.select("OBSBOT", "SAMSUNG", 780.0)
    for d in (780, 781, 782):
        cal.add(est(d))
    for d in (790, 795, 792):  # within DRIFT_MS: kept
        assert cal.add(est(d)) is None
    assert cal.pairs["OBSBOT -> SAMSUNG"]["delay_ms"] == 781
    assert cal.add(est(900)) is None and cal.add(est(905)) is None
    assert cal.add(est(902)) == 902  # the TV's processing changed
    assert Calibrator(where).pairs["OBSBOT -> SAMSUNG"]["delay_ms"] == 902


def test_echo_left_over_measures_again(where: Path) -> None:
    cal = Calibrator(where)
    cal.select("OBSBOT", "SAMSUNG", 780.0)
    for d in (780, 781, 782):
        cal.add(est(d))
    cal.residual(20.0, 60.0)
    assert not cal.wanted
    cal.residual(2.0, 5.0)  # too early to tell
    assert not cal.wanted
    cal.residual(2.0, 60.0)
    assert cal.wanted
    for d in (781, 782, 780):  # measured again (the same, this time) and settled
        cal.add(est(d))
    assert not cal.wanted


def test_show_and_reset(where: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    cal = Calibrator(where)
    cal.select("OBSBOT", "SAMSUNG", 780.0)
    for d in (780, 781, 782):
        cal.add(est(d))
    assert echo_cal.main(["show"], where) == 0
    assert "OBSBOT -> SAMSUNG: 781" in capsys.readouterr().out
    assert echo_cal.main(["reset"], where) == 0
    assert not where.exists() and Calibrator(where).pairs == {}


# --- the active fallback's conditions -----------------------------------------------------


def _room(**kw: Any) -> Any:
    a = SimpleNamespace(
        state="idle",
        speaker=SimpleNamespace(busy=False),
        mic_muted=False,
        floor=SimpleNamespace(owner=lambda: None),
        last_onset=0.0,
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_the_sweep_plays_only_into_a_quiet_idle_room(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import chirp_ok

    monkeypatch.setattr(calls, "last_started", 0.0)
    now = 10_000.0
    assert chirp_ok(_room(), now, last_chirp=0.0)
    assert not chirp_ok(_room(last_onset=now - 5), now, 0.0)  # someone is talking
    assert not chirp_ok(_room(state="speaking"), now, 0.0)
    assert not chirp_ok(_room(floor=SimpleNamespace(owner=lambda: "desk#1")), now, 0.0)
    assert not chirp_ok(_room(mic_muted=True), now, 0.0)
    assert not chirp_ok(_room(), now, last_chirp=now - 60)  # one every ten minutes
    monkeypatch.setattr(calls, "last_started", time.time() - 600)
    assert not chirp_ok(_room(), now, 0.0)  # a call through the iPhone plays on the Mac


# --- through the webrtc backend -------------------------------------------------------------


def test_the_backend_measures_its_own_playback_and_applies_it(where: Path) -> None:
    """A TV 780 ms behind: the passive check measures it from Scout's speech, keeps it,
    and the canceller holds its reference back by the measured amount."""

    async def go() -> tuple[dict[str, Any], int, int]:
        sound = FakeSound()
        cal = Calibrator(where)
        io = WebRTCAudioIO("3", "", sound=sound, watch_devices=False, calibration=cal)
        await io.start(asyncio.get_running_loop(), lambda kind, info: None)
        assert io._aec.ref_delay_ms == 0  # not measured yet: the stream latency alone
        stream = sound.streams[0]
        lag = 78  # blocks: 780 ms
        outs: list[np.ndarray] = []
        for i in range(3):
            io.play(speechlike(2.5, 120, 3 + i), SR)
            for _ in range(250 + 160):  # the speech, then the quiet tail
                mic = (0.3 * outs[-lag]).astype(np.int16) if len(outs) >= lag else None
                outs.append(stream.tick(mic))
            for _ in range(100):
                await asyncio.sleep(0.01)
                if not io._measuring:
                    break
        stream.tick()  # applies the measured delay between blocks
        stats = io.stats()
        await io.close()
        return stats, io._aec.ref_delay_ms, io._aec.delay_ms

    stats, ref_delay, delay = run(go())
    stored = stats["calibration"]["stored"]
    assert stored is not None and abs(stored["delay_ms"] - 780) <= 2, stats["calibration"]
    assert abs(delay - 780) <= 2 and ref_delay in (570, 580)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_the_measuring_sweep_is_quiet_and_measures_a_tv() -> None:
    from scout.aec import resample_offline

    s = resample_offline(echo_cal.sweep(), 24000, SR)
    assert 20 * np.log10(np.abs(s).max()) <= -25  # quiet
    tail = np.zeros(int(1.2 * SR), np.float32)
    far = np.concatenate([s, tail])
    mic = (
        room(far, delay_ms=780, gain=0.05)
        + np.random.default_rng(1).standard_normal(far.size).astype(np.float32) * 3e-4
    )
    got = estimate(far, mic)
    assert got is not None and got.confident and got.delay_ms is not None and abs(got.delay_ms - 780) <= 2
