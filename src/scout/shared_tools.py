"""Mail, calendar, reminders and messages tools, defined once and served two ways:
- to the room assistant, inside its in-process voice_app tool server;
- to any other Claude session, through the MCP server (mcp_server.py), which
  forwards each call to the running app (POST /api/tool).

The app always does the work, so macOS's calendar and Mail permissions belong
to it alone (Messages: to its scout-messages helper). Tools with `asks=True` change something outside the Mac or under
the user's name; the app confirms each call by voice whoever made it (room:
brain.ALWAYS_ASK_TOOLS; other sessions: Assistant.run_shared_tool), and a
"yes, always" never applies to them.

Outputs are neutral, readable facts with exact values (ISO times, message ids);
how to say them out loud is the mail-calendar skill's and the room prompt's job.
Whoever calls, it's Claude: what the reads return (email summaries or text,
calendar notes) is decided by the privacy mode (privacy.py)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import calendar_mac, freebusy, mail_mac, messages_mac, privacy, reminders_mac


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
        "calendar limits to one calendar by name. Event notes and attendees are included only "
        "if the user's privacy mode allows.",
        _obj({"start": _STR, "end": _STR, "query": _STR, "calendar": _STR}),
        lambda a: privacy.current().calendar_events(
            a.get("start"), a.get("end"), a.get("query"), a.get("calendar")
        ),
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
        "calendar_free",
        "Free time in working hours (config [calendar] work_start/work_end, default 9:00 to "
        "17:00), worked out exactly from the calendar: for each day from start to end, the "
        "free gaps and the busy blocks. start/end are ISO 8601 local dates or times (default "
        "today; at most 31 days); today's free time starts from now. min_minutes (default 30) "
        "drops shorter gaps. All-day events and events shown as free don't block time.",
        _obj({"start": _STR, "end": _STR, "min_minutes": _INT}),
        lambda a: freebusy.free(a.get("start"), a.get("end"), a.get("min_minutes", 30)),
    ),
    SharedTool(
        "reminders_list",
        "Read open reminders from the Mac's Reminders (iCloud and other synced lists), due ones "
        'first: title, list, due time, id. list limits to one list by name ("shopping" finds '
        "Shopping); include_completed=true adds finished ones; due_before (ISO 8601) keeps only "
        "those due before it, e.g. tomorrow's date for everything due today or overdue.",
        _obj({"list": _STR, "include_completed": _BOOL, "due_before": _STR}),
        lambda a: reminders_mac.reminders(
            a.get("list"), a.get("include_completed", False), a.get("due_before")
        ),
    ),
    SharedTool(
        "reminder_lists",
        "List the Mac's reminder lists, which is the default, and which can be written to.",
        _obj({}),
        lambda a: reminders_mac.lists(),
    ),
    SharedTool(
        "reminder_add",
        "Add a reminder (the user confirms by voice). list is a name from reminder_lists "
        "(default: the default list). due is an ISO 8601 local date (due that day) or time "
        "(due then, with an alert).",
        _obj({"title": _STR, "list": _STR, "due": _STR, "notes": _STR}, ["title"]),
        reminders_mac.add,
        asks=True,
    ),
    SharedTool(
        "reminder_complete",
        "Mark a reminder done (the user confirms by voice). id and title both come from "
        "reminders_list; the title must match the id's reminder, so a stale id can't finish "
        "the wrong one.",
        _obj({"id": _STR, "title": _STR}, ["id", "title"]),
        reminders_mac.complete,
        asks=True,
    ),
    SharedTool(
        "mail_recent",
        "List the newest messages in Mail's inbox (all accounts, last 30 days, newest first): "
        "id, received time, sender, subject, and (privacy mode permitting) a one-line gist "
        "written by Scout's local model. count 1-50 (default 10); unread_only=true for unread only.",
        _obj({"count": _INT, "unread_only": _BOOL}),
        lambda a: privacy.current().mail_list(a.get("count"), a.get("unread_only", False)),
    ),
    SharedTool(
        "mail_search",
        "Find recent inbox messages whose subject or sender contains query (last 180 days, newest "
        "first); same fields as mail_recent.",
        _obj({"query": _STR, "count": _INT}, ["query"]),
        lambda a: privacy.current().mail_list(a.get("count"), False, a.get("query") or ""),
    ),
    SharedTool(
        "mail_read",
        "Read one inbox message by its id (from mail_recent or mail_search): sender, recipients, "
        "subject and, privacy mode permitting, a few-sentence summary by Scout's local model "
        "(the full text stays on the Mac). Enough to answer what an email says.",
        _obj({"id": _INT}, ["id"]),
        lambda a: privacy.current().mail_read(a.get("id")),
    ),
    SharedTool(
        "mail_read_full",
        "The exact text of one inbox message by id. Use only when the task needs its exact "
        "wording (quoting it in a reply, copying a detail the summary left out); mail_read "
        "answers what it says. Refused in strict privacy mode.",
        _obj({"id": _INT}, ["id"]),
        lambda a: privacy.current().mail_read_full(a.get("id")),
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
    SharedTool(
        "messages_recent",
        "Read the newest iMessage and SMS messages across all conversations (newest first, sent "
        "and received): time, who, conversation, text. count 1-50 (default 10). Read-only.",
        _obj({"count": _INT}),
        lambda a: messages_mac.recent(a.get("count"), view=privacy.current().messages_view),
    ),
    SharedTool(
        "messages_from",
        "Read the texts one person sent the user (newest first). contact is a name from Contacts, "
        "a phone number or an email address. count 1-50 (default 10); days back 1-365 (default 30).",
        _obj({"contact": _STR, "count": _INT, "days": _INT}, ["contact"]),
        lambda a: messages_mac.from_contact(
            a.get("contact"), a.get("count"), a.get("days"), view=privacy.current().messages_view
        ),
    ),
    SharedTool(
        "messages_unread",
        "List unread incoming iMessage and SMS messages (newest first). count 1-50 (default 20); "
        "days back 1-365 (default 30).",
        _obj({"count": _INT, "days": _INT}),
        lambda a: messages_mac.unread(a.get("count"), a.get("days"), view=privacy.current().messages_view),
    ),
    SharedTool(
        "messages_search",
        "Find iMessage and SMS messages whose text contains text (newest first, sent and "
        "received). count 1-50 (default 10); days back 1-365 (default 90).",
        _obj({"text": _STR, "count": _INT, "days": _INT}, ["text"]),
        lambda a: messages_mac.search(
            a.get("text"), a.get("count"), a.get("days"), view=privacy.current().messages_view
        ),
    ),
)

SHARED = (
    *SHARED,
    SharedTool(
        "messages_chat",
        "Read one iMessage or SMS conversation, both sides, newest first: chat is a person's name, "
        "number or email, or a group's name. count 1-50 (default 20); days back 1-365 (default 30).",
        _obj({"chat": _STR, "count": _INT, "days": _INT}, ["chat"]),
        lambda a: messages_mac.chat(
            a.get("chat"), a.get("count"), a.get("days"), view=privacy.current().messages_view
        ),
    ),
)

BY_NAME = {t.name: t for t in SHARED}
