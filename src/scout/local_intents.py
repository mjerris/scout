"""Tier 0: everyday requests answered by plain code, in well under a second and
with no tokens: the time and date, timers, volume, play/pause, what's on the
calendar today or tomorrow, free and busy time ("am I free at 3", "when am I free
tomorrow"), reminder lists and what's due, the briefing ("brief me"), and the task list
("what should I work on", "what am I waiting on", "what does Pat owe me").

Tier 0 only reads. Adding or completing a reminder ("add milk to my shopping
list") is left to Claude, whose reminder_add / reminder_complete calls are
confirmed by voice every time; answering it here would skip that question.

Matching is deliberately narrow (whole-request patterns, no guessing): anything
that doesn't clearly fit goes to Claude as before. Answers are short spoken
sentences. Each answer is remembered (Assistant.recent_local) so a follow-up that
does go to Claude ("and what about Friday?") still has the context."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import (
    briefing,
    calendar_mac,
    freebusy,
    mac,
    mail_mac,
    maps,
    notes,
    trust,
    reminders_mac,
    starlink,
    tasks,
    weather,
)
from . import ledger
from .config import load
from .freebusy import clock as _clock
from .freebusy import join
from .mac import ToolError

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


_PLAYERS = ("Spotify", "Music")


async def _media(action: str) -> str:
    """mac.media, plus an explicit play or pause for Spotify and Music, so "play"
    never pauses music that is already playing. No browser space bar for these: it
    would type into whatever page is in front."""
    if action not in ("play", "pause"):
        return await mac.media(action)
    for app in _PLAYERS:
        if await mac.is_running(app):
            await mac.osascript(f'tell application "{app}" to {action}')
            return f"{app}: {action}."
    raise ToolError(f"nothing to {action}: Spotify and Music aren't running")


async def _load_tasks() -> list[tasks.Task]:
    return await asyncio.to_thread(tasks.all_tasks)


async def _task_people() -> list[str]:
    return await asyncio.to_thread(tasks._people)


@dataclass
class Context:
    """What tier 0 can reach: the app's timers, and the Mac and calendar tools."""

    timers: Any  # tools.Timers
    now: Callable[[], dt.datetime] = briefing.local_now  # a real zone: other days get their own offset
    volume: Callable[..., Awaitable[str]] = mac.volume
    media: Callable[[str], Awaitable[str]] = _media
    events: Callable[..., Awaitable[dict[str, Any]]] = calendar_mac.event_data
    reminders: Callable[..., Awaitable[dict[str, Any]]] = reminders_mac.reminder_data
    reminder_lists: Callable[[], Awaitable[list[str]]] = reminders_mac.list_names
    weather: Callable[[str], Awaitable[dict[str, Any]]] = weather.forecast
    starlink_on: Callable[[], bool] = lambda: load().starlink.enabled
    note_add: Callable[[str], Awaitable[dict[str, Any]]] = notes.add
    near: Callable[[str, str], Awaitable[dict[str, Any]]] = maps.near
    route: Callable[[str, str], Awaitable[dict[str, Any]]] = maps.route
    request: str = ""  # the request as said (answer() sets it)
    notes_find: Callable[[str], Awaitable[list[dict[str, Any]]]] = notes.find
    starlink: Callable[[], Awaitable[dict[str, Any]]] = starlink.status
    starlink_usage: Callable[[], dict[str, float]] = lambda: starlink.Usage().summary()
    mail: Callable[..., Awaitable[list[dict[str, Any]]]] = mail_mac.message_data
    hours: Callable[[], freebusy.Hours] = freebusy.work_hours
    last_spoken: Callable[[], tuple[str, str] | None] = lambda: None  # (who, text)
    task_list: Callable[[], Awaitable[list[tasks.Task]]] = _load_tasks
    people: Callable[[], Awaitable[list[str]]] = _task_people
    # The last answer used text from an email or meeting (a found task's title): spoken
    # here, but withheld from Claude in strict privacy mode like other private answers.
    private: bool = False


Handler = Callable[[re.Match[str], Context], Awaitable[str | None]]  # None: Claude takes it
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
async def _timer2(m: re.Match[str], c: Context) -> str | None:
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


