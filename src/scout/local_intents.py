"""Tier 0: everyday requests answered by plain code, in well under a second and
with no tokens: the time and date, timers, volume, play/pause, and what's on the
calendar today or tomorrow.

Matching is deliberately narrow (whole-request patterns, no guessing): anything
that doesn't clearly fit goes to Claude as before. Answers are short spoken
sentences. Each answer is remembered (Assistant.recent_local) so a follow-up that
does go to Claude ("and what about Friday?") still has the context."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import calendar_mac, mac

_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
}  # fmt: skip
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUMBERS), key=len, reverse=True)) + r")"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_POLITE = r"(?:please |can you |could you |would you |will you )*"
_TAIL = r"(?: please| for me| now)*"


def _clean(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[\"\u201c\u201d!?.,;:]+", " ", t)
    t = t.replace("\u2019", "'")  # curly apostrophe
    return re.sub(r"\s+", " ", t).strip()


def _num(word: str) -> float:
    return float(word) if word[0].isdigit() else float(_NUMBERS[word])


def _seconds(n: str, unit: str) -> float:
    v = _num(n)
    return v * 3600 if unit.startswith("h") else v * 60 if unit.startswith("m") else v


def _clock(t: dt.datetime) -> str:
    """'2:37 PM', 'noon', 'midnight', '3 o'clock' style is left to TTS; keep it plain."""
    if t.hour == 12 and t.minute == 0:
        return "noon"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    return t.strftime("%-I:%M %p") if t.minute else t.strftime("%-I %p")


