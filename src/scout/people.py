"""One person across Scout's sources (docs/contacts.md, People; docs/links.md, same_person).

A person is a contact: the record `person:<contact id>` (kind `person`) holds who they
are elsewhere, found by email address:

- the email addresses of theirs that Scout has seen: mail senders, meeting attendees
  (Granola's attendee list), their Slack profile's email;
- their Slack user (users.list: the profile email matches one of their addresses), by
  the name Slack messages are stored under;
- the names they go by (the contact's, Slack's).

The app builds the people (`refresh`, every PASS_EVERY_S): it asks the store which
addresses it has seen, Contacts which contacts have them (the helper's `resolve`; never
asks for the permission) and Slack who has them, and hands the list to the store. The
store links each person `same_person` to the records that are theirs under those
identities (`matches`: mail from them, Slack messages they wrote, meetings they were in;
the newest LINKS_PER_SOURCE of each) and keeps those links current in its upkeep.

Uses: local_search's `person` ("what did Pat say about the launch"), tasks_list's
`owner` ("what does Pat owe me", for any of the names Pat goes by), and the record
pages (a person's identities; "Same person" on what's theirs)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

log = logging.getLogger(__name__)

KIND = "person"
PASS_EVERY_S = 6 * 3600.0
FIRST_PASS_S = 120.0  # after the app starts
MAX_EMAILS = 400  # addresses looked up in Contacts a pass (two calls to the helper)
LINKS_PER_SOURCE = 200  # newest records of each source linked to a person
SOURCES = ("mail", "meeting", "slack")
_EMAIL = re.compile(r"[^\s<>@\"',;]+@[^\s<>@\"',;]+\.[^\s<>@\"',;]+")


def person_id(contact_id: str) -> str:
    return f"person:{contact_id}"


def norm_email(s: str) -> str:
    return s.strip().strip("<>").lower()


def _fold(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def sender_email(who: str) -> str:
    from .mailindex import sender_address

    return sender_address(who)


def attendees(meta: dict[str, Any]) -> list[dict[str, str]]:
    return [a for a in meta.get("attendees") or [] if isinstance(a, dict)]


def emails_in(kind: str, who: str, meta: dict[str, Any]) -> list[str]:
    """The email addresses a record shows for the people in it: a mail's sender, a
    meeting's attendees."""
    if kind == "mail":
        return [e] if (e := sender_email(who)) else []
    if kind == "meeting":
        return [norm_email(str(a["email"])) for a in attendees(meta) if a.get("email")]
    return []


def slack_shown(u: dict[str, Any]) -> str:
    """The name a Slack user's messages are stored under (slack.Names)."""
    p = u.get("profile") or {}
    return str(p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or "")


def build(
    found: dict[str, dict[str, Any]], slack_users: Sequence[dict[str, Any]] = ()
) -> list[dict[str, Any]]:
    """The people: `found` is Contacts' answer for each address ({email: {id, name}}),
    `slack_users` Slack's users.list members. A Slack user joins the contact their
    profile email belongs to; a name two Slack users share isn't used to match."""
    people: dict[str, dict[str, Any]] = {}
    for email, c in sorted(found.items()):
        if not c.get("id"):
            continue
        p = people.setdefault(
            str(c["id"]),
            {"contact": str(c["id"]), "name": str(c.get("name") or ""), "emails": [], "slack": []},
        )
        if (e := norm_email(email)) not in p["emails"]:
            p["emails"].append(e)
    shown_by = [_fold(slack_shown(u)) for u in slack_users]
    by_email = {e: p for p in people.values() for e in p["emails"]}
    for u in slack_users:
        email = norm_email(str((u.get("profile") or {}).get("email") or ""))
        owner = by_email.get(email) if email else None
        if owner is None or u.get("deleted") or u.get("is_bot"):
            continue
        shown = slack_shown(u)
        prof = u.get("profile") or {}
        owner["slack"].append({
            "id": str(u.get("id") or ""),
            "shown": shown if shown and shown_by.count(_fold(shown)) == 1 else "",
            "real": str(prof.get("real_name") or u.get("real_name") or ""),
        })  # fmt: skip
    return sorted(people.values(), key=lambda p: p["contact"])