@_rule(r"(pause|play|resume|unpause)(?: (?:the )?(music|song|video|show|it))?")
async def _play_pause(m: re.Match[str], c: Context) -> str:
    if m.group(2) in ("video", "show"):
        await c.media("play_pause")  # a web player: the browser's space bar toggles it
    else:
        await c.media("pause" if m.group(1) == "pause" else "play")
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


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY = r"(today|tomorrow|(?:on )?(?:this )?(?:" + "|".join(_WEEKDAYS) + r"))"
_HOURS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")
_MINUTE_WORDS = {"o'clock": 0, "fifteen": 15, "thirty": 30, "forty-five": 45}
_TIME_RE = (
    r"(noon|midnight|(?:\d{1,2}|"
    + "|".join(_HOURS)
    + r")(?:(?: |:)(?:\d{2}|o'clock|fifteen|thirty|forty-five))?"
    r"(?: ?(?:am|pm|a m|p m|in the morning|in the afternoon|in the evening|tonight))?)"
)


def _day(word: str | None, now: dt.datetime) -> tuple[dt.date, str]:
    """(the day, how to say it: "", "today", "tomorrow", "on Thursday")."""
    if not word:
        return now.date(), ""
    w = word.removeprefix("on ").removeprefix("this ")
    if w == "today":
        return now.date(), "today"
    if w == "tomorrow":
        return now.date() + dt.timedelta(days=1), "tomorrow"
    ahead = (_WEEKDAYS.index(w) - now.weekday()) % 7
    return now.date() + dt.timedelta(days=ahead), f"on {w.capitalize()}"


def _subject(word: str) -> str:
    w = word or "today"
    return w[0].upper() + w[1:]


def _first_day(m: re.Match[str]) -> str | None:
    return next(
        (
            g
            for g in m.groups()
            if g and g.removeprefix("on ").removeprefix("this ") in (*_WEEKDAYS, "today", "tomorrow")
        ),
        None,
    )


def parse_clock(text: str) -> dt.time | None:
    """A spoken time of day: "3", "3 30 pm", "three thirty", "noon". Without am or pm,
    1 to 6 is the afternoon."""
    if text in ("noon", "midnight"):
        return dt.time(12 if text == "noon" else 0)
    # The hour stops before a letter, so "3pm" is 3 and "pm".
    m = re.fullmatch(r"(\d{1,2}|[a-z]+)(?:[ :](\d{2}|o'clock|fifteen|thirty|forty-five))?(?: ?(.+))?", text)
    if not m:
        return None
    word, mins, suffix = m.groups()
    if not word.isdigit() and word not in _HOURS:
        return None
    h = int(word) if word.isdigit() else _HOURS.index(word) + 1
    minute = int(mins) if mins and mins.isdigit() else _MINUTE_WORDS.get(mins or "", 0)
    if h > 23 or minute > 59:
        return None
    pm = suffix in ("pm", "p m", "in the afternoon", "in the evening", "tonight")
    if suffix and h > 12:
        return None
    if pm and h < 12:
        h += 12
    elif suffix in ("am", "a m", "in the morning") and h == 12:
        h = 0
    elif not suffix and 1 <= h <= 6:
        h += 12
    return dt.time(h, minute)


async def _day_events(c: Context, day: dt.date) -> list[dict[str, Any]]:
    data = await c.events(*freebusy.day_range(day, c.now()))
    events: list[dict[str, Any]] = data.get("events", [])
    return events


@_rule(
    r"(?:am i|are we) (?:free|available|open) (?:"
    + _DAY
    + r" )?(?:at |around )?"
    + _TIME_RE
    + r"(?: "
    + _DAY
    + r")?"
)
async def _free_at(m: re.Match[str], c: Context) -> str | None:
    when = parse_clock(m.group(2))
    if when is None:
        return None
    now = c.now()
    day, word = _day(m.group(1) or m.group(3), now)
    at = dt.datetime.combine(day, when, now.tzinfo)
    events = await _day_events(c, day)
    spans = freebusy.timed(events)
    suffix = f" {word}" if word else ""
    clash = [s for s in spans if s.start < at + dt.timedelta(minutes=30) and s.end > at]
    if clash:
        block = freebusy.merge(clash)[0]
        titles = join([t for s in clash for t in s.titles])
        return f"No, you have {titles} from {freebusy.span_words(block)}{suffix}."
    out = f"Yes, you're free at {_clock(at)}{suffix}."
    if allday := freebusy.all_day(events, day):  # e.g. out of office: not busy, but worth knowing
        out += f" You have {join(allday)} all day."
    later = [s for s in spans if at < s.start < at + dt.timedelta(hours=3)]
    if later:
        out += f" {later[0].titles[0]} starts at {_clock(later[0].start)}."
    return out


