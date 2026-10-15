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

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import (
    calendar_mac,
    freebusy,
    index,
    mail_mac,
    maps,
    messages_mac,
    notes,
    privacy,
    reference,
    refs,
    reminders_mac,
    starlink,
    tasks,
    weather,
)
from .mac import ToolError


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

SHARED = (
    *SHARED,
    SharedTool(
        "weather",
        "Current weather, the next 24 hours and 7 days for a place (free Open-Meteo forecast, "
        "no sign-in). place like 'Luther, Michigan' or 'Paris, France'; empty = the user's home "
        "(weather.home). Exact values in °F and mph.",
        _obj({"place": _STR}),
        lambda a: weather.report_for(a.get("place")),
    ),
    SharedTool(
        "local_search",
        "Search Scout's local index on this Mac (memories the user asked Scout to keep, past "
        "conversations with Scout, notes, lists, the inbox, meetings and Slack) by words and "
        "by meaning. Use it for 'what did I say about...', 'did we talk about...', 'where did I "
        "note...', 'what did we decide in the meeting about...'. kinds: any of memory, turn (past "
        "conversations), note, list, mail, meeting (Granola notes and transcripts), slack (Slack messages and threads) (empty = all); days: only the last N "
        "days (0 = any time); limit 1-20 (default 12). Returns the best matches, best first: the right one is often not the first, so read them all.",
        _obj(
            {"query": _STR, "kinds": {"type": "array", "items": _STR}, "days": _INT, "limit": _INT}, ["query"]
        ),
        lambda a: _local_search(a),
    ),
    SharedTool(
        "note_add",
        "Save a note in Scout (searchable later; also copied to Apple Notes' Scout folder when "
        "allowed). text is the note; title is optional (default: its first words).",
        _obj({"text": _STR, "title": _STR}, ["text"]),
        notes.add_text,
    ),
    SharedTool(
        "notes_find",
        "Find the user's notes by words or meaning, best first, with their full text and ids. "
        "Empty query = the newest notes. limit 1-20 (default 5).",
        _obj({"query": _STR, "limit": _INT}),
        notes.find_text,
    ),
    SharedTool(
        "tasks_list",
        "The user's open tasks, in priority order, each with its number, due date and why it's "
        "there. who: mine (what the user owes; default all), waiting (what others owe the user) "
        "or all. query narrows by words and meaning. limit 1-50 (default 10). For 'what should I "
        "work on', 'what's on my list', 'what am I waiting on'.",
        _obj({"who": _STR, "query": _STR, "limit": _INT}),
        lambda a: tasks.list_text(a),
    ),
    SharedTool(
        "task_add",
        "Add a task to the user's list. title says what to do. owner: 'me' (default) or the "
        "person who owes it (a 'waiting on' item). due: YYYY-MM-DD. importance: high, normal or "
        "low; urgency: urgent or normal; effort: small, medium or large. context: why, in a "
        "sentence or two.",
        _obj(
            {
                "title": _STR,
                "owner": _STR,
                "due": _STR,
                "importance": _STR,
                "urgency": _STR,
                "effort": _STR,
                "context": _STR,
            },
            ["title"],
        ),
        lambda a: tasks.add_text(a),
    ),
    SharedTool(
        "task_done",
        "Mark a task done by its number from tasks_list (drop: true when it's no longer needed "
        "rather than done). Its reminder is marked done too.",
        _obj({"id": _INT, "drop": _BOOL}, ["id"]),
        lambda a: tasks.done(a),
    ),
    SharedTool(
        "task_update",
        "Change a task by its number: title, owner, due (YYYY-MM-DD), importance, urgency, "
        "effort, pin (top, high, low or none: overrides the order), or context to add.",
        _obj(
            {
                "id": _INT,
                "title": _STR,
                "owner": _STR,
                "due": _STR,
                "importance": _STR,
                "urgency": _STR,
                "effort": _STR,
                "pin": _STR,
                "context": _STR,
            },
            ["id"],
        ),
        lambda a: tasks.update(a),
    ),
    SharedTool(
        "task_why",
        "Where a task came from (the meeting or email), when it was found, and its context.",
        _obj({"id": _INT}, ["id"]),
        lambda a: tasks.why_text(a),
    ),
    SharedTool(
        "note_delete",
        "Delete a note by its id from notes_find (the user confirms by voice).",
        _obj({"id": _INT}, ["id"]),
        notes.delete,
        asks=True,
    ),
    SharedTool(
        "places_near",
        "Find places near a town or address from OpenStreetMap (free, no sign-in), closest by "
        "driving time first: name, address, miles, minutes, hours, phone. what is a kind of place "
        "(dump station, water, campground, rv park, gas, diesel, truck stop, grocery, hardware, "
        "laundromat, pharmacy, hospital, urgent care, vet, restaurant, coffee, rest area, post "
        "office, library, atm, dog park) or a name (Walmart). near is a place like 'Cadillac, "
        "Michigan'. radius_miles 1-60 (default 25). Drive times are for a car.",
        _obj({"what": _STR, "near": _STR, "radius_miles": _INT, "limit": _INT}, ["what", "near"]),
        maps.near_text,
    ),
    SharedTool(
        "drive_route",
        "Driving distance and time between two places (OpenStreetMap routing for a car; it does "
        "not know RV height or weight limits). origin and destination are towns or addresses.",
        _obj({"origin": _STR, "destination": _STR}, ["origin", "destination"]),
        maps.route_text,
    ),
    SharedTool(
        "starlink",
        "The Starlink dish's status from its local API: online or the outage cause, latency, "
        "dropped pings (now and 15-minute average), current traffic, obstruction, alerts, "
        "bandwidth limits, dropouts in the last hour, data used today and this month. "
        "Read-only; works only when [starlink] enabled = true.",
        _obj({}),
        lambda a: starlink.report_now(),
    ),
    SharedTool(
        "open_link",
        "What a record Scout spoke about is and where it came from, with its link: a task's "
        "source meeting, email or Slack message, or the record's page on Scout's web UI. ref is "
        "a record id (task:15, granola:..., mail:..., note:3; shown as refs in the request "
        "context and in local_search results); or about: 'that' (what was just said) or a few "
        "words about it. Read-only. The link isn't for reading aloud: send_link puts it on the "
        "user's phone, open_url opens it on the Mac.",
        _obj({"ref": _STR, "about": _STR}),
        refs.open_link,
    ),
    SharedTool(
        "send_link",
        "Put a record's link on the user's phone: the task's own reminder already has it, else "
        "a reminder in their Scout list titled 'Link: ...' with the URL. ref or about as for "
        "open_link. Needs no confirmation (Scout's own list).",
        _obj({"ref": _STR, "about": _STR}),
        refs.send_link,
    ),
    SharedTool(
        "reference_search",
        "Search the offline reference on this Mac (Kiwix archives such as Wikipedia, WikiMed and "
        "iFixit; works with no internet) by words, best first: each article's title, archive, first "
        "lines and ref. Use it for 'look up X' and general-knowledge questions, and whenever the web "
        "is slow or down. book limits it to one archive (its name, e.g. wikipedia_en_all_nopic); "
        "limit 1-10 (default 5). reference_read reads an article; open_link and send_link take its ref.",
        _obj({"query": _STR, "book": _STR, "limit": _INT}, ["query"]),
        reference.search_text,
    ),
    SharedTool(
        "reference_read",
        "Read an article of the offline reference by its ref (zim:..., from reference_search): its "
        "title, link, list of sections and introduction. section names one section to read instead "
        "(with its subsections); query picks the sections that best match a few words. max_chars "
        "500-12000 (default 4000).",
        _obj({"ref": _STR, "section": _STR, "query": _STR, "max_chars": _INT}, ["ref"]),
        reference.read_text,
    ),
)


