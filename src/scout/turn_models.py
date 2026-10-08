"""The end-of-turn models behind turn.py's interfaces, and the segmenter that uses them.

- `SileroVAD`: Silero VAD v5 (ONNX, MIT), frame-level speech probability.
- `SmartTurn`: Pipecat smart-turn v3.2 (ONNX, BSD-2), "has the speaker finished?".
- `EndOfTurnSegmenter`: drop-in replacement for audio.Segmenter that starts an
  utterance on Silero speech and ends it as soon as smart-turn says the turn is
  complete after a short pause, or after a longer pause regardless.

Model files come from scripts/fetch-models.sh (pinned URLs and sha256 sums).
"""

from __future__ import annotations

import collections
import math
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .turn import SpeechDetector, TurnDetector

SAMPLE_RATE = 16000
FRAME_MS = 30

SILERO_MODEL = "models/silero_vad.onnx"
SMART_TURN_MODEL = "models/smart-turn-v3.2-cpu.onnx"


class SileroVAD:
    """Silero VAD v5 on 30 ms int16 frames.

    The model takes 512-sample windows (32 ms at 16 kHz) preceded by the last
    64 samples of the previous window, and carries a recurrent state between
    calls. Frames are 480 samples, so samples are buffered and each call runs
    every whole window available; `speech_prob` returns the newest probability
    (unchanged if this frame did not complete a window).
    """

    WINDOW = 512
    CONTEXT = 64

    def __init__(self, model_path: str | Path = SILERO_MODEL) -> None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros(self.CONTEXT, np.float32)
        self._pending = np.zeros(0, np.float32)
        self._prob = 0.0

    def speech_prob(self, frame: bytes) -> float:
        samples = np.frombuffer(frame, np.int16).astype(np.float32) / 32768.0
        self._pending = np.concatenate((self._pending, samples))
        while self._pending.size >= self.WINDOW:
            window, self._pending = self._pending[: self.WINDOW], self._pending[self.WINDOW :]
            x = np.concatenate((self._context, window))[None, :]
            out, self._state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
            self._context = window[-self.CONTEXT :]
            self._prob = float(out[0, 0])
        return self._prob


# Whisper log-mel front end, as transformers' WhisperFeatureExtractor computes it.
N_FFT = 400
HOP = 160
N_MELS = 80
TURN_SECONDS = 8
TURN_SAMPLES = TURN_SECONDS * SAMPLE_RATE


def _hz_to_mel(f: np.ndarray) -> np.ndarray:
    """Slaney mel scale: linear below 1 kHz, logarithmic above."""
    logstep = 27.0 / np.log(6.4)
    mel: np.ndarray = np.where(
        f < 1000.0, 3.0 * f / 200.0, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) * logstep
    )
    return mel


def _mel_to_hz(m: np.ndarray) -> np.ndarray:
    logstep = np.log(6.4) / 27.0
    hz: np.ndarray = np.where(m < 15.0, 200.0 * m / 3.0, 1000.0 * np.exp(logstep * (m - 15.0)))
    return hz


def _mel_filters() -> np.ndarray:
    """(N_FFT//2+1, N_MELS) triangular filters, Slaney scale and Slaney area norm
    (librosa's default; what Whisper and WhisperFeatureExtractor use)."""
    fft_freqs = np.linspace(0.0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    mels = np.linspace(_hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(SAMPLE_RATE / 2)), N_MELS + 2)
    edges = _mel_to_hz(mels)
    diff = np.diff(edges)
    slopes = edges[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    filters = np.maximum(0.0, np.minimum(down, up))
    filters *= (2.0 / (edges[2:] - edges[:-2]))[None, :]
    return filters


_MEL_FILTERS = _mel_filters()
_WINDOW = np.hanning(N_FFT + 1)[:-1]  # periodic Hann


def turn_features(audio: np.ndarray) -> np.ndarray:
    """Smart-turn's input: (80, 800) float32 log-mel features of the last 8 s.

    Matches the reference (pipecat-ai/smart-turn inference.py): keep the last
    8 s, zero-pad at the START to 8 s, then WhisperFeatureExtractor with
    do_normalize=True: zero-mean unit-variance over the whole padded 8 s, a
    centred (reflect-padded) 400-point periodic-Hann STFT with hop 160, power
    spectrum through 80 Slaney mel filters, log10 floored at 1e-10, last frame
    dropped, clamped to (max - 8), then (x + 4) / 4.
    """
    # The dtypes follow the reference step by step (float32 normalisation, a
    # float64 FFT stored as complex64, float64 mel, float32 log): the int8
    # model is sensitive enough that float64 throughout moves some outputs.
    x = np.asarray(audio, dtype=np.float32)[-TURN_SAMPLES:]
    if x.size < TURN_SAMPLES:
        x = np.concatenate((np.zeros(TURN_SAMPLES - x.size, np.float32), x))
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    padded = np.pad(x, N_FFT // 2, mode="reflect").astype(np.float64)
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP]
    spec = np.fft.rfft(frames * _WINDOW, axis=1).astype(np.complex64)
    power = np.abs(spec, dtype=np.float64) ** 2
    mel = np.maximum(1e-10, _MEL_FILTERS.T @ power.T)
    log_spec = np.log10(mel).astype(np.float32)[:, :-1]
    log_spec = np.maximum(log_spec, log_spec.max() - np.float32(8.0))
    feats: np.ndarray = (log_spec + np.float32(4.0)) / np.float32(4.0)
    return feats


class SmartTurn:
    """Pipecat smart-turn v3.2 (CPU, int8): probability that the turn is complete."""

    def __init__(self, model_path: str | Path = SMART_TURN_MODEL) -> None:
        opts = ort.SessionOptions()
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )

    def complete_prob(self, audio: np.ndarray) -> float:
        feats = turn_features(audio)[None, :, :]
        (out,) = self._session.run(None, {"input_features": feats})
        return float(out[0, 0])


