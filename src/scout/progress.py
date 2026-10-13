"""What to say while a lookup runs, by the tool running (or the request): the room's
progress lines and the phone agent's fillers use the same words."""

from __future__ import annotations

import re

_PROGRESS = [
    (re.compile(r"\b(e-?mails?|mail|inbox)\b"), "Checking your mail."),
    (re.compile(r"\b(calendar|schedule|agenda|meetings?|events?)\b"), "Looking at your calendar."),
    (re.compile(r"\b(weather|forecast|rain|winds?|temperature)\b"), "Getting the weather."),
    (
        re.compile(r"\b(maps?|places?|route|directions|drive|driving|near|nearest|nearby)\b"),
        "Looking that up on the map.",
    ),
    (re.compile(r"web|search|fetch|look up"), "Searching."),
]
STILL_WORKING = "Still working on it."


def progress_phrase(what: str) -> str:
    """A short line for a lookup: `what` is a tool name or the request."""
    low = what.lower().replace("_", " ")
    return next((said for pattern, said in _PROGRESS if pattern.search(low)), "One moment.")


# A second line, so a slow tool doesn't repeat itself.
_AGAIN = {
    "Checking your mail.": "Going through your mail.",
    "Looking at your calendar.": "Checking the calendar.",
    "Getting the weather.": "Pulling up the forecast.",
    "Looking that up on the map.": "Checking the map.",
    "Searching.": "Still looking.",
    "One moment.": "Just a second.",
}


def fillers(name: str) -> list[str]:
    """Lines the phone agent says while tool `name` runs."""
    first = progress_phrase(name)
    return [first, _AGAIN.get(first, "Just a second.")]