async def _local_search(a: dict[str, Any]) -> str:
    query = str(a.get("query") or "").strip()
    if not query or len(query) > 300:
        raise ToolError("query must be 1-300 characters")
    kinds = [str(k) for k in a.get("kinds") or [] if str(k)] or None
    days = int(a.get("days") or 0)
    limit = max(1, min(int(a.get("limit") or 12), 20))
    since = time.time() - days * 86400 if days > 0 else None
    hits = await index.default().asearch(query, kinds=kinds, since=since, limit=limit)
    pol = privacy.current()
    if pol.mode == "strict":  # a hit on mail would say its text holds the words: none at all
        hits = [h for h in hits if h.kind not in _PRIVATE_KINDS]
    if pol.mode != "open":  # mail, meeting and Slack text, and answers built from them: titles only
        for h in hits:
            if h.meta.get("private") or h.kind in _PRIVATE_KINDS:
                h.body = ""
            elif h.kind == "task":  # a task found in mail or a meeting: what came from there
                h.title, h.body = tasks.shown(h.meta, h.title, h.body, pol.mode)
    # Each with its record id, for open_link / send_link ("send me the link to that meeting").
    return "\n".join(f"{index.format_hits([h])} (ref {refs.norm(h.id)})" for h in hits) or index.format_hits(
        []
    )


_PRIVATE_KINDS = ("mail", "meeting", "slack")  # their text stays on the Mac outside "open" privacy mode


BY_NAME = {t.name: t for t in SHARED}
