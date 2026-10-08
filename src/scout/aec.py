"""DSP for the WebRTC backend, kept apart from the audio I/O so it can be tested offline.

- `EchoCanceller` wraps livekit's WebRTC AudioProcessingModule (AEC3, noise
  suppression, high-pass filter, optional gain control) for 10 ms int16 mono
  frames, and keeps a rough running estimate of how much echo it removes.
- `Resampler` is a streaming polyphase FIR resampler (Kaiser-windowed sinc)
  for any rational rate change: 48k -> 16k for the microphone path (with a
  proper anti-aliasing filter, unlike linear interpolation), and 24k (or any
  rate) -> 48k for playback. `resample_offline` resamples a whole clip with
  the filter delay removed.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# Echo energy below this (mean square of int16 samples, ~ -60 dBFS) is not
# counted towards the echo-removal estimate: the reference is effectively silent.
_REF_ACTIVE_POWER = (32768.0 * 10 ** (-60 / 20)) ** 2


MAX_DELAY_HINT_MS = 500  # set_stream_delay_ms fails above this


class EchoCanceller:
    """WebRTC audio processing on 10 ms mono int16 frames.

    `process(mic, ref)` feeds `ref` (exactly what was sent to the speaker for
    this block) as the reverse stream first, then cleans `mic` (the block
    captured at the same time). The returned array is an internal buffer that
    the next call overwrites; copy it if it must be kept.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        *,
        echo_cancellation: bool = True,
        noise_suppression: bool = True,
        high_pass_filter: bool = True,
        auto_gain_control: bool = False,
    ) -> None:
        from livekit import rtc

        if sample_rate % 100:
            raise ValueError("sample rate must be a multiple of 100 Hz (10 ms frames)")
        self.sample_rate = sample_rate
        self.block = sample_rate // 100
        self.echo_cancellation = echo_cancellation
        self._apm = rtc.AudioProcessingModule(
            echo_cancellation=echo_cancellation,
            noise_suppression=noise_suppression,
            high_pass_filter=high_pass_filter,
            auto_gain_control=auto_gain_control,
        )
        # The APM processes these buffers in place; the numpy views share their memory.
        self._mic_buf = bytearray(self.block * 2)
        self._ref_buf = bytearray(self.block * 2)
        self._mic = np.frombuffer(self._mic_buf, np.int16)
        self._ref = np.frombuffer(self._ref_buf, np.int16)
        self._mic_frame = rtc.AudioFrame(self._mic_buf, sample_rate, 1, self.block)
        self._ref_frame = rtc.AudioFrame(self._ref_buf, sample_rate, 1, self.block)
        self._scratch = np.zeros(self.block, np.float32)
        self.delay_ms = 0
        self.frames = 0
        # Exponential averages of echo (mic before processing) and residual
        # (after) power, over blocks where the reference is playing.
        self._echo_pow = 0.0
        self._res_pow = 0.0
        self._erle_blocks = 0

    def set_delay_ms(self, delay_ms: float) -> None:
        """Delay between a reference sample being processed and its echo being captured.
        Only a hint: AEC3 finds the real delay itself (it found a TV's ~750 ms from an
        80 ms hint). The processor rejects hints over 500 ms ("Failed to set stream
        delay" on every block, so no mic audio at all), so the hint is capped there."""
        self.delay_ms = min(MAX_DELAY_HINT_MS, max(0, round(delay_ms)))

    def _power(self, x: np.ndarray) -> float:
        np.multiply(x, x, out=self._scratch, dtype=np.float32)
        return float(self._scratch.mean())

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """Clean one 10 ms block. `mic` and `ref` are int16 (or float in [-1, 1])."""
        if mic.dtype != np.int16:
            mic = np.clip(mic * 32767.0, -32768, 32767)
        if ref.dtype != np.int16:
            ref = np.clip(ref * 32767.0, -32768, 32767)
        self._ref[:] = ref
        self._mic[:] = mic
        ref_pow = self._power(self._ref)
        echo_pow = self._power(self._mic) if ref_pow > _REF_ACTIVE_POWER else 0.0
        self._apm.process_reverse_stream(self._ref_frame)
        if self.echo_cancellation:
            self._apm.set_stream_delay_ms(self.delay_ms)
        self._apm.process_stream(self._mic_frame)
        self.frames += 1
        if echo_pow:
            res_pow = self._power(self._mic)
            a = 0.02 if self._erle_blocks > 50 else 1.0 / (self._erle_blocks + 1)
            self._echo_pow += (echo_pow - self._echo_pow) * a
            self._res_pow += (res_pow - self._res_pow) * a
            self._erle_blocks += 1
        return self._mic

    def close(self) -> None:
        """Release the native processor now rather than at garbage collection
        (which can run after livekit's FFI has shut down at interpreter exit)."""
        self._apm._ffi_handle.dispose()

    @property
    def learned_seconds(self) -> float:
        """How much playback the canceller has adapted on (its echo-path experience)."""
        return self._erle_blocks * self.block / self.sample_rate

    @property
    def erle_db(self) -> float | None:
        """Rough echo return loss enhancement (dB) while playing; None before any playback.
        Includes noise suppression and is pulled down by double talk."""
        if not self._erle_blocks:
            return None
        return 10 * math.log10((self._echo_pow + 1e-3) / (self._res_pow + 1e-3))