@_rule(r"(?:when am i free|when(?:'s| is| do i have) (?:my )?(?:some )?free time)(?: " + _DAY + r")?")
async def _when_free(m: re.Match[str], c: Context) -> str:
    now = c.now()
    day, word = _day(m.group(1), now)
    start, end = c.hours()
    ws, we = freebusy.window(day, (start, end), now)
    if ws >= we:
        return "There's no working time left today."
    busy = freebusy.merge(freebusy.timed(await _day_events(c, day)))
    free = freebusy.gaps(busy, ws, we, 15)
    subject = _subject(word)
    if not free:
        return f"{subject} you're booked from {_clock(ws)} to {_clock(we)}."
    if len(free) == 1 and free[0].start == ws and free[0].end == we:
        if ws.time() > start:
            return f"You're free for the rest of the working day, until {_clock(we)}."
        return f"{subject} you're free all day, from {_clock(ws)} to {_clock(we)}."
    items = []
    for g in free[:5]:
        if g.end == we:
            items.append(f"after {_clock(g.start)}")
        elif g.start == ws:
            items.append(f"until {_clock(g.end)}")
        else:
            items.append(freebusy.span_words(g))
    return f"{subject} you're free {join(items)}."


@_rule(r"(?:(?:what|when)(?:'s| is) )?my (?:first|next) free (hour|half hour)(?: " + _DAY + r")?")
async def _first_free(m: re.Match[str], c: Context) -> str:
    now = c.now()
    day, word = _day(m.group(2), now)
    ws, we = freebusy.window(day, c.hours(), now)
    what = m.group(1)
    if ws >= we:
        return "There's no working time left today."
    busy = freebusy.merge(freebusy.timed(await _day_events(c, day)))
    free = freebusy.gaps(busy, ws, we, 30 if what == "half hour" else 60)
    if not free:
        return f"You don't have a free {what} {word or 'today'} between {_clock(ws)} and {_clock(we)}."
    return f"{_subject(word)} your first free {what} starts at {_clock(free[0].start)}; you're free until {_clock(free[0].end)}."


@_rule(
    r"how (?:busy|full|packed) (?:is my (?:day|calendar|schedule)(?: "
    + _DAY
    + r")?|am i(?: "
    + _DAY
    + r")?|is "
    + _DAY
    + r")"
)
async def _how_busy(m: re.Match[str], c: Context) -> str:
    now = c.now()
    day, word = _day(_first_day(m), now)
    return freebusy.day_load(await _day_events(c, day), day, c.hours(), now, _subject(word))


@_rule(
    r"(?:brief me|(?:give me |read me )?(?:my |the |a )?(?:morning |daily )?briefing|"
    r"what(?:'s| does) my day look like|how(?:'s| is| does) my day look(?:ing)?(?: like)?)(?: today)?"
)
async def _briefing(m: re.Match[str], c: Context) -> str:
    return await briefing.build(c.now(), c.events, c.reminders, c.mail, c.hours())


# The task list (tasks.py). Spoken here only, so found tasks' real titles are used; the
# answer is marked private when one is, for strict privacy mode.

_THEN = r"(?: next| now| first| today| right now)"


@_rule(
    r"(?:what should i (?:(?:work on|tackle|focus on|start (?:on|with)|be working on)" + _THEN + r"?|"
    r"(?:do|be doing)" + _THEN + r")|"
    r"what(?:'s| is) next(?: on my (?:list|plate|task list))?|"
    r"what(?:'s| is| are) my (?:next|top) (?:task|tasks|priority|priorities|thing)|"
    r"what do i (?:need|have) to do next|what(?:'s| is) the next thing on my list)"
)
async def _task_next(m: re.Match[str], c: Context) -> str:
    text, c.private = tasks.speak_next(await c.task_list(), c.now().date(), await c.people())
    return text


@_rule(
    r"(?:what(?:'s| is) on my (?:task )?list|what(?:'s| is) on my plate|what do i have on my plate|"
    r"what are my tasks|what(?:'s| is| are) on my tasks|what tasks do i have|"
    r"(?:list|read(?: me)?|tell me) my tasks)"
)
async def _task_list(m: re.Match[str], c: Context) -> str:
    text, c.private = tasks.speak_list(await c.task_list(), c.now().date(), await c.people())
    return text