def names(p: dict[str, Any]) -> list[str]:
    """Every name a person goes by: the contact's, then Slack's."""
    out: list[str] = []
    for n in [p.get("name"), *(x for s in p.get("slack") or [] for x in (s.get("real"), s.get("shown")))]:
        if n and _fold(n) not in (_fold(o) for o in out):
            out.append(str(n))
    return out


def record(p: dict[str, Any]) -> dict[str, Any]:
    """The store item for a person (kind person; no ts: it keeps the one it has)."""
    known = names(p)
    bits = [f"{len(p['emails'])} email address{'es' * (len(p['emails']) != 1)}"]
    bits += [f"Slack {s['shown'] or s['real'] or s['id']}" for s in p.get("slack") or []]
    return {
        "id": person_id(p["contact"]),
        "kind": KIND,
        "title": str(p.get("name") or (known[0] if known else p["contact"])),
        "body": f"{p.get('name') or 'A contact'}: " + "; ".join(bits),
        "who": ", ".join(known)[:300],
        "meta": {"contact": p["contact"], "emails": list(p["emails"]), "slack": list(p.get("slack") or []), "names": known},
    }  # fmt: skip


def matches(meta: dict[str, Any], kind: str, who: str, record_meta: dict[str, Any]) -> bool:
    """Is this record (mail, meeting or Slack; not a part) one of the person's?"""
    emails = set(meta.get("emails") or [])
    if kind == "mail":
        return sender_email(who) in emails
    if kind == "meeting":
        listed = attendees(record_meta)
        if any(norm_email(str(a.get("email") or "")) in emails for a in listed):
            return True
        if any(a.get("email") for a in listed):
            return False  # their addresses are known, and none is theirs
        full = {_fold(n) for n in meta.get("names") or [] if len(str(n).split()) > 1}
        return any(_fold(n) in full for n in who.split(","))
    if kind == "slack":
        shown = {_fold(s["shown"]) for s in meta.get("slack") or [] if s.get("shown")}
        return any(_fold(n) in shown for n in who.split(","))
    return False


def prefilter(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind, needle) pairs: a record whose `who` or `meta` contains the needle may be
    the person's (matches decides). Keeps the store's queries to the few candidates."""
    out = [("mail", e) for e in meta.get("emails") or []]
    out += [("meeting", e) for e in meta.get("emails") or []]
    out += [("meeting", n) for n in meta.get("names") or [] if len(str(n).split()) > 1]
    out += [("slack", s["shown"]) for s in meta.get("slack") or [] if s.get("shown")]
    return out


def candidate_emails(rows: Iterable[tuple[str, str, str]], limit: int = MAX_EMAILS) -> list[str]:
    """The addresses in (kind, who, meta json) rows, newest first, each once."""
    out: dict[str, None] = {}
    for kind, who, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            m = {}
        for e in emails_in(kind, who, m if isinstance(m, dict) else {}):
            if _EMAIL.fullmatch(e):
                out.setdefault(e, None)
                if len(out) >= limit:
                    return list(out)
    return list(out)


# --- the app's pass ---------------------------------------------------------------------------


async def _slack_users() -> list[dict[str, Any]] | None:
    """Slack's members with their profiles, or None when Slack can't be read (not set up)."""
    from . import slack_send

    try:
        get = slack_send._fetch()
        people, _ = await asyncio.to_thread(slack_send.directory, get)
    except Exception as exc:  # no token, offline, refused: no Slack this pass
        log.info("people: no Slack users this pass (%s)", type(exc).__name__)
        return None
    return list(people)


async def _resolve(emails: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Contacts' {email: {id, name}} for the addresses it has; {} without the permission
    (never asks for it)."""
    from . import contacts_mac
    from .mac import ToolError

    h = contacts_mac.HELPER
    if not emails or not h.binary.exists() or not await contacts_mac._granted(h):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for i in range(0, len(emails), 200):
        args = [a for e in emails[i : i + 200] for a in ("--handle", e)]
        try:
            got = (await contacts_mac.run("resolve", *args, helper=h)).get("names") or {}
        except ToolError as exc:
            log.info("people: Contacts didn't answer (%s)", exc)
            return found
        found.update({norm_email(k): v for k, v in got.items() if isinstance(v, dict)})
    return found