class Resampler:
    """Streaming polyphase FIR resampler, src_rate -> dst_rate, float32 mono.

    Feed any number of samples per call; state carries across calls. The
    output lags the input by `delay` output samples (the filter's group delay).
    """

    def __init__(
        self, src_rate: int, dst_rate: int, *, half_taps: int = 24, beta: float = 8.0, rolloff: float = 0.92
    ) -> None:
        g = math.gcd(src_rate, dst_rate)
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self.up, self.down = dst_rate // g, src_rate // g
        up, down = self.up, self.down
        r = max(up, down)
        ntaps = 2 * half_taps * r + 1
        fc = 0.5 / r * rolloff  # cutoff in cycles per sample at the upsampled rate
        n = np.arange(ntaps) - (ntaps - 1) / 2
        h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(ntaps, beta)
        h *= up / h.sum()  # unity gain after zero-stuffing by `up`
        k = -(-ntaps // up)
        h = np.concatenate([h, np.zeros(k * up - ntaps)])
        # Polyphase bank, taps reversed so a window of input dots straight into it.
        self._bank = h.reshape(k, up).T[:, ::-1].astype(np.float32).copy()
        self._k = k
        self._hist = np.zeros(k - 1, np.float32)
        # Group delay in upsampled samples. Starting the output grid at its
        # fractional part makes the delay a whole number of output samples.
        d = (ntaps - 1) // 2
        self._u = (k - 1) * up + d % down  # upsampled index of the next output, in buffer coordinates
        self.delay = d // down

    def process(self, x: np.ndarray) -> np.ndarray:
        up, down, k = self.up, self.down, self._k
        buf = np.concatenate([self._hist, x.astype(np.float32, copy=False)])
        u = self._u
        count = max(0, (len(buf) * up - 1 - u) // down + 1)
        windows = sliding_window_view(buf, k)
        if up == 1:
            first = u - (k - 1)
            out: np.ndarray = windows[first : first + down * count : down] @ self._bank[0]
        else:
            us = u + down * np.arange(count)
            out = np.einsum("nk,nk->n", windows[us // up - (k - 1)], self._bank[us % up])
        self._u = u + down * count - (len(buf) - (k - 1)) * up
        self._hist = buf[len(buf) - (k - 1) :].copy()
        return out.astype(np.float32, copy=False)


def resample_offline(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample a whole clip, delay-compensated: output length is round(n * dst / src)."""
    x = samples.astype(np.float32, copy=False)
    if src_rate == dst_rate or x.size == 0:
        return x
    r = Resampler(src_rate, dst_rate)
    n_out = round(x.size * dst_rate / src_rate)
    start = r.delay
    pad = math.ceil((start + 2) * src_rate / dst_rate) + r._k
    step = max(1, src_rate)  # about a second of input per call keeps memory bounded
    parts = [r.process(x[i : i + step]) for i in range(0, x.size, step)]
    parts.append(r.process(np.zeros(pad, np.float32)))
    y = np.concatenate(parts)
    return y[start : start + n_out]
