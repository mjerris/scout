"""Regex substitutions applied to text before it is spoken (TTS) and to
transcripts before they are used (STT).

Rule files have one rule per line:

    DIRECTION  pattern  replacement  # optional description

DIRECTION is TTS or STT; pattern is a Python regex; pattern and replacement
are shell-quoted when they contain spaces. Built-in defaults load first, then
pronounce.txt in the project root, so your rules can add to or undo them.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Rule:
    direction: str
    pattern: re.Pattern[str]
    replacement: str


def parse(text: str, source: str = "") -> list[Rule]:
    rules = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = shlex.split(line)
            if len(parts) != 3 or parts[0].upper() not in ("TTS", "STT"):
                raise ValueError("expected: TTS|STT pattern replacement")
            rules.append(Rule(parts[0].upper(), re.compile(parts[1], re.I), parts[2]))
        except (ValueError, re.error) as exc:
            log.warning("pronounce rule %s:%d skipped (%s): %s", source, n, exc, line)
    return rules


class Pronouncer:
    def __init__(self, user_file: Path | None = None) -> None:
        default = resources.files(__package__).joinpath("pronounce_default.txt").read_text()
        self.rules = parse(default, "default")
        if user_file and user_file.exists():
            self.rules += parse(user_file.read_text(), str(user_file))

    def _apply(self, direction: str, text: str) -> str:
        for r in list(self.rules):
            if r.direction == direction:
                try:
                    text = r.pattern.sub(r.replacement, text)
                except (re.error, IndexError) as exc:
                    log.warning("pronounce rule %r -> %r disabled: %s", r.pattern.pattern, r.replacement, exc)
                    self.rules.remove(r)
        return text

    def tts(self, text: str) -> str:
        return self._apply("TTS", text)

    def stt(self, text: str) -> str:
        return self._apply("STT", text)
