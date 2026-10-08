"""The voice agent's own tools (an in-process MCP server). All of them are
pre-approved: they do one fixed, safe thing with checked arguments."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from . import mac
from .shared_tools import SHARED
from .config import DATA

log = logging.getLogger(__name__)

SERVER = "voice_app"
REQUESTS_FILE = DATA / "state" / "change-requests.jsonl"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


class Timers:
    """In-memory timers; when one fires, `announce(label)` is called."""

    MAX_MINUTES = 24 * 60

    def __init__(self, announce: Callable[[str], None]) -> None:
        self.announce = announce
        self._ids = itertools.count(1)
        self.active: dict[int, tuple[str, float, asyncio.Task[Any]]] = {}

    def set(self, seconds: float, label: str) -> int:
        if not 1 <= seconds <= self.MAX_MINUTES * 60:
            raise mac.ToolError("timers can run from 1 second to 24 hours")
        label = (label or "").strip()[:60] or "timer"
        tid = next(self._ids)
        self.active[tid] = (label, time.monotonic() + seconds, asyncio.create_task(self._fire(tid, seconds)))
        return tid

    async def _fire(self, tid: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        label, _, _ = self.active.pop(tid, ("timer", 0, None))
        log.info("timer done: %s", label)
        self.announce(label)

    def cancel(self, which: str) -> list[str]:
        which = (which or "").strip().lower()
        hits = [
            tid
            for tid, (label, _, _) in self.active.items()
            if which in ("all", "") or which == str(tid) or which in label.lower()
        ]
        names = []
        for tid in hits:
            label, _, task = self.active.pop(tid)
            task.cancel()
            names.append(label)
        return names

    def listing(self) -> list[str]:
        now = time.monotonic()
        return [
            f"{label}: {_human(due - now)} left"
            for label, due, _ in sorted(self.active.values(), key=lambda v: v[1])
        ]


def _human(seconds: float) -> str:
    s = max(0, round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' * (h != 1)}"] * bool(h) + [f"{m} minute{'s' * (m != 1)}"] * bool(m)
    if s and not h:
        parts.append(f"{s} second{'s' * (s != 1)}")
    return " ".join(parts) or "0 seconds"


def build_server(timers: Timers) -> tuple[McpSdkServerConfig, list[str]]:
    def wrap(
        fn: Callable[[dict[str, Any]], Awaitable[str]],
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return _ok(await fn(args))
            except mac.ToolError as exc:
                return _err(str(exc))
            except Exception as exc:  # unexpected: surface it, don't crash the session
                log.exception("tool failed")
                return _err(f"failed: {exc}")

        return handler

    tools = [
        tool("open_url", "Open an http(s) URL in the default browser.", {"url": str})(
            wrap(lambda a: mac.open_url(a["url"]))
        ),
        tool(
            "search_site",
            "Search a site in the browser. site: " + ", ".join(mac.SEARCH_SITES) + ".",
            {"site": str, "query": str},
        )(wrap(lambda a: mac.search_site(a["site"], a["query"]))),
        tool("open_app", "Open (or bring to front) an installed Mac app by name.", {"name": str})(
            wrap(lambda a: mac.open_app(a["name"]))
        ),
        tool("frontmost_app", "Name of the app currently in front.", {})(wrap(lambda a: mac.frontmost_app())),
        tool("running_apps", "Names of the apps currently open.", {})(wrap(lambda a: mac.running_apps())),
        tool("list_tabs", "List Google Chrome windows and tabs (title and site).", {})(
            wrap(lambda a: mac.list_tabs())
        ),
        tool(
            "switch_tab",
            "Bring a Google Chrome tab to the front (numbers from list_tabs).",
            {"window": int, "tab": int},
        )(wrap(lambda a: mac.switch_tab(a["window"], a["tab"]))),
        tool("new_tab", "Open an http(s) URL in a new Google Chrome tab.", {"url": str})(
            wrap(lambda a: mac.new_tab(a["url"]))
        ),
        tool(
            "fullscreen",
            "Put the front window into (on=true) or out of (on=false) full screen.",
            {"on": bool},
        )(wrap(lambda a: mac.fullscreen(bool(a["on"])))),
        tool(
            "media",
            "Control playback: action is play_pause, next or previous. Uses Spotify or "
            "Music if running, otherwise play_pause toggles a video in the front browser.",
            {"action": str},
        )(wrap(lambda a: mac.media(a["action"]))),
        tool(
            "volume",
            "Get or set the Mac's output volume. Give level (0-100), or change "
            "(e.g. -10 or 10), or mute (true/false); give none of them to read the volume.",
            {
                "type": "object",
                "properties": {
                    "level": {"type": "integer"},
                    "change": {"type": "integer"},
                    "mute": {"type": "boolean"},
                },
            },
        )(wrap(lambda a: mac.volume(a.get("level"), a.get("change"), a.get("mute")))),
        tool(
            "set_timer",
            "Start a timer; when it ends the assistant announces it out loud.",
            {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number"},
                    "seconds": {"type": "number"},
                    "label": {"type": "string"},
                },
            },
        )(wrap(lambda a: _set_timer(timers, a))),
        tool("list_timers", "List running timers and the time left on each.", {})(
            wrap(lambda a: _async("\n".join(timers.listing()) or "No timers running."))
        ),
        tool("cancel_timer", "Cancel timers by label or number, or 'all'.", {"which": str})(
            wrap(lambda a: _async(_cancelled(timers.cancel(a.get("which", "")))))
        ),
        tool(
            "request_app_change",
            "Send a requested change to this voice app (how it listens, talks, asks for "
            "approval, its config) to the Claude session that maintains it.",
            {"summary": str, "details": str},
        )(wrap(lambda a: _request_change(a))),
    ]
    # Mail and calendar: the same definitions the MCP server offers other sessions.
    tools += [tool(t.name, t.description, t.schema)(wrap(t.run)) for t in SHARED]
    return create_sdk_mcp_server(SERVER, tools=tools), [f"mcp__{SERVER}__{t.name}" for t in tools]


async def _async(value: str) -> str:
    return value


async def _set_timer(timers: Timers, a: dict[str, Any]) -> str:
    seconds = float(a.get("minutes") or 0) * 60 + float(a.get("seconds") or 0)
    label = a.get("label") or ""
    timers.set(seconds, label)
    return f"Timer set for {_human(seconds)}" + (f" ({label})." if label else ".")


def _cancelled(names: list[str]) -> str:
    return f"Cancelled: {', '.join(names)}." if names else "No matching timer."


_REQUEST_TIMES: list[float] = []


async def _request_change(a: dict[str, Any]) -> str:
    now = time.time()
    _REQUEST_TIMES[:] = [t for t in _REQUEST_TIMES if now - t < 3600]
    if len(_REQUEST_TIMES) >= 20:
        raise mac.ToolError("too many change requests this hour")
    _REQUEST_TIMES.append(now)
    entry = {
        "ts": now,
        # Agent-written text for the owner session to read: bounded, marked as such.
        "from": "voice agent (not the user's own words)",
        "summary": str(a.get("summary", ""))[:200],
        "details": str(a.get("details", ""))[:2000],
    }
    REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REQUESTS_FILE.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("change request: %s", entry["summary"])
    return "Sent to the owner session."