@_rule(
    r"how many (?:open )?(?:tasks|things|to-?dos) (?:do i have|have i got|are on my (?:list|plate))"
    r"(?: (?:left|open|today))?"
)
async def _task_count(m: re.Match[str], c: Context) -> str:
    text, c.private = tasks.speak_count(await c.task_list(), c.now().date())
    return text


_WHO = r"(?P<who>[a-z][a-z'.-]*(?: [a-z][a-z'.-]*)?)"


@_rule(
    r"(?:what am i (?:still )?waiting (?:on|for)|who owes me (?:anything|something|stuff)|"
    r"what do (?:people|others) owe me)"
)
async def _task_waiting(m: re.Match[str], c: Context) -> str | None:
    got = tasks.speak_waiting(await c.task_list(), c.now().date())
    if got is None:
        return None
    text, c.private = got
    return text


@_rule(
    r"(?:what am i (?:still )?waiting (?:on|for) from "
    + _WHO
    + r"|what(?:'s| is) "
    + _WHO.replace("who", "who2")
    + r" (?:working on|doing) for me|what does "
    + _WHO.replace("who", "who3")
    + r" owe me)"
)
async def _task_owed_by(m: re.Match[str], c: Context) -> str | None:
    name = m.group("who") or m.group("who2") or m.group("who3")
    got = tasks.speak_waiting(await c.task_list(), c.now().date(), name)
    if got is None:
        return None  # nothing from them on the list ("what is the team doing for me"): Claude
    text, c.private = got
    return text


@_rule(r"(?:what(?:'s| is) on|read(?: me)?|check) (?:my |the )?((?:[a-z0-9'-]+ ){0,2}?[a-z0-9'-]+) list")
async def _reminder_list(m: re.Match[str], c: Context) -> str | None:
    name = m.group(1)
    if name in ("my", "the", "reading", "mailing", "email", "mail", "contact", "contacts"):
        return None
    try:
        items = (await c.reminders(name)).get("reminders", [])
    except ToolError as exc:
        # No list by that name (helper or local lists): it may be a guest list in a
        # note or a doc, so Claude takes it.
        if re.match(r"no reminder list matches|there's no .* list", str(exc), re.I):
            return None
        raise
    if not items:
        return f"Your {name} list is empty."
    titles = [r.get("title") or "an untitled item" for r in items[:8]]
    if len(items) > 8:
        return f"Your {name} list has {len(items)} things, starting with {join(titles)}."
    return f"Your {name} list has {join(titles)}."


@_rule(
    r"(?:(?:what are|read(?: me)?|what(?:'s| is)) my reminders(?: (?:for )?" + _DAY + r")?|"
    r"what reminders do i have(?: " + _DAY + r")?|(?:do i have )?any reminders(?: " + _DAY + r")?|"
    r"what(?:'s| is) due " + _DAY + r")"
)
async def _reminders_due(m: re.Match[str], c: Context) -> str:
    now = c.now()
    word = _first_day(m)
    if not word:
        items = (await c.reminders(None)).get("reminders", [])
        if not items:
            return "You have no open reminders."
        late = sum(reminders_mac.is_overdue(r, now) for r in items)
        titles = [r.get("title") or "an untitled reminder" for r in items[:5]]
        more = f", and {len(items) - 5} more" if len(items) > 5 else ""
        head = f"You have {len(items)} open reminder{'s' * (len(items) != 1)}" + (
            f", {late} overdue" if late else ""
        )
        return f"{head}: {join(titles) if not more else ', '.join(titles)}{more}."
    day, spoken = _day(word, now)
    before = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), now.tzinfo)
    items = (await c.reminders(None, False, before.isoformat())).get("reminders", [])
    if day == now.date():
        return briefing.reminders_part(items, now)
    due = [r for r in items if (t := reminders_mac.due_time(r)) and t.date() == day]
    if not due:
        return f"No reminders due {spoken}."
    titles = [r.get("title") or "an untitled reminder" for r in due[:5]]
    return f"{_subject(spoken)} you have {len(due)} reminder{'s' * (len(due) != 1)} due: {join(titles)}."


_REPEAT = (
    r"(?:(?:sorry |what |huh |pardon )*(?:i )?(?:didn't|did not|couldn't|could not) (?:catch|hear|get) (?:that|it|you)"
    r"(?: (?:can|could) you (?:say|repeat) (?:it|that)(?: again)?)?|(?:say|repeat) (?:that|it)(?: again)?|"
    r"what did you say|come again|pardon|what was that)"
)