def _duration(seconds: float) -> str:
    s = round(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' * (h != 1)}")
    if m:
        parts.append(f"{m} minute{'s' * (m != 1)}")
    if s and not h:
        parts.append(f"{s} second{'s' * (s != 1)}")
    return " and ".join(parts) or "0 seconds"


@dataclass
class Context:
    """What tier 0 can reach: the app's timers, and the Mac and calendar tools."""

    timers: Any  # tools.Timers
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone()
    volume: Callable[..., Awaitable[str]] = mac.volume
    media: Callable[[str], Awaitable[str]] = mac.media
    events: Callable[..., Awaitable[dict[str, Any]]] = calendar_mac.event_data


Handler = Callable[[re.Match[str], Context], Awaitable[str]]
_RULES: list[tuple[re.Pattern[str], Handler]] = []


def _rule(pattern: str) -> Callable[[Handler], Handler]:
    def add(fn: Handler) -> Handler:
        _RULES.append((re.compile(r"^" + _POLITE + pattern + _TAIL + r"$"), fn))
        return fn

    return add


@_rule(r"(?:what(?:'s| is) the time|what time is it|what time(?: is it)? now|tell me the time|time|the time)")
async def _time(m: re.Match[str], c: Context) -> str:
    return f"It's {_clock(c.now())}."


@_rule(
    r"(?:what(?:'s| is) (?:the date|today's date|the date today)|what day is (?:it|today)|what(?:'s| is) today)"
)
async def _date(m: re.Match[str], c: Context) -> str:
    return c.now().strftime("It's %A, %B %-d.")


@_rule(
    r"(?:set|start) (?:a |an )?(?:timer|alarm) (?:for )?"
    + _NUM
    + r" "
    + _UNIT
    + r"(?: (?:called|named|for) (.+))?"
)
async def _timer(m: re.Match[str], c: Context) -> str:
    seconds = _seconds(m.group(1), m.group(2))
    label = (m.group(3) or "").strip()
    c.timers.set(seconds, label)
    return f"Okay, {label + ' ' if label else ''}timer for {_duration(seconds)}."


@_rule(
    r"(?:set|start) (?:a |an )?" + _NUM + r" " + _UNIT + r" (?:timer|alarm)(?: (?:called|named|for) (.+))?"
)
async def _timer2(m: re.Match[str], c: Context) -> str:
    return await _timer(m, c)


@_rule(
    r"(?:how (?:much time is|long is) left|how(?:'s| is) (?:the|my) timer|(?:check|list) (?:the |my )?timers?)(?: on (?:the|my) timers?)?"
)
async def _timers_left(m: re.Match[str], c: Context) -> str:
    left = c.timers.listing()
    if not left:
        return "No timers are running."
    return "; ".join(left) + "."


@_rule(r"(?:cancel|stop|clear|delete) (?:the |my |all (?:the |my )?)?timers?")
async def _cancel_timers(m: re.Match[str], c: Context) -> str:
    gone = c.timers.cancel("all")
    return "Cancelled." if gone else "No timers are running."


@_rule(r"(?:turn (?:it|the volume|the sound) (up|down)|volume (up|down)|(louder|quieter|softer))")
async def _volume_step(m: re.Match[str], c: Context) -> str:
    word = next(g for g in m.groups() if g)
    up = word in ("up", "louder")
    await c.volume(change=10 if up else -10)
    return "Okay."


@_rule(r"(?:set |turn )?(?:the )?volume (?:to )?" + _NUM + r"(?: percent)?")
async def _volume_set(m: re.Match[str], c: Context) -> str:
    level = int(_num(m.group(1)))
    if not 0 <= level <= 100:
        return "Volume goes from 0 to 100."
    await c.volume(level=level)
    return f"Volume {level}."


@_rule(r"(mute|unmute)(?: (?:the )?(?:sound|volume|audio|mac|tv))?")
async def _mute(m: re.Match[str], c: Context) -> str:
    await c.volume(mute=m.group(1) == "mute")
    return "Okay."


@_rule(r"(pause|play|resume|unpause)(?: (?:the )?(?:music|song|video|show|it))?")
async def _play_pause(m: re.Match[str], c: Context) -> str:
    await c.media("play_pause")
    return ""  # the sound itself is the answer


@_rule(r"(?:(next|skip)|(previous|last|go back))(?: (?:song|track|one|video))?")
async def _skip(m: re.Match[str], c: Context) -> str:
    await c.media("next" if m.group(1) else "previous")
    return ""


def speak_events(data: dict[str, Any], day_word: str) -> str:
    """One or two spoken sentences for a day's events (calendar_mac.event_data)."""
    items = []
    for e in data.get("events", []):
        title = e.get("title") or "an untitled event"
        if e.get("all_day"):
            items.append(f"{title}, all day")
        else:
            start = dt.datetime.fromisoformat(e["start"])
            items.append(f"{title} at {_clock(start)}")
    if not items:
        return f"Nothing on your calendar {day_word}."
    if len(items) == 1:
        return f"{day_word.capitalize()} you have {items[0]}."
    more = f" That's {len(items)} things." if len(items) > 3 else ""
    return f"{day_word.capitalize()} you have {', '.join(items[:-1])}, and {items[-1]}.{more}"


@_rule(r"(?:what(?:'s| is) (?:on )?(?:my |the )?(?:calendar|schedule|agenda)(?: (?:for )?(today|tomorrow))?|"
       r"what(?:'s| is) on (?:my |the )?(?:calendar|schedule|agenda)(?: (?:for )?(today|tomorrow))?|"
       r"what do i have (?:on )?(today|tomorrow)|(?:am i|are we) (?:busy|free) (today|tomorrow))")  # fmt: skip
async def _calendar(m: re.Match[str], c: Context) -> str:
    day = next((g for g in m.groups() if g), "today")
    start = c.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if day == "tomorrow":
        start += dt.timedelta(days=1)
    end = start + dt.timedelta(days=1)
    return speak_events(await c.events(start.isoformat(), end.isoformat()), day)


async def answer(text: str, ctx: Context) -> str | None:
    """The spoken answer when tier 0 handles `text`, "" when it acted silently,
    or None to send it on to Claude."""
    t = _clean(text)
    for pattern, handler in _RULES:
        m = pattern.match(t)
        if m:
            return await handler(m, ctx)
    return None
