"""Gate decisions, using transcripts seen in real use."""

import pytest

from claude_voice.config import GateConfig
from claude_voice.gate import AudioStats, Transcript, check, clean, junk, repeats

CFG = GateConfig()
CLEAR = Transcript("", avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.2)
# A normal "Hey Claude, ..." from across the room: 2 s of speech, 25 dB over the room.
NEAR = AudioStats(voiced_ms=2000, level_db=-30, floor_db=-55)


def tr(text, **kw):
    return Transcript(**{**CLEAR.__dict__, "text": text, **kw})


@pytest.mark.parametrize("text", [
    "Mmm.", "you", "Thank you.", "[Music]", "Thanks for watching!",
    "A conversation with an assistant named Claude. A conversation with an assistant named "
    "Claude. A conversation with an assistant named Claude. you",
])
def test_noise_is_junk(text):
    assert junk(clean(text)) is not None


@pytest.mark.parametrize("text", [
    "Hey Claude, what time is it?", "Yes.", "Okay.", "no", "Echo hello.",
    "Great. How long do you maintain and follow up after a previous?",
])
def test_speech_is_not_junk(text):
    assert junk(clean(text)) is None


def test_clean_strips_annotations():
    assert clean("[Music] Hey Claude (laughs) stop ♪") == "Hey Claude stop"


def test_repeats():
    assert repeats("Run it. Run it. Run it.")
    assert not repeats("Run it. Then run it again.")


def test_wake_request_accepted():
    assert check("wake", "Hey Claude, what time is it?", tr("x"), NEAR, None, CFG) is None


def test_low_confidence_rejected():
    assert "confidence" in check("wake", "Hey Claude, sing", tr("x", avg_logprob=-1.4), NEAR, None, CFG)


def test_followup_stricter_than_wake():
    t = tr("x", avg_logprob=-0.95)
    assert check("wake", "Hey Claude, what time is it", t, NEAR, None, CFG) is None
    assert "confidence" in check("followup", "what time is it", t, NEAR, None, CFG)


def test_too_many_words_for_speech():
    # A sentence of text from a fraction of a second of sound is a hallucination.
    blip = AudioStats(voiced_ms=300, level_db=-30, floor_db=-55)
    reason = check("followup", "I'm going to go ahead and put it in the background", tr("x"), blip, None, CFG)
    assert "too many words" in reason


@pytest.mark.parametrize("text", ["tunnel", "BING"])
def test_followup_one_word_rejected(text):
    assert "too short" in check("followup", text, tr("x"), NEAR, None, CFG)


def test_followup_near_noise_floor_rejected():
    faint = AudioStats(voiced_ms=2000, level_db=-50, floor_db=-55)
    assert "background noise" in check("followup", "what about tomorrow", tr("x"), faint, None, CFG)


def test_followup_much_quieter_than_user_rejected():
    # TV in the next room: well above the noise floor, but far quieter than you.
    tv = AudioStats(voiced_ms=2000, level_db=-45, floor_db=-60)
    assert "quieter" in check("followup", "what about tomorrow", tr("x"), tv, -28.0, CFG)
    assert check("followup", "what about tomorrow", tr("x"), NEAR, -28.0, CFG) is None


def test_residual_ignores_clip_stats():
    # Residual text came from a clip mixed with our own voice; its stats aren't the user's.
    t = tr("x", avg_logprob=-1.5, no_speech_prob=0.9)
    assert check("residual", "no that was right i have two accounts", t, NEAR, None, CFG) is None
    assert "too short" in check("residual", "hallow", t, NEAR, None, CFG)


def test_confirm_answer_accepted():
    assert check("confirm", "Yes.", tr("x"), AudioStats(400, -30, -55), None, CFG) is None