def is_repeat(text: str) -> bool:
    return bool(re.match(r"^" + _POLITE + _REPEAT + _TAIL + r"$", _clean(text)))


@_rule(_REPEAT)
async def _repeat(m: re.Match[str], c: Context) -> str:
    last = c.last_spoken()
    if not last:
        return "I haven't said anything yet."
    who, text = last
    return text if who == "Scout" else f"{who.split('#')[0]} said: {text}"


_PLACE = (
    r"(?: (?:in|for|at|near) (?!(?:today|tonight|tomorrow|now|right now|this evening|here|"
    r"the (?:morning|afternoon|evening|night|weekend|week)|this (?:morning|afternoon|weekend|week))\b)"
    r"(?P<place>[a-z][a-z .'-]*?(?:, ?[a-z][a-z .'-]*?)?))?"
)


@_rule(
    r"(?:what(?:'s| is) the |how(?:'s| is) the |)(?P<what>weather|forecast|wind|temperature)"
    r"(?: forecast)?(?: like)?"
    + _PLACE
    + r"(?: (?:for |this |in the )?(?P<when>tomorrow morning|today|tonight|tomorrow|right now|now|this evening|evening|morning))?"
    + _PLACE.replace("place", "place2")
)
async def _weather(m: re.Match[str], c: Context) -> str | None:
    place = m.group("place") or m.group("place2") or ""
    if place.strip() == "home":
        place = ""  # weather.home
    when = (
        (m.group("when") or "now")
        .replace("right now", "now")
        .replace("this evening", "tonight")
        .replace("tomorrow morning", "morning")
        .replace("evening", "tonight")
    )
    if m.group("what") == "wind" and when == "now":
        when = "wind"
    data = await c.weather(place.strip())
    return weather.speak(data, when)


@_rule(
    r"(?:is it|will it|is it going to|are we going to get) (?:rain|snow)(?:ing)?"
    + _PLACE
    + r"(?: (?P<when>today|tonight|tomorrow))?"
)
async def _rain(m: re.Match[str], c: Context) -> str | None:
    when = m.group("when") or "today"
    data = await c.weather((m.group("place") or "").strip())
    return weather.speak(
        data, "tonight" if when == "tonight" else f"rain-{when}" if when == "tomorrow" else "rain"
    )


_DISH = r"(?:the )?(?:starlink|internet|connection|dish|wi-?fi)"


@_rule(
    r"(?:(?:is|how(?:'s| is)|what(?:'s| is)(?: up with)?|check) "
    + _DISH
    + r"(?: (?:down|out|up|working|ok|okay|online|offline|status|doing))?"
    + r"|(?:starlink|dish) status|are we (?:online|offline))"
)
async def _starlink(m: re.Match[str], c: Context) -> str | None:
    if not c.starlink_on():
        return None
    if (
        "starlink" not in m.group(0)
        and "dish" not in m.group(0)
        and "online" not in m.group(0)
        and "out" not in m.group(0)
        and "down" not in m.group(0)
    ):
        return None  # "how's the wifi" alone: leave it to Claude
    return starlink.speak(await c.starlink())


@_rule(r"why is " + _DISH + r" (?:so )?(?:slow|bad|laggy|spotty|choppy|dropping|cutting out)")
async def _starlink_slow(m: re.Match[str], c: Context) -> str | None:
    if not c.starlink_on():
        return None
    return starlink.speak(await c.starlink(), "slow")


@_rule(
    r"how much (?:data|internet|starlink(?: data)?) (?:have we|did we|have i|did i) used?(?: (?:today|this month))?"
)
async def _starlink_usage(m: re.Match[str], c: Context) -> str | None:
    if not c.starlink_on():
        return None
    return starlink.speak({}, "usage", c.starlink_usage())


_NOTE = r"(?:(?:take|make|add|write(?: down)?|save) (?:a )?note(?: (?:that|of|saying))?|note that) "


@_rule(_NOTE + r"(?P<text>.+)")
async def _note(m: re.Match[str], c: Context) -> str:
    # The note's own words as said (case and punctuation kept), not the cleaned match.
    said = re.sub(
        r"^\W*" + _POLITE.replace(" ", r"[\s,:]+") + r"\W*" + _NOTE.replace(" ", r"[\s,:]+"),
        "",
        c.request.strip(),
        flags=re.I,
    )
    await c.note_add(said.strip() or m.group("text"))
    return "Noted."


