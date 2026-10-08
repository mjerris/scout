"""Configuration: defaults, overridden by config.toml in the project root."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class WakeConfig:
    # Words that wake the assistant when they appear in the first few words of
    # an utterance. Whisper spells "Claude" many ways, so the near-misses are
    # included.
    names: list[str] = field(
        default_factory=lambda: ["claude", "claud", "clod", "clawed", "cloud", "klaud", "claudia"]
    )
    max_position: int = 3  # name must be within the first N words
    # After a reply, listen this many seconds for a follow-up without the wake word.
    follow_up_seconds: float = 8.0


@dataclass
class AudioConfig:
    input_device: str = ""  # name substring or index; empty = system default
    output_device: str = ""
    vad_aggressiveness: int = 2  # 0-3, higher filters more non-speech
    silence_ms: int = 1200  # trailing silence that ends an utterance
    min_speech_ms: int = 300
    max_utterance_s: float = 30.0
    echo_tail_ms: int = 400  # ignore speech starting this soon after TTS stops


@dataclass
class GateConfig:
    """Local checks every transcript passes before it can reach Claude."""
    min_logprob: float = -1.0  # Whisper confidence floor with the wake word
    min_logprob_followup: float = -0.9  # stricter floor without it
    max_no_speech_prob: float = 0.6
    max_compression_ratio: float = 2.4
    max_words_per_second: float = 6.0  # of VAD-voiced speech
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
    always_ask: list[str] = field(default_factory=lambda: [
        "Bash(git push:*)", "Bash(gh pr merge:*)", "Bash(gh pr create:*)",
    ])
    # Show the voice session in Remote Control under this name ("" = off).
    remote_control: str = ""
    # Tools that run without asking under the strict policy.
    auto_allow_tools: list[str] = field(default_factory=lambda: [
        "Read", "Glob", "Grep", "LS", "WebSearch", "TodoWrite", "ToolSearch", "Task", "Agent",
        "BashOutput", "NotebookRead", "WebFetch",
    ])
    # Programs allowed as a single plain Bash command (no pipes, chaining or redirects).
    auto_allow_commands: list[str] = field(default_factory=lambda: [
        "date", "cal", "uptime", "whoami", "pwd", "ls", "df", "du", "which", "sw_vers",
    ])
    extra_system_prompt: str = ""


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8765


@dataclass
class Config:
    wake: WakeConfig = field(default_factory=WakeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    web: WebConfig = field(default_factory=WebConfig)

    def path(self, p: str) -> Path:
        """Resolve a config path relative to the project root."""
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else ROOT / q


def _merge(obj, data: dict, where: str) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        val = data.pop(f.name)
        cur = getattr(obj, f.name)
        if is_dataclass(cur):
            _merge(cur, dict(val), f"{where}{f.name}.")
        else:
            setattr(obj, f.name, val)
    if data:
        raise ValueError(f"unknown config keys: {', '.join(where + k for k in data)}")


def load(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or ROOT / "config.toml"
    if path.exists():
        with open(path, "rb") as fh:
            _merge(cfg, tomllib.load(fh), "")
    return cfg
