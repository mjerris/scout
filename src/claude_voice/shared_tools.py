"""Mail and calendar tools, defined once and served two ways:
- to the room assistant, inside its in-process voice_app tool server;
- to any other Claude session, through the MCP server (mcp_server.py), which
  forwards each call to the running app (POST /api/tool).

The app always does the work, so macOS's calendar and Mail permissions belong
to it alone. Tools with `asks=True` change something outside the Mac or under
the user's name; the app confirms each call by voice whoever made it (room:
brain.ALWAYS_ASK_TOOLS; other sessions: Assistant.run_shared_tool), and a
"yes, always" never applies to them.

Outputs are neutral, readable facts with exact values (ISO times, message ids);
how to say them out loud is the mail-calendar skill's and the room prompt's job."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import calendar_mac, mail_mac


@dataclass(frozen=True)
class SharedTool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]
    asks: bool = False  # confirmed by voice on every call


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_STR, _INT, _BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
_ADDRESSES = {"type": "array", "items": {"type": "string"}}
_COMPOSE = _obj({"to": _ADDRESSES, "cc": _ADDRESSES, "subject": _STR, "body": _STR}, ["to"])

SHARED: tuple[SharedTool, ...] = (
    SharedTool(
        "calendar_events",
        "Read events from the Mac's calendars (Google, iCloud and others synced to this Mac). "
        "start/end are ISO 8601 local times or dates, e.g. 2026-10-08 or 2026-10-08T15:00; "
        "default is today. Optional query filters by title, location or notes; optional "
        "calendar limits to one calendar by name.",
        _obj({"start": _STR, "end": _STR, "query": _STR, "calendar": _STR}),
        lambda a: calendar_mac.events(a.get("start"), a.get("end"), a.get("query"), a.get("calendar")),
    ),
    SharedTool(
        "calendar_list",
        "List the Mac's calendars and which can be written to.",
        _obj({}),
        lambda a: calendar_mac.calendars(),
    ),
    SharedTool(
        "calendar_create_event",
        "Add an event to a calendar (the user confirms by voice). start/end are ISO 8601 "
        "local times; end defaults to one hour after start. all_day=true for an all-day "
        "event (start is a date). calendar is a name from calendar_list; default is the "
        "Mac's default calendar.",
        _obj(
            {
                "title": _STR,
                "start": _STR,
                "end": _STR,
                "all_day": _BOOL,
                "calendar": _STR,
                "location": _STR,
                "notes": _STR,
            },
            ["title", "start"],
        ),
        calendar_mac.create_event,
        asks=True,
    ),
    SharedTool(
        "mail_recent",
        "List the newest messages in Mail's inbox (all accounts): id, received time, sender, "
        "subject. count 1-50 (default 10); unread_only=true for unread only.",
        _obj({"count": _INT, "unread_only": _BOOL}),
        lambda a: mail_mac.recent(a.get("count"), a.get("unread_only", False)),
    ),
    SharedTool(
        "mail_search",
        "Find recent inbox messages whose subject or sender contains query (searches the newest 300).",
        _obj({"query": _STR, "count": _INT}, ["query"]),
        lambda a: mail_mac.search(a.get("query"), a.get("count")),
    ),
    SharedTool(
        "mail_read",
        "Read one inbox message by its id (from mail_recent or mail_search).",
        _obj({"id": _INT}, ["id"]),
        lambda a: mail_mac.read(a.get("id")),
    ),
    SharedTool(
        "mail_draft",
        "Create an email draft in Mail and open it for the user to review. Sends nothing. "
        "to and cc are lists of email addresses.",
        _COMPOSE,
        mail_mac.draft,
    ),
    SharedTool(
        "mail_send",
        "Send an email from Mail (the user confirms by voice). to and cc are lists of email "
        "addresses. Prefer mail_draft unless the user clearly asked to send.",
        _COMPOSE,
        mail_mac.send,
        asks=True,
    ),
)

BY_NAME = {t.name: t for t in SHARED}
