"""Fixed, parameter-checked Mac actions for the voice agent's own tools.

Every AppleScript here is a constant. Values from the agent are validated in
Python and passed as script arguments (`on run argv`), never spliced into the
script text, so a tool can't be turned into arbitrary AppleScript.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus, urlparse

BROWSERS = ("Google Chrome", "Safari", "Arc", "Firefox", "Microsoft Edge", "Brave Browser")
SEARCH_SITES = {
    "netflix": "https://www.netflix.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "maps": "https://www.google.com/maps/search/{q}",
}
_APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'+\-]{0,59}$")


class ToolError(ValueError):
    """A bad argument or a failed action, reported back to the agent."""


# --- validation (pure) -----------------------------------------------------------


def check_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ToolError("only http and https URLs can be opened")
    if any(c in url for c in "\n\r\t "):
        raise ToolError("URL must not contain whitespace")
    return url


def search_url(site: str, query: str) -> str:
    template = SEARCH_SITES.get((site or "").strip().lower())
    if template is None:
        raise ToolError(f"unknown site; choose one of {', '.join(SEARCH_SITES)}")
    query = (query or "").strip()
    if not query or len(query) > 200:
        raise ToolError("query must be 1-200 characters")
    return template.format(q=quote_plus(query))


def check_app_name(name: str) -> str:
    name = (name or "").strip()
    if not _APP_NAME.match(name):
        raise ToolError("not a valid app name")
    return name


def check_int(value: Any, lo: int, hi: int, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a whole number") from None
    if not lo <= n <= hi:
        raise ToolError(f"{what} must be between {lo} and {hi}")
    return n


# --- running things ------------------------------------------------------------------


async def _run(*argv: str, timeout: float = 10.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise ToolError(f"{argv[0]} timed out") from None
    if proc.returncode != 0:
        raise ToolError(err.decode(errors="replace").strip()[:300] or f"{argv[0]} failed")
    return out.decode(errors="replace").strip()


async def osascript(script: str, *args: str) -> str:
    return await _run("/usr/bin/osascript", "-e", script, *args)


async def is_running(process_name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pgrep",
        "-xq",
        process_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


# --- set 1: open things, browser, apps --------------------------------------------------


async def open_url(url: str) -> str:
    url = check_url(url)
    await _run("/usr/bin/open", url)
    return f"Opened {urlparse(url).netloc}."


async def search_site(site: str, query: str) -> str:
    url = search_url(site, query)
    await _run("/usr/bin/open", url)
    return f"Searched {site} for {query!r}."


async def open_app(name: str) -> str:
    name = check_app_name(name)
    await _run("/usr/bin/open", "-a", name)
    return f"Opened {name}."


_FRONTMOST = """
tell application "System Events" to return name of first application process whose frontmost is true
"""


async def frontmost_app() -> str:
    return await osascript(_FRONTMOST)


_RUNNING = """
tell application "System Events" to return name of every application process whose background only is false
"""


async def running_apps() -> str:
    return await osascript(_RUNNING)


_CHROME_TABS = """
on run argv
  set out to ""
  tell application "Google Chrome"
    set wi to 0
    repeat with w in windows
      set wi to wi + 1
      set ti to 0
      repeat with t in tabs of w
        set ti to ti + 1
        set out to out & wi & tab & ti & tab & (title of t) & tab & (URL of t) & linefeed
      end repeat
    end repeat
  end tell
  return out
end run
"""


async def list_tabs() -> str:
    if not await is_running("Google Chrome"):
        return "Google Chrome is not running."
    rows = []
    for line in (await osascript(_CHROME_TABS)).splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            w, t, title, url = parts
            rows.append(f"window {w} tab {t}: {title} ({urlparse(url).netloc})")
    return "\n".join(rows) or "No tabs open."


_CHROME_SWITCH = """
on run argv
  set w to (item 1 of argv) as integer
  set t to (item 2 of argv) as integer
  tell application "Google Chrome"
    set active tab index of window w to t
    set index of window w to 1
    activate
  end tell
