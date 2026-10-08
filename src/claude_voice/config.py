"""Configuration: defaults, overridden by config.toml in the project root."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class WakeConfig:
    # Words that wake the assistant when they appear in the first few words of
    # an utterance. Whisper spells "Claude" many ways, so the near-misses are
    # included.
    names: list[str] = field(
        # Not "cloud", "clawed", "clod" or "claudia": those are everyday words and
        # names, and woke the assistant during ordinary talk.
        default_factory=lambda: ["claude", "claud", "klaud"]
    )
    max_position: int = 3  # name must be within the first N words
    # After a reply, listen this many seconds for a follow-up without the wake word.
    follow_up_seconds: float = 8.0
    # Ending with "hang on" / "wait" / "give me a sec" keeps listening this long.
    wait_seconds: float = 20.0


@dataclass
class AudioConfig:
    input_device: str = ""  # name substring or index; empty = system default
    output_device: str = ""
    vad_aggressiveness: int = 2  # 0-3, higher filters more non-speech
    silence_ms: int = 1200  # trailing silence that ends an utterance
    min_speech_ms: int = 300
    max_utterance_s: float = 30.0
    echo_tail_ms: int = 400  # ignore speech starting this soon after TTS stops
    # The voice layer: "auto" (= webrtc: WebRTC echo cancellation on one duplex
    # stream; measured best on the Mac mini), "apple" (macOS voice processing;
    # build it with scripts/build-voiceio.sh), or "plain" (no echo cancellation).
    backend: str = "auto"
    # Talking over the assistant stops it (needs an echo-cancelling backend).
    barge_in: bool = True
    # End of turn: "smart" (Silero VAD + smart-turn model; falls back to "simple"
    # when the models aren't downloaded) or "simple" (silence_ms of quiet).
    turn_detection: str = "smart"
    min_pause_ms: int = 250  # smart: ask the turn model after this much quiet
    max_pause_ms: int = 1500  # smart: end the turn after this much quiet regardless
    turn_threshold: float = 0.5  # smart: "finished" probability that ends the turn
    # Keep the last N utterances (wav + json with transcript and stats) in
    # state/utterances/ for tuning. 0 = off. Audio stays on this machine.
    save_utterances: int = 0


@dataclass
class GateConfig:
    """Local checks every transcript passes before it can reach Claude."""

    min_logprob: float = -1.0  # Whisper confidence floor with the wake word
    min_logprob_followup: float = -0.9  # stricter floor without it
    max_no_speech_prob: float = 0.6
    max_compression_ratio: float = 2.4
    max_words_per_second: float = 6.0  # of VAD-voiced speech
    # A clip of at least coverage_min_seconds with fewer words per second than this
    # was mostly not transcribed (often our own voice mixed in); drop it.
    min_words_per_second: float = 0.5
    coverage_min_seconds: float = 4.0
    followup_min_words: int = 2
    followup_min_snr_db: float = 10.0  # above the room's noise floor
    followup_max_drop_db: float = 12.0  # quieter than your last wake request by more = someone else


@dataclass
class AsrConfig:
    model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "en"


@dataclass
class TtsConfig:
    model: str = "models/kokoro-v1.0.onnx"
    voices: str = "models/voices-v1.0.bin"
    voice: str = "af_heart"
    speed: float = 1.1
    chimes: bool = True
    working_sound: bool = True  # soft tick while tools run with nothing to say


@dataclass
class ClaudeConfig:
    cwd: str = "~"
    model: str = ""  # empty = Claude Code's default
    permission_mode: str = "default"
    setting_sources: list[str] = field(default_factory=lambda: ["user"])
    confirm_timeout_s: float = 30.0
    # "strict": only auto_allow_* below run without a spoken yes, even if your
    # ~/.claude settings allow more.
    # "settings": your settings' allow rules and hooks apply; you're asked out
    # loud only where Claude Code would normally prompt.
    # "settings_no_hooks": like "settings", but your settings' hooks are off (a
    # hook that answers "allow" would otherwise skip every prompt).
    approval_policy: str = "strict"
    # Under the settings policies, always ask out loud for these (permission rules).
    always_ask: list[str] = field(
        default_factory=lambda: [
            "Bash(git push:*)",
            "Bash(gh pr merge:*)",
            "Bash(gh pr create:*)",
        ]
    )
    # Show the voice session in Remote Control under this name ("" = off).
    remote_control: str = ""
    # Tools that run without asking under the strict policy.
    auto_allow_tools: list[str] = field(
        default_factory=lambda: [
            "Read",
            "Glob",
            "Grep",
            "LS",
            "WebSearch",
            "TodoWrite",
            "ToolSearch",
            "Task",
            "Agent",
            "BashOutput",
            "NotebookRead",
            "WebFetch",
        ]
    )
    # Programs allowed as a single plain Bash command (no pipes, chaining or redirects).
    auto_allow_commands: list[str] = field(
        default_factory=lambda: [
            "date",
            "cal",
            "uptime",
            "whoami",
            "pwd",
            "ls",
            "df",
            "du",
            "which",
            "sw_vers",
        ]
    )
    extra_system_prompt: str = ""


@dataclass
class WebConfig:
    enabled: bool = True
    # Addresses to listen on: "localhost" (which Tailscale HTTPS forwards to), "lan"
    # (this Mac's home-network address), "tailscale" (its tailnet address), or
    # literal IPs. Never all interfaces.
    hosts: list[str] = field(default_factory=lambda: ["localhost", "lan", "tailscale"])
    port: int = 8765


@dataclass
class FloorConfig:
    hold_seconds: float = 10.0  # how long a session may keep the floor between turns
    room_wait_seconds: float = 15.0  # a wake request waits this long for another session to finish


@dataclass
class Config:
    wake: WakeConfig = field(default_factory=WakeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    floor: FloorConfig = field(default_factory=FloorConfig)

    def path(self, p: str) -> Path:
        """Resolve a config path relative to the project root."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else ROOT / q


def _coerce(key: str, val: Any, cur: Any) -> Any:
    """Check a config value against the default's type, with clear errors."""
    if isinstance(cur, bool):
        if not isinstance(val, bool):
            raise ValueError(f"config {key} must be true or false, not {val!r}")
        return val
    if isinstance(cur, int):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"config {key} must be a whole number, not {val!r}")
        return val
    if isinstance(cur, float):
        if isinstance(val, bool) or not isinstance(val, int | float):
            raise ValueError(f"config {key} must be a number, not {val!r}")
        return float(val)
    if isinstance(cur, str):
        if isinstance(val, int) and not isinstance(val, bool) and key.endswith("_device"):
            return str(val)  # a device index
        if not isinstance(val, str):
            raise ValueError(f"config {key} must be a string, not {val!r}")
        return val
    if isinstance(cur, list):
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ValueError(f"config {key} must be a list of strings, not {val!r}")
        return val
    return val


def _merge(obj: Any, data: dict[str, Any], where: str) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        val = data.pop(f.name)
        cur = getattr(obj, f.name)
        if is_dataclass(cur):
            if not isinstance(val, dict):
                raise ValueError(f"config [{where}{f.name}] must be a section, not {val!r}")
            _merge(cur, dict(val), f"{where}{f.name}.")
        else:
            setattr(obj, f.name, _coerce(f"{where}{f.name}", val, cur))
    if data:
        raise ValueError(f"unknown config keys: {', '.join(where + k for k in data)}")


def load(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or ROOT / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            _merge(cfg, tomllib.load(fh), "")
    return cfg
