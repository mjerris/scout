"""Echo-path calibration per output and input device pair (webrtc backend).

AEC3 finds an echo only up to about 500 ms behind the reference it is given (measured
offline: tests/test_audio_backends.py), so a longer path (a TV over HDMI) needs the
reference held back first (aec.EchoCanceller). How long, and whether at all, is
measured rather than assumed:

- Passively: each stretch of Scout's own playback (its TTS, exactly what went to the
  speaker) is cross-correlated with the raw microphone signal of the same time
  (GCC-PHAT, at 8 kHz). A stretch counts when its peak stands well clear of the rest;
  three that agree within CONSISTENT_MS make a decision.
- Actively, as a fallback: a short quiet sweep, played by the app only when the room
  is quiet (config [audio] calibrate_chirp; __init__._echo_check), measured the same way.

Results are kept per device pair in state/audio_devices.json and used at startup. The
passive check goes on all the time: a delay that drifts more than DRIFT_MS, or echo that
stays high after the canceller has had time to adapt, measures the pair again. A pair
with no meaningful echo path (headphones) is recorded as such. Each decision is logged
in one line. `python -m scout.echo_cal show|reset` shows or clears what's stored.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .aec import plan, resample_offline
from .config import DATA

log = logging.getLogger(__name__)

PATH = DATA / "state" / "audio_devices.json"

RATE = 8000  # estimation rate: 0.125 ms resolution is plenty
MAX_LAG_MS = 1500  # the longest echo path looked for
MIN_ACTIVE_S = 0.8  # playback (above -50 dBFS) a stretch needs to be measured
MIN_PSR = 4.0  # peak over the next-best lag (outside +-3 ms): a clear peak
NO_ECHO_RHO = 0.1  # correlation below this, and no clear peak (NO_ECHO_PSR), with clear
NO_ECHO_PSR = 2.0  # playback: no echo path (headphones). Changes no cancelling, only the record
NEEDED = 3  # agreeing measurements for a decision
CONSISTENT_MS = 20.0  # ...within this of their median
DRIFT_MS = 50.0  # a stored delay this far off what's measured now is measured again
RESIDUAL_LOW_DB = 6.0  # echo removal still under this...
RESIDUAL_AFTER_S = 20.0  # ...after this much playback on the pair: measure it again


@dataclass
class Estimate:
    """One stretch of playback against the microphone."""

    delay_ms: float | None  # None: no echo found (see echo)
    psr: float  # peak-to-sidelobe ratio of the cross-correlation
    rho: float  # normalised correlation at the peak
    echo_db: float  # echo level relative to the reference, dB
    echo: bool  # is there an echo path at all

    @property
    def confident(self) -> bool:
        return self.delay_ms is not None and self.psr >= MIN_PSR


def _active_seconds(ref: np.ndarray) -> float:
    n = RATE // 100
    blocks = ref[: ref.size // n * n].reshape(-1, n)
    level = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1))
    return float(np.count_nonzero(level > 10 ** (-50 / 20))) / 100


def estimate(ref: np.ndarray, mic: np.ndarray, rate: int = 48000) -> Estimate | None:
    """The echo delay of `mic` behind `ref` (float in [-1, 1] or int16, same length and
    rate), or None when the stretch holds too little playback to tell."""
    if ref.dtype == np.int16:
        ref = ref / 32768.0
    if mic.dtype == np.int16:
        mic = mic / 32768.0
    r = resample_offline(np.asarray(ref, np.float32), rate, RATE).astype(np.float64)
    m = resample_offline(np.asarray(mic, np.float32), rate, RATE).astype(np.float64)
    n = min(r.size, m.size)
    r, m = r[:n], m[:n]
    if _active_seconds(r) < MIN_ACTIVE_S:
        return None
    max_lag = min(n - 1, RATE * MAX_LAG_MS // 1000)
    size = 1 << math.ceil(math.log2(2 * n))
    cross = np.fft.rfft(m, size) * np.conj(np.fft.rfft(r, size))
    phat = np.fft.irfft(cross / (np.abs(cross) + 1e-12), size)[: max_lag + 1]
    peak = int(np.argmax(phat))
    guard = RATE * 3 // 1000
    rest = np.concatenate([phat[: max(0, peak - guard)], phat[peak + guard + 1 :]])
    side = float(rest.max()) if rest.size else 0.0
    psr = float(phat[peak]) / side if side > 0 else float("inf")
    a, b = m[peak:], r[: n - peak]
    rho = float(np.dot(a, b) / (math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b))) + 1e-12))
    gain = float(np.dot(a, b) / (float(np.dot(b, b)) + 1e-12))
    echo_db = 20 * math.log10(abs(gain) + 1e-9)
    if abs(rho) < NO_ECHO_RHO and psr < NO_ECHO_PSR:
        return Estimate(None, psr, rho, echo_db, False)
    return Estimate(1000 * peak / RATE, psr, rho, echo_db, True)


def pair_key(input_name: str, output_name: str) -> str:
    return f"{input_name} -> {output_name}"


class Calibrator:
    """The stored measurements, and the passive check for the device pair in use."""

    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.pairs: dict[str, dict[str, Any]] = self._load()
        self.key = ""
        self.default_ms = 0.0
        self._recent: deque[Estimate] = deque(maxlen=2 * NEEDED)
        self._quiet: deque[Estimate] = deque(maxlen=NEEDED)  # clear playback, no echo
        self.wanted = False  # a measurement is due (new pair, drift, echo left over)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.pairs, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def select(self, input_name: str, output_name: str, default_ms: float) -> float:
        """The pair now in use. Returns the echo delay to plan for: the stored one, else
        `default_ms` (stream latency and [audio] output_delay_ms) until it's measured."""
        key = pair_key(input_name, output_name)
        with self._lock:
            if key != self.key:
                self._recent.clear()
                self._quiet.clear()
            self.key, self.default_ms = key, default_ms
            stored = self.pairs.get(key)
            self.wanted = stored is None
        if stored is None:
            log.info("echo path %s: not measured yet; planning for %.0f ms until it is", key, default_ms)
            return default_ms
        log.info(
            "echo path %s: %s, measured %s",
            key,
            _describe(stored),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(stored.get("measured_at", 0))),
        )
        return float(stored.get("delay_ms") or default_ms)

    def add(self, est: Estimate | None) -> float | None:
        """One passive (or chirp) measurement. Returns a new delay to plan for when this
        settles one (a first measurement, or a drift past DRIFT_MS), else None."""
        if est is None or not self.key:
            return None
        with self._lock:
            if not est.echo:
                self._quiet.append(est)
                self._recent.clear()
                if len(self._quiet) >= NEEDED and self.pairs.get(self.key, {}).get("decision") != "none":
                    self._store(None, est, "none")
                    self._quiet.clear()
                return None
            self._quiet.clear()
            if not est.confident:
                return None
            self._recent.append(est)
            delays = sorted(e.delay_ms for e in self._recent if e.delay_ms is not None)
            med = delays[len(delays) // 2]
            agree = [
                e for e in self._recent if e.delay_ms is not None and abs(e.delay_ms - med) <= CONSISTENT_MS
            ]
            if len(agree) < NEEDED:
                return None
            stored = self.pairs.get(self.key)
            old = None if stored is None else stored.get("delay_ms")
            if (
                stored is not None
                and old is not None
                and abs(float(old) - med) <= DRIFT_MS
                and not self.wanted
            ):
                stored["checked_at"] = round(time.time())  # still right
                self._recent.clear()
                return None
            best = max(agree, key=lambda e: e.psr)
            self._store(med, best, "delay_line" if plan(med)[0] else "aec3", agree=len(agree), was=old)
            self._recent.clear()
            return med

    def _store(
        self, delay: float | None, est: Estimate, decision: str, agree: int = NEEDED, was: Any = None
    ) -> None:
        ref, hint = plan(delay if delay is not None else self.default_ms)
        entry = {
            "pair": self.key,
            "delay_ms": None if delay is None else round(delay, 1),
            "confidence": round(min(est.psr, 99.0), 1),
            "echo_db": round(est.echo_db, 1),
            "decision": decision,
            "ref_delay_ms": ref if decision != "none" else 0,
            "hint_ms": hint,
            "measurements": agree,
            "measured_at": round(time.time()),
        }
        self.pairs[self.key] = entry
        self.wanted = False
        why = "first measurement" if was is None else f"drifted from {was} ms"
        log.info("echo path %s: %s (%s)", self.key, _describe(entry), why)
        try:
            self._save()
        except OSError as exc:
            log.warning("echo path: can't save %s: %s", self.path, exc)

    def residual(self, erle_db: float | None, learned_s: float) -> None:
        """Echo removal after a fair amount of playback on this pair: still low means the
        stored delay is wrong (or the room changed). Measure again."""
        if erle_db is None or self.wanted or learned_s < RESIDUAL_AFTER_S or erle_db >= RESIDUAL_LOW_DB:
            return
        stored = self.pairs.get(self.key)
        if stored is not None and stored.get("decision") != "none":
            log.info(
                "echo path %s: only %.1f dB removed after %.0f s; measuring it again",
                self.key,
                erle_db,
                learned_s,
            )
            self.wanted = True
            self._recent.clear()

    def status(self) -> dict[str, Any]:
        stored = self.pairs.get(self.key)
        return {"pair": self.key, "measuring": self.wanted, "stored": dict(stored) if stored else None}

    def reset(self) -> None:
        with self._lock:
            self.pairs.clear()
            self.wanted = bool(self.key)
            self.path.unlink(missing_ok=True)


def _describe(entry: dict[str, Any]) -> str:
    if entry.get("decision") == "none":
        return f"no echo path (echo {entry.get('echo_db')} dB)"
    held = entry.get("ref_delay_ms") or 0
    how = f"reference held back {held} ms, AEC3 finds the rest" if held else "AEC3 alone"
    return (
        f"{entry.get('delay_ms')} ms, echo {entry.get('echo_db')} dB, "
        f"confidence {entry.get('confidence')}: {how}"
    )


def sweep(rate: int = 24000, seconds: float = 1.0, level_db: float = -26.0) -> np.ndarray:
    """The active fallback: a quiet log sweep, 200 Hz to 4 kHz, faded in and out."""
    t = np.arange(int(rate * seconds)) / rate
    f0, f1 = 200.0, 4000.0
    k = math.log(f1 / f0)
    phase = 2 * math.pi * f0 * seconds / k * (np.exp(t / seconds * k) - 1)
    fade = np.minimum(1.0, np.minimum(t, seconds - t) / 0.05)
    return np.asarray(10 ** (level_db / 20) * fade * np.sin(phase), np.float32)


def main(argv: list[str], path: Path | None = None) -> int:
    cal = Calibrator(path or PATH)
    cmd = argv[0] if argv else "show"
    if cmd == "show":
        if not cal.pairs:
            print("no echo paths measured yet")
        for key, entry in sorted(cal.pairs.items()):
            print(f"{key}: {_describe(entry)}")
        return 0
    if cmd == "reset":
        cal.reset()
        print("cleared; restart Scout (scoutctl.sh restart) to measure again")
        return 0
    print("usage: python -m scout.echo_cal show|reset", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