end run
"""


async def switch_tab(window: int, tab: int) -> str:
    w = check_int(window, 1, 50, "window")
    t = check_int(tab, 1, 500, "tab")
    if not await is_running("Google Chrome"):
        raise ToolError("Google Chrome is not running")
    await osascript(_CHROME_SWITCH, str(w), str(t))
    return f"Switched to window {w} tab {t}."


_CHROME_NEW_TAB = """
on run argv
  set u to item 1 of argv
  tell application "Google Chrome"
    if (count of windows) is 0 then
      make new window
      set URL of active tab of front window to u
    else
      tell front window to make new tab with properties {URL:u}
    end if
    activate
  end tell
end run
"""


async def new_tab(url: str) -> str:
    url = check_url(url)
    await osascript(_CHROME_NEW_TAB, url)
    return f"Opened a new tab for {urlparse(url).netloc}."


_FULLSCREEN = """
on run argv
  set want to (item 1 of argv) is "true"
  tell application "System Events"
    set p to first application process whose frontmost is true
    tell p to set value of attribute "AXFullScreen" of front window to want
    return name of p
  end tell
end run
"""


async def fullscreen(on: bool) -> str:
    app = await osascript(_FULLSCREEN, "true" if on else "false")
    return f"{app} is {'now full screen' if on else 'out of full screen'}."


# --- set 2: media and volume ------------------------------------------------------------

_MEDIA = {
    # app -> action -> fixed script
    "Spotify": {
        "play_pause": 'tell application "Spotify" to playpause',
        "next": 'tell application "Spotify" to next track',
        "previous": 'tell application "Spotify" to previous track',
    },
    "Music": {
        "play_pause": 'tell application "Music" to playpause',
        "next": 'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
    },
}
_BROWSER_SPACE = """
tell application "System Events"
  set p to first application process whose frontmost is true
  key code 49
  return name of p
end tell
"""


async def media(action: str) -> str:
    if action not in ("play_pause", "next", "previous"):
        raise ToolError("action must be play_pause, next or previous")
    for app, scripts in _MEDIA.items():
        if await is_running(app):
            await osascript(scripts[action])
            return f"{app}: {action.replace('_', '/')}."
    if action == "play_pause":
        front = await frontmost_app()
        if front in BROWSERS:
            await osascript(_BROWSER_SPACE)  # space bar toggles most web players
            return f"Toggled playback in {front}."
        raise ToolError(f"nothing to play or pause; {front} is in front and no music app is running")
    raise ToolError("next and previous work with Spotify or Music only")


_GET_VOLUME = 'set s to get volume settings\nreturn ((output volume of s) as text) & "," & ((output muted of s) as text)'
_SET_VOLUME = "on run argv\nset volume output volume ((item 1 of argv) as integer)\nset volume output muted false\nend run"
_SET_MUTED = 'on run argv\nset volume output muted ((item 1 of argv) is "true")\nend run'


async def get_volume() -> tuple[int, bool]:
    level, muted = (await osascript(_GET_VOLUME)).split(",")
    if not level.strip().isdigit():  # e.g. "missing value" on HDMI outputs
        raise ToolError("this audio output doesn't report a volume level")
    return int(level), muted.strip() == "true"


async def volume(level: int | None = None, change: int | None = None, mute: bool | None = None) -> str:
    if mute is not None:
        await osascript(_SET_MUTED, "true" if mute else "false")
        return "Muted." if mute else "Unmuted."
    if level is not None:
        target = check_int(level, 0, 100, "level")
    elif change is not None:
        current, _ = await get_volume()
        target = max(0, min(100, current + check_int(change, -100, 100, "change")))
    else:
        current, muted = await get_volume()
        return f"Volume is {current}" + (" (muted)." if muted else ".")
    await osascript(_SET_VOLUME, str(target))
    return f"Volume set to {target}."
