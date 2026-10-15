"""The Mac's Contacts (iCloud, Google, Exchange: whatever is synced to this Mac), read
through the scout-contacts helper (native/contacts, the Contacts framework). The
helper holds its own Contacts permission, asked for once (a macOS prompt naming
"scout-contacts"); nothing here can change a contact, and a contact's notes are never
read.

Used for:
- the contacts_search / contact_get tools (what Claude sees is privacy.py's call);
- tier 0's "what's Pat's number", "when is Sam's birthday" (local_intents.py);
- names instead of raw numbers and addresses in Messages and Mail listings
  (`names_for`), never asking for the permission on their own;
- a name where an email address or phone number is wanted (`addresses_for`,
  `handles_for`): mail drafts to "Pat", the texts from Pat.

Contact details are the user's private data: what Claude gets is decided by the
privacy mode (privacy.Policy.contacts_view)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mac
from .config import DATA, load
from .mac import ToolError

BINARY = DATA / "bin" / "scout-contacts"  # scripts/build-contacts.sh
_ACCESS_ERRORS = ("access not granted", "access is ")  # native/contacts' refusals
_GRANTED_S, _DENIED_S = 300.0, 60.0
NAMES_TTL_S = 600.0  # a handle's name is remembered this long
SETTINGS_HINT = "System Settings, Privacy and Security, Contacts"


@dataclass
class Helper:
    """Where the helper is; tests add ["--fixture", FILE] to `args` (never the real store)."""

    binary: Path = BINARY
    args: list[str] = field(default_factory=list)
    timeout_s: float = 15.0
    _access: tuple[float, str] = (0.0, "")  # (good until, "" or the refusal)
    _names: dict[str, tuple[float, str]] = field(default_factory=dict)  # handle -> (at, name or "")


HELPER = Helper()


async def run(*args: str, helper: Helper | None = None, timeout: float | None = None) -> dict[str, Any]:
    h = helper or HELPER
    if not h.binary.exists():
        raise ToolError("The contacts helper isn't built; ask the owner to run scripts/build-contacts.sh.")
    proc = await asyncio.create_subprocess_exec(
        str(h.binary), *h.args, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout or h.timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError("the contacts helper timed out") from None
    try:
        data: dict[str, Any] = json.loads(out.decode(errors="replace") or "{}")
    except json.JSONDecodeError:
        raise ToolError(err.decode(errors="replace").strip()[:300] or "the contacts helper failed") from None
    if data.get("error") or proc.returncode != 0:
        msg = str(data.get("error") or "the contacts helper failed")
        if args and args[0] not in ("status", "request") and any(e in msg for e in _ACCESS_ERRORS):
            h._access = (0.0, "")  # access was taken away: ask again next time
        raise ToolError(msg)
    return data


def _enabled() -> None:
    if not load().contacts.enabled:
        raise ToolError("Contacts are turned off (contacts.enabled = false in config.toml).")


async def ensure_access(helper: Helper | None = None) -> None:
    """Ask macOS for Contacts access the first time (a prompt on the Mac's screen),
    remembered for a while (granted 5 min, refused 1 min)."""
    h = helper or HELPER
    until, refusal = h._access
    if time.monotonic() < until:
        if refusal:
            raise ToolError(refusal)
        return
    try:
        await _ask(h)
    except ToolError as exc:
        h._access = (time.monotonic() + _DENIED_S, str(exc))
        raise
    h._access = (time.monotonic() + _GRANTED_S, "")


async def _ask(h: Helper) -> None:
    status = (await run("status", helper=h)).get("status")
    if status in ("authorized", "limited", "fixture"):
        return
    if status == "not_determined":
        res = await run("request", helper=h, timeout=120)
        if res.get("granted"):
            return
        raise ToolError(
            "Contacts access wasn't allowed. A prompt appears on the Mac's screen the first time; "
            f"it can also be turned on in {SETTINGS_HINT}."
        )
    raise ToolError(f"Contacts access is {status}. Turn it on in {SETTINGS_HINT}.")


async def _granted(h: Helper) -> bool:
    """Is access already granted? Never asks (for names in other tools' answers)."""
    until, refusal = h._access
    if time.monotonic() < until:
        return not refusal
    try:
        status = (await run("status", helper=h)).get("status")
    except ToolError:
        return False
    return status in ("authorized", "limited", "fixture")


# --- lookups ---------------------------------------------------------------------------


def _query(value: Any, what: str = "query", required: bool = True) -> str:
    s = " ".join(str(value or "").split())
    s = re.sub(r"(?:'s|\u2019s|'|\u2019)$", "", s)  # "Pat's" -> "Pat", "Chris'" -> "Chris"
    if not s and required:
        raise ToolError(f"{what} is required")
    if len(s) > 100:
        raise ToolError(f"{what} is too long (max 100 characters)")
    if any(ord(c) < 32 for c in s):
        raise ToolError(f"{what} has control characters")
    return s


async def search_data(
    query: Any, limit: Any = None, group: Any = None, helper: Helper | None = None
) -> dict[str, Any]:
    """{"contacts": [person, ...best first], "total": n}; each person as `get` returns it,
    plus "match" (name, nickname, organization, email, phone, similar or group)."""
    _enabled()
    q = _query(query, required=False)
    g = _query(group, "group", required=False)
    if not q and not g:
        raise ToolError("say who to look for (a name, email address or phone number), or a group")
    n = mac.check_int(limit if limit is not None else 10, 1, 25, "limit")
    await ensure_access(helper)
    args = ["search", "--limit", str(n)]
    if q:
        args += ["--query", q]
    if g:
        args += ["--group", g]
    return await run(*args, helper=helper)


async def contact_data(contact_id: Any, helper: Helper | None = None) -> dict[str, Any]:
    _enabled()
    cid = str(contact_id or "").strip()
    if not cid or len(cid) > 200 or any(ord(c) < 32 for c in cid):
        raise ToolError("id must be a contact id from contacts_search")
    await ensure_access(helper)
    person: dict[str, Any] = (await run("get", "--id", cid, helper=helper))["contact"]
    return person


async def groups_data(helper: Helper | None = None) -> list[dict[str, Any]]:
    _enabled()
    await ensure_access(helper)
    return list((await run("groups", helper=helper)).get("groups") or [])


async def names_for(handles: list[str], helper: Helper | None = None) -> dict[str, str]:
    """Phone numbers and email addresses to contact names, for the ones Contacts knows.
    Quiet: {} when Contacts is off, not built or not allowed yet (it never asks)."""
    h = helper or HELPER
    wanted = sorted({x for x in handles if x})
    if not wanted:
        return {}
    try:
        _enabled()
    except ToolError:
        return {}
    now = time.monotonic()
    out = {
        x: name
        for x in wanted
        if (got := h._names.get(x)) and now - got[0] < NAMES_TTL_S and (name := got[1])
    }
    todo = [x for x in wanted if not ((got := h._names.get(x)) and now - got[0] < NAMES_TTL_S)]
    if not todo or not h.binary.exists() or not await _granted(h):
        return out
    try:
        args = [a for x in todo[:200] for a in ("--handle", x)]
        found = (await run("resolve", *args, helper=h)).get("names") or {}
    except ToolError:
        return out
    for x in todo[:200]:
        name = str((found.get(x) or {}).get("name") or "")
        h._names[x] = (now, name)
        if name:
            out[x] = name
    return out


class Unclear(ToolError):
    """Contacts answered, but a name doesn't give one address: nobody, several, none on file."""


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONEISH = re.compile(r"^\+?[\d\s().-]{7,}$")


def is_handle(text: str) -> bool:
    """An email address or a phone number, not a name."""
    t = text.strip()
    return bool(_EMAIL.match(t) or _PHONEISH.match(t))


async def person_named(name: str, helper: Helper | None = None) -> list[dict[str, Any]]:
    """The people a spoken name means, best first: one when it's clear (a single match,
    or a whole-name match that beats the rest), else all the candidates (up to 5)."""
    data = await search_data(name, 5, helper=helper)
    people = list(data.get("contacts") or [])
    if len(people) <= 1:
        return people
    q = " ".join(_query(name).lower().split())
    exact = [p for p in people if " ".join(str(p.get("name", "")).lower().split()) == q]
    if len(exact) == 1:
        return exact
    best = min(int(p.get("score", 0)) for p in people)
    top = [p for p in people if int(p.get("score", 0)) == best]
    return top if len(top) == 1 else people


def describe(p: dict[str, Any]) -> str:
    """A person as "Name (Org)" for choices."""
    org = str(p.get("organization") or "")
    return f"{p.get('name')} ({org})" if org and org != p.get("name") else str(p.get("name"))


async def addresses_for(recipients: list[str], helper: Helper | None = None) -> list[str]:
    """Each recipient that is a name, as that person's email address (their first one)
    from Contacts; email addresses pass through. ToolError when a name isn't clear."""
    out = []
    for r in recipients:
        if "@" in r:
            out.append(r)
            continue
        people = await person_named(r, helper)
        if not people:
            raise Unclear(f"{r!r} isn't an email address, and there's no {r} in Contacts")
        if len(people) > 1:
            raise Unclear(
                f"{r!r} matches {len(people)} contacts: "
                + "; ".join(f"{describe(p)}: {_first(p, 'emails') or 'no email'}" for p in people)
                + ". Use the email address of the one the user means."
            )
        email = _first(people[0], "emails")
        if not email:
            raise Unclear(f"{people[0].get('name')} has no email address in Contacts")
        out.append(email)
    return out


def _first(p: dict[str, Any], key: str) -> str:
    items = p.get(key) or []
    return str(items[0].get("value") or "") if items else ""


async def handles_for(name: str, helper: Helper | None = None) -> tuple[str, list[str]]:
    """A person's name and their phone numbers and email addresses (for Messages), when
    the name means one person in Contacts; ("", []) when it doesn't."""
    try:
        people = await person_named(name, helper)
    except ToolError:
        return "", []
    if len(people) != 1:
        return "", []
    p = people[0]
    return str(p.get("name") or name), [
        str(x.get("value")) for x in (p.get("phones") or []) + (p.get("emails") or []) if x.get("value")
    ]


_MOBILE = ("iphone", "mobile", "cell")
_NOT_A_PHONE = ("fax", "pager")


def phone_choices(p: dict[str, Any]) -> list[tuple[str, str]]:
    """A person's phone numbers to call or text, as (number, label), iPhone and mobile
    first; fax and pager numbers left out."""
    phones = [
        (str(x.get("value") or ""), str(x.get("label") or ""))
        for x in p.get("phones") or []
        if x.get("value") and not any(w in str(x.get("label") or "").lower() for w in _NOT_A_PHONE)
    ]
    return sorted(phones, key=lambda nl: not any(w in nl[1].lower() for w in _MOBILE))


def dial_number(number: str) -> str:
    """A phone number as Messages and the phone want it: +<digits> ("+1 (555) 010-2000"
    -> "+15550102000"); a 10-digit number is taken as North American (+1)."""
    digits = re.sub(r"\D", "", number)
    if not digits:
        raise ToolError(f"{number!r} isn't a phone number")
    if number.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


async def one_person(name: str, what: str, helper: Helper | None = None) -> dict[str, Any] | str:
    """The one contact a spoken name means, or (a str) what to ask: the choices when it
    fits several. ToolError when nobody in Contacts has it."""
    people = await person_named(name, helper)
    if not people:
        raise Unclear(f"There's no {name} in Contacts: give a phone number or email address instead.")
    if len(people) > 1:
        return (
            f"More than one contact matches {name}: {'; '.join(describe(p) for p in people)}. "
            f"Ask the user which one, then call {what} again with that full name. Nothing was done."
        )
    return people[0]


# --- saying it -------------------------------------------------------------------------


def birthday_text(b: dict[str, Any] | None) -> str:
    if not b or not b.get("month") or not b.get("day"):
        return ""
    import calendar

    month = calendar.month_name[int(b["month"])]
    return f"{month} {int(b['day'])}" + (f", {int(b['year'])}" if b.get("year") else "")


def address_text(a: dict[str, Any]) -> str:
    parts = [a.get("street"), a.get("city"), " ".join(x for x in (a.get("state"), a.get("postal_code")) if x)]
    parts.append(a.get("country"))
    return ", ".join(" ".join(str(p).split()) for p in parts if p and str(p).strip())


def spoken_number(number: str) -> str:
    """A phone number said in groups of digits: "+1 (555) 010-2000" -> "5 5 5, 0 1 0, 2 0 0 0"."""
    d = re.sub(r"\D", "", number)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        groups = [d[:3], d[3:6], d[6:]]
    elif len(d) == 7:
        groups = [d[:3], d[3:]]
    else:  # elsewhere: groups of three, the last up to four
        groups = []
        while len(d) > 4:
            groups.append(d[:3])
            d = d[3:]
        groups.append(d)
    return ", ".join(" ".join(g) for g in groups if g)


def format_person(p: dict[str, Any], fields: set[str]) -> str:
    """One contact for Claude: the name, and of the rest only `fields` (phones, emails,
    addresses, birthday, groups); organization and title always."""
    head = f"{p.get('name') or '(no name)'}"
    extra = []
    if p.get("nickname") and p.get("nickname") != p.get("name"):
        extra.append(f"nickname {p['nickname']}")
    work = ", ".join(x for x in (p.get("job_title"), p.get("department"), p.get("organization")) if x)
    if work and p.get("organization") != p.get("name"):
        extra.append(work)
    lines = [head + (f" ({'; '.join(extra)})" if extra else "") + f" (id {p.get('id')})"]

    def label(x: dict[str, Any]) -> str:
        return f" ({x['label']})" if x.get("label") else ""

    for key, what in (("phones", "phone"), ("emails", "email")):
        if key in fields:
            lines += [f"  {what}{label(x)}: {x.get('value')}" for x in p.get(key) or []]
    if "addresses" in fields:
        lines += [f"  address{label(a)}: {address_text(a)}" for a in p.get("addresses") or []]
    if "birthday" in fields and (b := birthday_text(p.get("birthday"))):
        lines.append(f"  birthday: {b}")
    if "groups" in fields and p.get("groups"):
        lines.append(f"  groups: {', '.join(p['groups'])}")
    return "\n".join(lines)


def format_people(data: dict[str, Any], what: str, fields: set[str]) -> str:
    people = data.get("contacts") or []
    if not people:
        return f"No contacts match {what}."
    lines = [format_person(p, fields) for p in people]
    total = int(data.get("total") or len(people))
    if total > len(people):
        lines.append(f"(showing {len(people)} of {total}; add a last name or an organization)")
    return "\n".join(lines)


ALL_FIELDS = frozenset({"phones", "emails", "addresses", "birthday", "groups"})