@_rule(
    r"(?:(?:what|which) (?:are |were |is )?my notes?|(?:read|find|check)(?: me)? my notes?|"
    r"do i have (?:a |any )?notes?)(?: (?:about|on|for|from|regarding) (?P<about>.+))?"
)  # one group: _rule anchors the whole pattern
async def _notes(m: re.Match[str], c: Context) -> str:
    return notes.speak(await c.notes_find((m.group("about") or "").strip()))


@_rule(
    r"(?:how far (?:is it )?from (?P<a>.+?) to (?P<b>.+)|how far is (?P<b2>.+?) from (?P<a2>.+)|"
    r"how long (?:does it take |is the drive |will it take )?to (?:drive|get) from (?P<a3>.+?) to (?P<b3>.+)|"
    r"(?:what(?:'s| is) the )?(?:driving )?distance (?:from|between) (?P<a4>.+?) (?:to|and) (?P<b4>.+))"
)
async def _route(m: re.Match[str], c: Context) -> str | None:
    a = m.group("a") or m.group("a2") or m.group("a3") or m.group("a4")
    b = m.group("b") or m.group("b2") or m.group("b3") or m.group("b4")
    if {a, b} & {"here", "home", "me", "us", "where i am", "where we are", "my location"}:
        return None  # maps can't place "here": Claude works out where that is
    return maps.speak_route(await c.route(a, b))


@_rule(
    r"(?:where(?:'s| is) |find |what(?:'s| is) )?(?:the |a )?(?:nearest|closest) (?P<what>.+?) "
    r"(?:near|to|in|around|by) (?P<place>.+)"
)
async def _near(m: re.Match[str], c: Context) -> str | None:
    if maps.category(m.group("what")) is None:
        return None  # a name or something unusual: Claude, with the places_near tool
    return maps.speak_near(await c.near(m.group("what"), m.group("place")))


_ACTION = r"(?P<action>(?:adding |add |new |marking |mark |completing |complete |finishing |checking off |creating |deleting |delete |removing |sending |send )?[a-z -]+?)"


@_rule(
    r"(?:always allow|(?:you can )?stop asking (?:me )?(?:about|before)|don't ask (?:me )?(?:about|before)|just go ahead with) "
    + _ACTION
)
async def _trust_on(m: re.Match[str], c: Context) -> str | None:
    action = trust.action_for(m.group("action"))
    return trust.default().promote(action) if action else None


@_rule(
    r"(?:always ask (?:me )?(?:about|before)|(?:start )?ask(?:ing)? me (?:again )?(?:about|before)) "
    + _ACTION
)
async def _trust_off(m: re.Match[str], c: Context) -> str | None:
    action = trust.action_for(m.group("action"))
    return trust.default().demote(action) if action else None


@_rule(r"what (?:do you|can you) do without asking(?: me)?|what(?:'s| is) (?:on )?auto(?:matic)?")
async def _trust_status(m: re.Match[str], c: Context) -> str:
    return trust.default().speak_status()


@_rule(
    r"(?:how much (?:have you|did you|has claude|did claude) (?:spent|cost|used)|how much (?:claude )?usage|"
    r"how many (?:requests|questions) (?:did you|have you) (?:answer|handle)(?:ed)?(?: yourself| locally)?|"
    r"(?:what(?:'s| is) (?:the|my) )?(?:claude )?(?:usage|cost)(?: report)?)"
    r"(?: (?P<period>today|this week|this month))?"
)
async def _ledger(m: re.Match[str], c: Context) -> str:
    period = (m.group("period") or "today").removeprefix("this ")
    return ledger.speak(period, ledger.summary(ledger.start_of(period, c.now())))


# What a request from the phone may not do here: change the room (timers that ring in it,
# volume, playback), repeat what the room last heard, or change what asks first.
PHONE_SKIP = frozenset({
    "_timer", "_timer2", "_cancel_timers", "_volume_step", "_volume_set", "_mute",
    "_play_pause", "_skip", "_repeat", "_trust_on", "_trust_off",
})  # fmt: skip


async def answer(text: str, ctx: Context, skip: frozenset[str] = frozenset()) -> str | None:
    """The spoken answer when tier 0 handles `text`, "" when it acted silently,
    or None to send it on to Claude. Handlers named in `skip` are passed over."""
    t = _clean(text)
    ctx.request = text
    ctx.private = False
    for pattern, handler in _RULES:
        if handler.__name__ in skip:
            continue
        m = pattern.match(t)
        if m:
            return await handler(m, ctx)
    return None
