"""Local, deterministic checks that decide whether a transcript is worth sending
to Claude. Everything here is plain code: rejected utterances cost no tokens.

Each check returns None to accept, or a short reason string to reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import GateConfig

# Things Whisper produces from noise, silence or music rather than speech.
HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "thank you so much for watching", "please subscribe", "subscribe", "like and subscribe",
    "bye", "bye bye", "so", "uh", "um", "hmm", "mmm", "mm", "oh", "ah", "huh",
    "subtitles by the amaraorg community", "transcribed by", "music", "applause", "silence",
    "i'm sorry", "sorry",
}
_NON_SPEECH = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫]+")


@dataclass
class Transcript:
    text: str
    avg_logprob: float = 0.0  # mean token log-probability; closer to 0 = more confident
    no_speech_prob: float = 0.0  # Whisper's estimate that the clip had no speech
    compression_ratio: float = 1.0  # high = repetitive output (decoding loops)


@dataclass
class AudioStats:
    voiced_ms: int  # VAD-voiced duration of the utterance
    level_db: float  # loudness of the voiced frames, dBFS
    floor_db: float  # ambient noise floor before the utterance, dBFS


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z' ]", " ", text.lower())).strip()


def clean(text: str) -> str:
    """Drop non-speech annotations such as [Music], (laughs) or ♪."""
    return re.sub(r"\s{2,}", " ", _NON_SPEECH.sub(" ", text)).strip()


def repeats(text: str, times: int = 3) -> bool:
    """True if a single sentence appears `times` or more times."""
    sentences = [s for s in (norm(x) for x in re.split(r"[.!?]+", text)) if s]
    return any(sentences.count(s) >= times for s in set(sentences))


def junk(text: str) -> str | None:
    """Reject transcripts that are not speech at all, whatever the context."""
    n = norm(text)
    if not n:
        return "empty"
    if n in HALLUCINATIONS:
        return "noise phrase"
    if repeats(text):
        return "repeated sentence"
    return None


def check(kind: str, text: str, t: Transcript, a: AudioStats, ref_db: float | None,
          cfg: GateConfig) -> str | None:
    """kind: "wake" (wake word present), "followup" (no wake word, inside the
    follow-up window), "confirm" (answering an approval) or "residual" (speech
    recovered from a clip that also held the assistant's own voice)."""
    if reason := junk(text):
        return reason
    words = len(norm(text).split())
    if kind != "residual":  # residual stats describe the whole mixed clip
        if t.compression_ratio > cfg.max_compression_ratio:
            return f"repetitive decode (compression {t.compression_ratio:.1f})"
        if t.no_speech_prob > cfg.max_no_speech_prob and t.avg_logprob < cfg.min_logprob_followup:
            return f"probably not speech (no-speech {t.no_speech_prob:.2f})"
        rate = words / max(a.voiced_ms / 1000, 0.3)
        if rate > cfg.max_words_per_second:
            return f"too many words for the speech heard ({rate:.1f}/s)"
        floor = cfg.min_logprob_followup if kind == "followup" else cfg.min_logprob
        if t.avg_logprob < floor:
            return f"low confidence ({t.avg_logprob:.2f})"

    if kind in ("followup", "residual"):
        if words < cfg.followup_min_words:
            return f"too short without the wake word ({words} word{'s' if words != 1 else ''})"
    if kind == "followup":
        snr = a.level_db - a.floor_db
        if snr < cfg.followup_min_snr_db:
            return f"too close to background noise ({snr:.0f} dB)"
        if ref_db is not None and a.level_db < ref_db - cfg.followup_max_drop_db:
            return f"much quieter than your wake request ({a.level_db - ref_db:.0f} dB)"
    return None