def _frames(ms: int) -> int:
    return max(1, math.ceil(ms / FRAME_MS))


class EndOfTurnSegmenter:
    """Groups frames into utterances: Silero speech onset → end of turn.

    Same public API as audio.Segmenter: `feed(frame)` returns True at speech
    onset, the utterance bytes at its end (None if it held too little speech),
    else None; `triggered`, `last_voiced_ms` and `last_level_db` likewise.

    Onset: `onset_ratio` of the last `onset_ms` frames are speech; those frames
    become the utterance's pre-roll. A frame is speech at probability >=
    `speech_threshold`, silence below `speech_threshold - 0.15`, and in between
    it continues whichever it was (hysteresis, as Silero's own VADIterator).

    End: after `min_pause_ms` of silence the turn detector hears the utterance
    so far (its last 8 s); at probability >= `turn_threshold` the utterance ends
    there. Otherwise it is asked again every `recheck_ms` of continued silence,
    and the utterance ends at `max_pause_ms` of silence regardless, or at
    `max_utterance_s` in total.

    After an end, `last_end_reason` is "turn", "pause" or "max", and
    `last_turn_prob` holds the detector's latest answer (None if never asked).
    """

    def __init__(
        self,
        vad: SpeechDetector,
        turn: TurnDetector,
        *,
        min_speech_ms: int = 300,
        max_utterance_s: float = 30.0,
        min_pause_ms: int = 250,
        max_pause_ms: int = 1500,
        recheck_ms: int = 200,
        turn_threshold: float = 0.5,
        speech_threshold: float = 0.5,
        onset_ms: int = 300,
        onset_ratio: float = 0.7,
    ) -> None:
        self.vad, self.turn = vad, turn
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self.max_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self.min_pause_frames = _frames(min_pause_ms)
        self.max_pause_frames = max(_frames(max_pause_ms), self.min_pause_frames)
        self.recheck_frames = _frames(recheck_ms)
        self.turn_threshold = turn_threshold
        self.speech_on = speech_threshold
        self.speech_off = speech_threshold - 0.15
        self.ring_len = _frames(onset_ms)
        self.onset_frames = onset_ratio * self.ring_len
        self.turn_frames = _frames(TURN_SECONDS * 1000)
        self.ring: collections.deque[tuple[bytes, bool]] = collections.deque(maxlen=self.ring_len)
        self.last_voiced_ms = 0
        self.last_level_db = -120.0
        self.last_end_reason = ""
        self.last_turn_prob: float | None = None
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self.ring.clear()
        self.buf: list[bytes] = []
        self.triggered = False
        self.voiced = 0
        self.voiced_energy = 0.0  # summed mean-square power of voiced frames
        self.trailing = 0  # consecutive silent frames
        self.next_check = self.min_pause_frames
        self.turn_prob: float | None = None

    @staticmethod
    def _power(frame: bytes) -> float:
        s = np.frombuffer(frame, np.int16).astype(np.float32) / 32768.0
        return float(np.mean(s * s))

    def _is_speech(self, prob: float, was_speech: bool) -> bool:
        if prob >= self.speech_on:
            return True
        if prob < self.speech_off:
            return False
        return was_speech

    def _ask_turn(self) -> float:
        pcm = b"".join(self.buf[-self.turn_frames :])
        audio = np.frombuffer(pcm, np.int16)[-TURN_SAMPLES:].astype(np.float32) / 32768.0
        self.turn_prob = self.turn.complete_prob(audio)
        return self.turn_prob

    def feed(self, frame: bytes) -> bytes | bool | None:
        """Returns True at speech onset, the utterance bytes at its end, else None."""
        prob = self.vad.speech_prob(frame)
        if not self.triggered:
            was = self.ring[-1][1] if self.ring else False
            speech = self._is_speech(prob, was)
            self.ring.append((frame, speech))
            if sum(s for _, s in self.ring) >= self.onset_frames:
                self.triggered = True
                self.buf = [f for f, _ in self.ring]
                self.voiced = sum(s for _, s in self.ring)
                self.voiced_energy = sum(self._power(f) for f, s in self.ring if s)
                self.trailing = 0
                self.next_check = self.min_pause_frames
                self.ring.clear()
                return True
            return None
        self.buf.append(frame)
        speech = self._is_speech(prob, self.trailing == 0)
        if speech:
            self.voiced += 1
            self.voiced_energy += self._power(frame)
            self.trailing = 0
            self.next_check = self.min_pause_frames
        else:
            self.trailing += 1
        reason = ""
        if len(self.buf) >= self.max_frames:
            reason = "max"
        elif self.trailing >= self.max_pause_frames:
            reason = "pause"
        elif self.trailing >= self.next_check:
            if self._ask_turn() >= self.turn_threshold:
                reason = "turn"
            else:
                self.next_check = self.trailing + self.recheck_frames
        if not reason:
            return None
        pcm, voiced = b"".join(self.buf), self.voiced
        self.last_voiced_ms = voiced * FRAME_MS
        self.last_level_db = float(10 * np.log10(self.voiced_energy / max(voiced, 1) + 1e-12))
        self.last_end_reason, self.last_turn_prob = reason, self.turn_prob
        self.reset()
        return pcm if voiced >= self.min_speech_frames else None