async def refresh(store: Any = None) -> dict[str, int]:
    """One pass: the people from what the store has seen, Contacts and Slack, into the
    store. Counts only in the answer and the log."""
    from . import index
    from .config import load

    if not load().contacts.enabled:
        return {}
    store = store or index.default()
    users = await _slack_users()
    seen = await asyncio.to_thread(store.people_candidates, MAX_EMAILS)
    emails = list(
        dict.fromkeys(
            [*seen, *(norm_email(str((u.get("profile") or {}).get("email") or "")) for u in users or [])]
        )
    )
    emails = [e for e in emails if e][: MAX_EMAILS * 2]
    found = await _resolve(emails)
    if not found:
        return {"emails": len(emails), "people": 0}
    people = build(found, users or [])
    # Without Slack this pass, people only Slack found are kept as they are.
    got = await asyncio.to_thread(store.put_people, people, users is not None)
    log.info(
        "people: %d addresses, %d people, %d same_person links", len(emails), len(people), got.get("links", 0)
    )
    return {"emails": len(emails), "people": len(people), **got}


async def loop(first_s: float = FIRST_PASS_S, every_s: float = PASS_EVERY_S) -> None:
    await asyncio.sleep(first_s)
    while True:
        try:
            await refresh()
        except Exception:
            log.exception("people: the pass failed; trying again later")
        await asyncio.sleep(every_s)


# --- using it ---------------------------------------------------------------------------------


def owner_names(contact: dict[str, Any], record: Any) -> tuple[set[str], set[str]]:
    """The names a task's owner may be written as for this person: (the names they go
    by in full: the contact's, their nickname, Slack's; the first names of those)."""
    full = [str(contact.get("name") or ""), str(contact.get("nickname") or "")]
    if record is not None:
        full += [str(n) for n in record.meta.get("names") or []]
    whole = {_fold(n) for n in full if n.strip()}
    return whole, {_fold(n).split()[0] for n in full if len(n.split()) > 1}


async def owed_by(owner: str, tasks: Sequence[Any], store: Any) -> list[Any] | str:
    """The tasks whose owner is this person (tasks_list's `owner`): written as any name
    they go by. An owner written as a bare first name another contact shares counts
    when the task was found in a record of theirs (same_person). Without Contacts, the
    owner as written. A str: which person to ask about."""
    from . import contacts_mac
    from .mac import ToolError

    try:
        p = await contacts_mac.one_person(owner, "tasks_list")
    except ToolError:
        want = _fold(owner)
        return [
            t
            for t in tasks
            if _fold(t.owner) == want or _fold(t.owner).split()[:1] == want.split()[:1] == [want]
        ]
    if isinstance(p, str):
        return p
    pid = person_id(str(p.get("id")))
    rec = await asyncio.to_thread(store.get, pid)
    whole, firsts = owner_names(p, rec)
    theirs: set[str] | None = None
    shared: dict[str, bool] = {}  # a first name: does it mean only this person?
    out = []
    for t in tasks:
        o = _fold(t.owner)
        if o in whole:
            out.append(t)
            continue
        if o not in firsts:
            continue
        if o not in shared:
            try:
                others = await contacts_mac.person_named(o)
            except ToolError:
                others = [p]
            shared[o] = [x.get("id") for x in others] == [p.get("id")]
        if shared[o]:
            out.append(t)
            continue
        if theirs is None:
            theirs = {
                ln["id"] for ln in await asyncio.to_thread(store.links, pid) if ln["rel"] == "same_person"
            }
        srcs = {
            ln["id"] for ln in await asyncio.to_thread(store.links, f"task:{t.n}") if ln["rel"] == "found_in"
        }
        if srcs & theirs:
            out.append(t)
    return out
