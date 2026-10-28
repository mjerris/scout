"""Regex substitutions applied to text before it is spoken (TTS) and to
transcripts before they are used (STT).

Rule files have one rule per line:

    DIRECTION  pattern  replacement  # optional description

DIRECTION is TTS or STT; pattern is a Python regex; pattern and replacement
are shell-quoted when they contain spaces. Built-in defaults load first, then
pronounce.txt in Scout's data folder, so your rules can add to or undo them; it's read
again when it changes, and say_as() writes it (the pronounce_set tool: "you're saying
Andrey wrong"). Before the TTS rules, identifiers are put digit by digit ("ticket 61723" -> "ticket 6 1 7 2 3",
speech.speak_ids).
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .speech import speak_ids

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
        self.defaults = parse(default, "default")
        self.user_file = user_file
        self._stamp: tuple[int, int] | None = None
        self.rules = list(self.defaults)
        self._fresh()

    def _fresh(self) -> None:
        """Reload the user's rules when their file changed (a rule just added)."""
        if self.user_file is None:
            return
        try:
            st = self.user_file.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            mine = parse(self.user_file.read_text(), str(self.user_file)) if stamp is not None else []
        except OSError:
            mine = []
        self.rules = [*self.defaults, *mine]

    def _apply(self, direction: str, text: str) -> str:
        self._fresh()
        for r in list(self.rules):
            if r.direction == direction:
                try:
                    text = r.pattern.sub(r.replacement, text)
                except (re.error, IndexError) as exc:
                    log.warning("pronounce rule %r -> %r disabled: %s", r.pattern.pattern, r.replacement, exc)
                    self.rules.remove(r)
                    if r in self.defaults:
                        self.defaults.remove(r)
        return text

    def tts(self, text: str) -> str:
        """Text as it's said: ticket and other identifiers digit by digit
        (speech.speak_ids), then the TTS rules."""
        return self._apply("TTS", speak_ids(text))

    def stt(self, text: str) -> str:
        return self._apply("STT", text)


_WORD = re.compile(r"[\w][\w' .-]{0,39}")


def say_as(user_file: Path, word: str, sound: str) -> str:
    """Make Scout say `word` as `sound` (a spelling the voice reads right: "Andre",
    "Ahn-dray"), replacing an earlier rule for it; an empty `sound` drops that rule."""
    word, sound = word.strip(), sound.strip()
    if not _WORD.fullmatch(word):
        raise ValueError(f"not a word or name: {word!r}")
    if sound and not _WORD.fullmatch(sound):
        raise ValueError(f"say it with letters, hyphens and spaces: {sound!r}")
    pattern = rf"\b{re.escape(word)}\b"
    lines = user_file.read_text().splitlines() if user_file.exists() else []
    kept = []
    for line in lines:
        rules = parse(line.split(" #", 1)[0])
        if rules and rules[0].direction == "TTS" and rules[0].pattern.pattern.lower() == pattern.lower():
            continue
        kept.append(line)
    if not lines:
        kept = [
            "# Your pronunciation rules (added to Scout's built-in ones).",
            "# DIRECTION  pattern  replacement",
        ]
    if sound:
        kept.append(f"TTS  {shlex.quote(pattern)}  {shlex.quote(sound)}")
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("\n".join(kept) + "\n")
    if not sound:
        return f"Scout says {word} the voice's own way again."
    return f"From now on Scout says {word} as {sound}."
