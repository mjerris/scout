"""End-of-turn models (Silero VAD, smart-turn) and the EndOfTurnSegmenter.

Model tests look for the files in $CLAUDE_VOICE_MODELS, then the repo's models/
(scripts/fetch-models.sh), and skip when they are absent. Speech comes from the
Kokoro TTS model, so those tests also need kokoro-v1.0.onnx + voices-v1.0.bin.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from claude_voice.audio_io import resample, to_int16_bytes
from claude_voice.turn_models import (
    FRAME_MS,
    TURN_SAMPLES,
    EndOfTurnSegmenter,
    SileroVAD,
    SmartTurn,
    turn_features,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data" / "turn"
FRAME = 480


def _model(name: str) -> Path | None:
    dirs = [Path(d) for d in [os.environ.get("CLAUDE_VOICE_MODELS", "")] if d] + [ROOT / "models"]
    for d in dirs:
        if (d / name).is_file():
            return d / name
    return None


def _need(*names: str) -> list[Path]:
    paths = [_model(n) for n in names]
    missing = [n for n, p in zip(names, paths, strict=True) if p is None]
    if missing:
        pytest.skip(f"model files not present: {', '.join(missing)} (run scripts/fetch-models.sh)")
    return [p for p in paths if p is not None]


@pytest.fixture(scope="module")
def silero_path() -> Path:
    return _need("silero_vad.onnx")[0]


@pytest.fixture(scope="module")
def smart_turn() -> SmartTurn:
    return SmartTurn(_need("smart-turn-v3.2-cpu.onnx")[0])


@pytest.fixture(scope="module")
def tts() -> Callable[[str, str], np.ndarray]:
    """text, voice -> 16 kHz float32 speech."""
    model, voices = _need("kokoro-v1.0.onnx", "voices-v1.0.bin")
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(model), str(voices))
    cache: dict[tuple[str, str], np.ndarray] = {}

    def speak(text: str, voice: str = "af_heart") -> np.ndarray:
        if (text, voice) not in cache:
            samples, sr = kokoro.create(text, voice=voice, speed=1.0)
            cache[text, voice] = resample(np.asarray(samples, np.float32), sr, 16000)
        return cache[text, voice]

    return speak


def _frames(audio: np.ndarray) -> list[bytes]:
    pcm = to_int16_bytes(audio)
    return [pcm[i : i + 2 * FRAME] for i in range(0, len(pcm) - 2 * FRAME + 1, 2 * FRAME)]


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * 16000), np.float32)


# --- smart-turn preprocessing ---------------------------------------------------


def synthetic(seconds: float) -> np.ndarray:
    """A voice-like test signal; tests/data/turn/whisper_features.npz holds
    transformers' WhisperFeatureExtractor output for it (made in a separate venv
    with the exact calls of pipecat-ai/smart-turn inference.py)."""
    t = np.arange(int(seconds * 16000)) / 16000.0
    f = 150.0 + 60.0 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f) / 16000.0
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t) ** 2
    x = env * (0.3 * np.sin(phase) + 0.1 * np.sin(3 * phase) + 0.05 * np.sin(7.3 * phase))
    return x.astype(np.float32)


@pytest.mark.parametrize(("key", "seconds"), [("short", 2.5), ("long", 9.0)])
def test_turn_features_match_reference(key: str, seconds: float) -> None:
    ref = np.load(DATA / "whisper_features.npz")[key]
    got = turn_features(synthetic(seconds))
    assert got.shape == (80, 800)
    assert got.dtype == np.float32
    np.testing.assert_allclose(got, ref, atol=1e-4, rtol=0)


def test_turn_features_keep_the_last_8s_and_pad_at_the_start() -> None:
    x = synthetic(9.0)
    np.testing.assert_array_equal(turn_features(x), turn_features(x[-TURN_SAMPLES:]))
    short = turn_features(synthetic(2.0))
    # Leading zero padding is flat; the signal is at the end.
    assert np.ptp(short[:, :500]) == 0
    assert np.ptp(short[:, -150:]) > 0.5


# --- Silero VAD ---------------------------------------------------------------


def _probs(vad: SileroVAD, audio: np.ndarray) -> np.ndarray:
    vad.reset()
    return np.array([vad.speech_prob(f) for f in _frames(audio)])


def test_silero_low_on_silence_and_noise(silero_path: Path) -> None:
    vad = SileroVAD(silero_path)
    rng = np.random.default_rng(0)
    t = np.arange(32000) / 16000
    clips = {
        "silence": _silence(2.0),
        "white": (0.1 * rng.standard_normal(32000)).astype(np.float32),
        "brown": (0.002 * np.cumsum(rng.standard_normal(32000))).astype(np.float32),
        "hum": (0.2 * np.sin(2 * np.pi * 120 * t)).astype(np.float32),
    }
    for name, clip in clips.items():
        p = _probs(vad, clip)
        assert p.max() < 0.3, (name, p.max())


def test_silero_high_on_speech(silero_path: Path, tts: Callable[[str, str], np.ndarray]) -> None:
    vad = SileroVAD(silero_path)
    speech = tts("Turn off the lights in the kitchen and set a timer for ten minutes.", "af_heart")
    p = _probs(vad, speech)
    assert (p > 0.5).mean() > 0.6
    assert p.max() > 0.95


def test_silero_buffers_480_sample_frames_into_512_sample_windows(
    silero_path: Path, tts: Callable[[str, str], np.ndarray]
) -> None:
    """Fed 30 ms frames, the wrapper must give the same probabilities as the model
    run directly on consecutive 512-sample windows with 64 samples of context."""
    speech = np.concatenate((_silence(0.3), tts("Hello there.", "am_michael"), _silence(0.3)))
    vad = SileroVAD(silero_path)
    got = _probs(vad, speech)
    x = np.frombuffer(to_int16_bytes(speech), np.int16).astype(np.float32) / 32768.0
    session = vad._session
    state = np.zeros((2, 1, 128), np.float32)
    context = np.zeros(64, np.float32)
    window_probs = []
    for i in range(0, len(_frames(speech)) * FRAME - 511, 512):
        w = x[i : i + 512]
        out, state = session.run(
            None, {"input": np.concatenate((context, w))[None], "state": state, "sr": np.array(16000)}
        )
        context = w[-64:]
        window_probs.append(float(out[0, 0]))
    # After frame k (480*(k+1) samples) the newest finished window is number
    # (480*(k+1)) // 512 - 1.
    for k, p in enumerate(got):
        n = 480 * (k + 1) // 512
        assert p == (window_probs[n - 1] if n else 0.0)
    # reset() starts over: the same input gives the same sequence.
    np.testing.assert_array_equal(_probs(vad, speech), got)


# --- smart-turn on speech ---------------------------------------------------------

PAIRS = [
    ("What's the weather going to be like tomorrow?", "What's the weather going to be like, um"),
    ("Turn off the lights in the kitchen.", "Turn off the lights in the, uh"),
    ("Send an email to John saying I'll be late.", "Send an email to John saying, um"),
    ("Can you set a timer for ten minutes?", "Can you set a timer for, um"),
]


@pytest.mark.parametrize("voice", ["af_heart", "am_michael"])
@pytest.mark.parametrize(("complete", "incomplete"), PAIRS)
def test_smart_turn_complete_vs_incomplete(
    smart_turn: SmartTurn,
    tts: Callable[[str, str], np.ndarray],
    voice: str,
    complete: str,
    incomplete: str,
) -> None:
    pause = _silence(0.25)  # what the segmenter has heard when it first asks
    p_done = smart_turn.complete_prob(np.concatenate((tts(complete, voice), pause)))
    p_cont = smart_turn.complete_prob(np.concatenate((tts(incomplete, voice), pause)))
    print(f"{voice} complete={p_done:.3f} incomplete={p_cont:.3f}: {complete!r} / {incomplete!r}")
    assert p_done >= 0.5 > p_cont
    assert p_done - p_cont > 0.5


# --- EndOfTurnSegmenter timing, with fake detectors ----------------------------------

SPEECH = (np.full(FRAME, 1000, np.int16)).tobytes()
QUIET = bytes(2 * FRAME)


class FakeVAD:
    """Speech probability from the frame itself: its first sample / 1000."""

    def __init__(self) -> None:
        self.resets = 0

    def speech_prob(self, frame: bytes) -> float:
        return min(1.0, abs(int(np.frombuffer(frame, np.int16)[0])) / 1000)

    def reset(self) -> None:
        self.resets += 1


class FakeTurn:
    """Answers from a script (the last answer repeats) and records each call."""

    def __init__(self, *answers: float) -> None:
        self.answers = list(answers)
        self.calls: list[int] = []  # audio length (samples) per call

    def complete_prob(self, audio: np.ndarray) -> float:
        self.calls.append(audio.size)
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def _frame(prob: float) -> bytes:
    return np.full(FRAME, round(prob * 1000), np.int16).tobytes()


def _run(seg: EndOfTurnSegmenter, frames: list[bytes]) -> list[tuple[int, bytes | bool]]:
    """Feed frames; return (frame index, result) for every non-None result."""
    out = []
    for i, f in enumerate(frames):
        r = seg.feed(f)
        if r is not None:
            out.append((i, r))
    return out


def test_onset_needs_most_of_300ms_and_keeps_it_as_preroll() -> None:
    seg = EndOfTurnSegmenter(FakeVAD(), FakeTurn(1.0))
    frames = [QUIET] * 5 + [SPEECH] * 6 + [QUIET] * 4  # never 7 of 10
    assert _run(seg, frames) == []
    assert not seg.triggered
    seg.reset()
    frames = [QUIET] * 5 + [SPEECH] * 7
    assert _run(seg, frames) == [(11, True)]
    assert seg.triggered
    assert seg.buf == [QUIET] * 3 + [SPEECH] * 7  # the 300 ms ring is the pre-roll


def test_ends_at_min_pause_when_turn_complete() -> None:
    turn = FakeTurn(0.9)
    seg = EndOfTurnSegmenter(FakeVAD(), turn)
    frames = [SPEECH] * 20 + [QUIET] * 60
    res = _run(seg, frames)
    # onset at frame 6 (7 of 10); 270 ms (9 frames, >= 250 ms) of silence, then the turn ends.
    assert [i for i, _ in res] == [6, 28]
    pcm = res[1][1]
    assert isinstance(pcm, bytes)
    assert len(pcm) == 29 * 2 * FRAME
    assert len(turn.calls) == 1
    assert seg.last_end_reason == "turn"
    assert seg.last_turn_prob == 0.9
    assert seg.last_voiced_ms == 20 * FRAME_MS
    assert not seg.triggered


def test_waits_to_max_pause_when_incomplete_rechecking_every_200ms() -> None:
    turn = FakeTurn(0.1)
    seg = EndOfTurnSegmenter(FakeVAD(), turn)
    frames = [SPEECH] * 20 + [QUIET] * 80
    res = _run(seg, frames)
    # 1500 ms = 50 silent frames after the last speech frame (index 19).
    assert [i for i, _ in res] == [6, 69]
    assert seg.last_end_reason == "pause"
    # Asked at 9, 16, 23, 30, 37, 44 silent frames (250 ms, then every 210 ms).
    assert len(turn.calls) == 6
    assert seg.last_turn_prob == 0.1


def test_incomplete_then_complete_ends_at_the_recheck() -> None:
    turn = FakeTurn(0.2, 0.3, 0.8)
    seg = EndOfTurnSegmenter(FakeVAD(), turn)
    res = _run(seg, [SPEECH] * 20 + [QUIET] * 80)
    assert [i for i, _ in res] == [6, 19 + 23]
    assert len(turn.calls) == 3
    assert seg.last_end_reason == "turn"


def test_speech_after_a_pause_restarts_the_pause_clock() -> None:
    turn = FakeTurn(0.1, 0.9)
    seg = EndOfTurnSegmenter(FakeVAD(), turn)
    frames = [SPEECH] * 20 + [QUIET] * 12 + [SPEECH] * 10 + [QUIET] * 30
    res = _run(seg, frames)
    # First pause: asked once at 9 frames (no); speech resumes; second pause is
    # asked again at 9 frames (yes): last speech frame is 41.
    assert [i for i, _ in res] == [6, 41 + 9]
    assert len(turn.calls) == 2


def test_hysteresis_between_thresholds() -> None:
    turn = FakeTurn(0.9)
    seg = EndOfTurnSegmenter(FakeVAD(), turn)
    # 0.4 lies between the off (0.35) and on (0.5) thresholds: after speech it
    # still counts as speech, so the pause only starts at the true silence.
    frames = [SPEECH] * 20 + [_frame(0.4)] * 5 + [QUIET] * 20
    res = _run(seg, frames)
    assert [i for i, _ in res] == [6, 24 + 9]
    # ...and inside a pause it counts as silence.
    seg = EndOfTurnSegmenter(FakeVAD(), FakeTurn(0.9))
    frames = [SPEECH] * 20 + [QUIET] * 3 + [_frame(0.4)] * 20
    assert [i for i, _ in _run(seg, frames)] == [6, 19 + 9]


def test_max_utterance_length() -> None:
    turn = FakeTurn(0.0)
    seg = EndOfTurnSegmenter(FakeVAD(), turn, max_utterance_s=1.5)
    res = _run(seg, [SPEECH] * 100)[:2]
    # onset at frame 6 with 7 frames buffered; 50 frames in all at frame 49.
    assert [i for i, _ in res] == [6, 49]
    assert isinstance(res[1][1], bytes)
    assert len(res[1][1]) == 50 * 2 * FRAME
    assert seg.last_end_reason == "max"
    assert turn.calls == []


def test_too_little_speech_ends_but_returns_none() -> None:
    seg = EndOfTurnSegmenter(FakeVAD(), FakeTurn(0.9), min_speech_ms=300)
    res = _run(seg, [SPEECH] * 8 + [QUIET] * 20)
    assert res == [(6, True)]  # the end (frame 16) returns None
    assert not seg.triggered
    assert seg.last_voiced_ms == 8 * FRAME_MS
    assert seg.last_end_reason == "turn"


def test_level_and_turn_audio_window() -> None:
    turn = FakeTurn(0.9)
    vad = FakeVAD()
    seg = EndOfTurnSegmenter(vad, turn)
    loud = np.full(FRAME, 3000, np.int16).tobytes()
    _run(seg, [loud] * 400 + [QUIET] * 10)  # 12 s of speech
    assert seg.last_level_db == pytest.approx(20 * np.log10(3000 / 32768), abs=1e-4)
    assert turn.calls == [TURN_SAMPLES]  # only the last 8 s go to the turn detector
    assert vad.resets >= 2  # at construction and after the utterance


def test_thresholds_are_configurable() -> None:
    turn = FakeTurn(0.6)
    seg = EndOfTurnSegmenter(
        FakeVAD(), turn, min_pause_ms=500, max_pause_ms=900, recheck_ms=100, turn_threshold=0.7
    )
    res = _run(seg, [SPEECH] * 20 + [QUIET] * 60)
    assert [i for i, _ in res] == [6, 19 + 30]  # 900 ms
    assert len(turn.calls) == 4  # at 17, 21, 25, 29 silent frames
    assert seg.last_end_reason == "pause"


# --- the real models together ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [("Turn off the lights in the kitchen.", "turn"), ("Turn off the lights in the, uh", "pause")],
)
def test_segmenter_with_real_models(
    silero_path: Path,
    smart_turn: SmartTurn,
    tts: Callable[[str, str], np.ndarray],
    text: str,
    reason: str,
) -> None:
    seg = EndOfTurnSegmenter(SileroVAD(silero_path), smart_turn)
    speech = tts(text, "am_michael")
    audio = np.concatenate((_silence(0.5), speech, _silence(2.5)))
    res = _run(seg, _frames(audio))
    assert [type(r) for _, r in res] == [bool, bytes]
    assert seg.last_end_reason == reason
    # Measured from the last loud sample (Kokoro pads its output with silence).
    voice_end = 0.5 + float(np.flatnonzero(np.abs(speech) > 0.02)[-1]) / 16000
    end_ms = (res[1][0] + 1) * FRAME_MS - int(voice_end * 1000)
    print(f"{text!r}: ended {end_ms} ms after the voice, p={seg.last_turn_prob:.3f}")
    if reason == "turn":
        assert end_ms < 600
    else:
        assert end_ms > 1400
