"""One person across Scout's sources (docs/contacts.md, People; docs/links.md, People).

A person is a record of kind `person`, one per real person, found by stable ids only:

- their Slack user id (U...; from each message's author and @-mentions, and a DM's other
  party), with the names Slack has shown for them over time;
- their email addresses (mail senders and recipients, meeting attendees, their Slack
  profile's email);
- their contact (Contacts' id, found by one of their addresses), and every email address
  and phone number on that card (phones as E.164 where the country can be told), so
  mail from any of them links the moment it's stored;
- their GitHub login (an item's author and assignees).

Every mail, meeting, Slack message and GitHub item is linked to the people in it as it's
stored (`participants`, Index.link_participants), one link per person, record to
person: `with` (a DM's other party, a group DM's members, a meeting's attendees), `from`
(a channel message's author, a mail's sender, an item's author), `to` (a mail's To and
Cc, an item's assignees), `mentions` (someone a Slack message @-mentions). Someone seen
for the first time gets a record of their own (`person:slack:<id>`,
`person:email:<address>`, `person:github:<login>`); a name alone never makes a person
(a meeting attendee without an address links to the one person with that full name).
The user (`[tasks] me`, their Slack id, their GitHub login) is never a person.

The app's pass (`refresh`, every PASS_EVERY_S) joins identities: Slack's directory
gives each user's profile email, Contacts the contact an address belongs to. The store
merges the records those join into one (`Index.put_people`): the contact's
(`person:<contact id>`) when there is one, else the Slack user's, with every link moved
to it. A backfill (`Index.backfill_people`, run by scout-memory at startup) links what
was stored before.

A GitHub login joins its person by an address or account, never a name
(github_people.py: commit emails and the public profile; slack_github: a Slack profile
field for GitHub), or by being told: "Brian's GitHub is briankwest" is a fact (told_github)
the store applies (Index.apply_told) and takes back when it's forgotten. A same name is a
suggestion the user answers (Index.people_suggestions).

Uses: local_search's `person` ("what did I talk about with Joao"), tasks_list's `owner`
("what does Pat owe me", for any of the names Pat goes by), and the record pages (a
person's identities, and what's theirs)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger(__name__)

KIND = "person"
PASS_EVERY_S = 6 * 3600.0
FIRST_PASS_S = 120.0  # after the app starts
MAX_EMAILS = 400  # addresses looked up in Contacts a pass (two calls to the helper)
SOURCES = ("mail", "meeting", "slack", "github")
# Record -> person, strongest first: a record links each person once, by the first that fits.
RELS = ("from", "with", "to", "mentions")
MAX_PARTIES = 60  # people linked from one record (a mail to a list, a big meeting)
_EMAIL = re.compile(r"[^\s<>@\"',;]+@[^\s<>@\"',;]+\.[^\s<>@\"',;]+")
_SLACK_ID = re.compile(r"[UW][A-Z0-9]+")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
# Addresses that are a machine's, not a person's: no record of their own.
_AUTOMATED = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|notify|mailer-daemon|postmaster|bounces?|"
    r"alerts?|calendar-notification|invitations?|updates?|news(?:letter)?|marketing|support|billing|"
    r"receipts?|automated|system|root|daemon)\b|[+-]noreply\b|^.*-notes@|^meet-recordings",
    re.I,
)
NOT_PEOPLE = frozenset({"someone", "a bot", "unknown", "them", "speaker"})


def person_id(contact_id: str) -> str:
    return f"person:{contact_id}"


def norm_email(s: str) -> str:
    return s.strip().strip("<>").lower()


def norm_phone(s: str) -> str:
    """A phone number as a key: +<digits> when the country can be told ("+44 20 7946
    0958", or a North American 10 digits), else its digits; "" when it has none."""
    from .contacts_mac import dial_number
    from .mac import ToolError

    try:
        return dial_number(str(s or ""))
    except ToolError:
        return ""


def _fold(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def sender_email(who: str) -> str:
    from .mailindex import sender_address

    return sender_address(who)


def is_part(kind: str, id: str) -> bool:
    """A part of a longer record (a thread's chunk, a meeting's transcript): linked with
    the record it's part of, not on its own. A GitHub id's # is the item's number."""
    return kind != "github" and "#" in id


def automated(email: str) -> bool:
    return bool(_AUTOMATED.search(email))


def attendees(meta: dict[str, Any]) -> list[dict[str, str]]:
    return [a for a in meta.get("attendees") or [] if isinstance(a, dict)]


def emails_in(kind: str, who: str, meta: dict[str, Any]) -> list[str]:
    """The email addresses a record shows for the people in it: a mail's sender and
    recipients, a meeting's attendees."""
    if kind == "mail":
        out = [e] if (e := sender_email(who)) else []
        return out + [norm_email(m.group(0)) for k in ("to", "cc") for a in meta.get(k) or []
                      if (m := _EMAIL.search(str(a)))]  # fmt: skip
    if kind == "meeting":
        return [norm_email(str(a["email"])) for a in attendees(meta) if a.get("email")]
    return []


def slack_shown(u: dict[str, Any]) -> str:
    """The name a Slack user's messages are stored under (slack.Names)."""
    p = u.get("profile") or {}
    return str(p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or "")


# --- the user, and who Slack's ids are (read from the state scout-memory keeps) ----------------


@dataclass(frozen=True)
class Ctx:
    """What linking needs besides the record: who the user is (never a person), each
    DM's other party, and the names Slack has shown for its users."""

    me_slack: str = ""
    me_emails: frozenset[str] = frozenset()
    me_github: frozenset[str] = frozenset()
    me_names: frozenset[str] = frozenset()  # folded
    dm_user: dict[str, str] = field(default_factory=dict)  # DM channel -> the other party
    slack_names: dict[str, list[str]] = field(default_factory=dict)  # user id -> names

    def is_me(self, key: str, name: str = "") -> bool:
        kind, _, v = key.partition(":")
        if kind == "slack":
            return bool(self.me_slack) and v == self.me_slack
        if kind == "email":
            return v in self.me_emails
        if kind == "github":
            return v in self.me_github
        return bool(name) and _fold(name) in self.me_names

    def uid_named(self, name: str) -> str:
        """The Slack user who goes by this name, when exactly one does."""
        want = _fold(name)
        if not want:
            return ""
        got = {u for u, ns in self.slack_names.items() if any(_fold(n) == want for n in ns)}
        return next(iter(got)) if len(got) == 1 else ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def make_ctx(me: Sequence[str], slack_state: dict[str, Any], github_state: dict[str, Any]) -> Ctx:
    """The Ctx from `[tasks] me`, state/slack.json and state/github.json."""
    emails = {m.group(0).lower() for x in me if (m := _EMAIL.search(str(x)))}
    names = {_fold(re.sub(r"<[^>]*>", " ", str(x))) for x in me if "@" not in re.sub(r"<[^>]*>", "", str(x))}
    slack_names: dict[str, list[str]] = {}
    for uid, shown in (slack_state.get("users") or {}).items():
        slack_names.setdefault(str(uid), []).append(str(shown))
    for uid, p in (slack_state.get("people") or {}).items():
        if isinstance(p, dict):
            slack_names.setdefault(str(uid), []).extend(
                str(p.get(k) or "") for k in ("real", "display", "name")
            )
    slack_names = {u: list(dict.fromkeys(n for n in ns if n and n != u)) for u, ns in slack_names.items()}
    me_slack = str(slack_state.get("me") or "")
    names |= {_fold(n) for n in slack_names.get(me_slack, [])}
    dm = {
        str(cid): str(c["user"])
        for cid, c in (slack_state.get("convs") or {}).items()
        if isinstance(c, dict) and c.get("is_im") and c.get("user")
    }
    login = str(github_state.get("login") or "").lower()
    return Ctx(me_slack, frozenset(emails), frozenset({login} if login else ()), frozenset(n for n in names if n),
               dm, slack_names)  # fmt: skip


_ctx: tuple[float, Ctx] | None = None
_ctx_lock = threading.Lock()
CTX_TTL_S = 60.0


def context() -> Ctx:
    """The Ctx scout-memory links with, read again at most every CTX_TTL_S."""
    global _ctx
    with _ctx_lock:
        if _ctx is not None and time.monotonic() - _ctx[0] < CTX_TTL_S:
            return _ctx[1]
        from .config import DATA, load

        try:
            me = list(load().tasks.me)
        except Exception:  # a config being edited: no user names this minute
            me = []
        state = DATA / "state"
        got = make_ctx(me, _read_json(state / "slack.json"), _read_json(state / "github.json"))
        _ctx = (time.monotonic(), got)
        return got


# --- who a record is with ---------------------------------------------------------------------


class Party(NamedTuple):
    """Someone in a record: an identity key (slack:<id>, email:<address>, github:<login>,
    or name:<full name> / slackname:<name>, which only find someone already known), how
    they're in it, the name the record shows for them, and whether they may get a record
    of their own."""

    key: str
    rel: str
    name: str = ""
    create: bool = True


def _name_party(name: str, rel: str, slack: bool = False) -> Party | None:
    n = " ".join(str(name).split())
    if not n or _fold(n) in NOT_PEOPLE:
        return None
    if slack:
        return Party(f"slackname:{_fold(n)}", rel, n, False)
    return Party(f"name:{_fold(n)}", rel, n, False) if len(n.split()) > 1 else None


def _slack_parties(id: str, title: str, who: str, meta: dict[str, Any], ctx: Ctx) -> list[Party]:
    channel = str(meta.get("channel") or (id.split(":")[1] if id.count(":") >= 2 else ""))
    named: dict[str, str] = {str(k): str(v) for k, v in (meta.get("names") or {}).items()}
    dm_user = str(meta.get("dm_user") or ctx.dm_user.get(channel) or "")
    group = title.startswith("Group DM with")
    out: list[Party] = []

    def slack_party(uid: str, rel: str) -> None:
        if _SLACK_ID.fullmatch(uid) and uid != "USLACKBOT":
            shown = named.get(uid, "")  # slack.Names gives the id itself when it couldn't look it up
            name = shown if shown and shown != uid else next(iter(ctx.slack_names.get(uid, [])), "")
            out.append(Party(f"slack:{uid}", rel, name))

    if dm_user:
        slack_party(dm_user, "with")
    if meta.get("bot"):  # an app's messages: only a DM's other party
        return out
    lines = [ln for ln in meta.get("lines") or [] if isinstance(ln, list) and ln]
    if lines:
        for ln in lines:
            slack_party(str(ln[0] or ""), "with" if dm_user or group else "from")
        for ln in lines:
            for uid in ln[1] if len(ln) > 1 and isinstance(ln[1], list) else []:
                slack_party(str(uid), "mentions")
        return out
    # Stored before messages kept their authors' ids: the names, through Slack's users.
    for n in who.split(", "):
        uid = ctx.uid_named(n)
        if uid:
            slack_party(uid, "with" if dm_user or group else "from")
        elif p := _name_party(n, "with" if dm_user or group else "from", slack=True):
            out.append(p)
    return out


def _display_name(who: str) -> str:
    n = re.sub(r"<[^>]*>", " ", who).strip().strip('"').strip("'").strip()
    return "" if "@" in n else " ".join(n.split())


def _email_party(raw: str, rel: str, name: str = "", create: bool = True) -> Party | None:
    m = _EMAIL.search(raw)
    if not m:
        return None
    e = norm_email(m.group(0))
    return Party(f"email:{e}", rel, name, create and not automated(e))


def participants(kind: str, id: str, title: str, who: str, meta: dict[str, Any], ctx: Ctx) -> list[Party]:
    """The people in a record, the user left out, each once (by their strongest rel)."""
    out: list[Party | None] = []
    if is_part(kind, id):
        return []
    if kind == "slack":
        out += _slack_parties(id, title, who, meta, ctx)
    elif kind == "mail":
        sender = sender_email(who)
        if sender:
            out.append(
                Party(
                    f"email:{sender}",
                    "from",
                    _display_name(who),
                    not automated(sender) and not meta.get("bulk"),
                )
            )
        out += [_email_party(str(a), "to") for k in ("to", "cc") for a in meta.get(k) or []]
    elif kind == "meeting":
        listed = attendees(meta)
        if listed:
            for a in listed:
                name = str(a.get("name") or "")
                out.append(
                    _email_party(str(a["email"]), "with", name)
                    if a.get("email")
                    else _name_party(name, "with")
                )
        else:
            for n in who.split(","):
                out.append(_email_party(n, "with", _display_name(n)) if "@" in n else _name_party(n, "with"))
    elif kind == "github":
        logins = [(str(meta.get("author") or ""), "from")] + [
            (str(a), "to") for a in meta.get("assignees") or []
        ]
        for login, rel in logins:
            if _LOGIN.fullmatch(login) and not login.endswith("[bot]"):
                out.append(Party(f"github:{login.lower()}", rel, login))
    seen: dict[str, Party] = {}
    for p in out:
        if p is None or ctx.is_me(p.key, p.name):
            continue
        if p.key not in seen or RELS.index(p.rel) < RELS.index(seen[p.key].rel):
            seen[p.key] = p
    return list(seen.values())[:MAX_PARTIES]


# --- a person's record ------------------------------------------------------------------------


def blank() -> dict[str, Any]:
    return {"contact": "", "name": "", "emails": [], "phones": [], "slack": [], "github": [], "names": []}


def meta_of(p: dict[str, Any]) -> dict[str, Any]:
    """A person's record meta from what's known of them (people.build's or a stored one)."""
    m = blank()
    m["contact"] = str(p.get("contact") or "")
    m["name"] = str(p.get("name") or "")
    m["emails"] = list(dict.fromkeys(norm_email(str(e)) for e in p.get("emails") or [] if str(e).strip()))
    m["phones"] = list(dict.fromkeys(n for x in p.get("phones") or [] if (n := norm_phone(str(x)))))
    m["slack"] = [
        {"id": str(s.get("id") or ""), "shown": str(s.get("shown") or ""), "real": str(s.get("real") or "")}
        for s in p.get("slack") or []
        if isinstance(s, dict) and s.get("id")
    ]
    m["github"] = list(dict.fromkeys(str(g).lower() for g in p.get("github") or [] if g))
    m["names"] = names(p)
    return m


def merge(metas: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One person's meta from several: the first contact and name win; identities and
    names are all kept."""
    out = blank()
    for m in metas:
        out["contact"] = out["contact"] or str(m.get("contact") or "")
        out["name"] = out["name"] or str(m.get("name") or "")
        out["emails"] = list(dict.fromkeys([*out["emails"], *(m.get("emails") or [])]))
        out["phones"] = list(dict.fromkeys([*out["phones"], *(m.get("phones") or [])]))
        have = {s["id"]: s for s in out["slack"]}
        for s in m.get("slack") or []:
            if s.get("id") in have:
                old = have[s["id"]]
                old["shown"], old["real"] = (
                    old["shown"] or s.get("shown", ""),
                    old["real"] or s.get("real", ""),
                )
            else:
                have[s["id"]] = dict(s)
                out["slack"].append(have[s["id"]])
        out["github"] = list(dict.fromkeys([*out["github"], *(m.get("github") or [])]))
        out["names"] = _uniq_names([*out["names"], *(m.get("names") or [])])
    return out


def _uniq_names(ns: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for n in ns:
        n = " ".join(str(n or "").split())
        if n and _fold(n) not in (_fold(o) for o in out):
            out.append(n)
    return out


def names(p: dict[str, Any]) -> list[str]:
    """Every name a person goes by: the contact's, then Slack's, then the rest seen."""
    return _uniq_names([p.get("name"), *(x for s in p.get("slack") or [] for x in (s.get("real"), s.get("shown"))),
                        *(p.get("names") or [])])  # fmt: skip


def strong_keys(meta: dict[str, Any]) -> list[str]:
    """A person's identities: each one is only ever theirs."""
    out = [f"contact:{meta['contact']}"] if meta.get("contact") else []
    out += [f"email:{e}" for e in meta.get("emails") or []]
    out += [f"phone:{n}" for n in meta.get("phones") or []]
    out += [f"slack:{s['id']}" for s in meta.get("slack") or [] if s.get("id")]
    out += [f"github:{g}" for g in meta.get("github") or []]
    return out


def name_keys(meta: dict[str, Any]) -> list[str]:
    """The names they're found by when a record shows only a name: a full name (meetings),
    and for a Slack user any name Slack showed (old Slack records)."""
    ns = [_fold(n) for n in meta.get("names") or []]
    out = [f"name:{n}" for n in ns if len(n.split()) > 1]
    if meta.get("slack"):
        out += [f"slackname:{n}" for n in ns]
    return list(dict.fromkeys(out))


def canonical(meta: dict[str, Any]) -> str:
    """The record id a person is kept under: the contact's, else their Slack user's,
    else their first address's or GitHub login's."""
    if meta.get("contact"):
        return person_id(str(meta["contact"]))
    for key in strong_keys(meta):
        if key.startswith("slack:"):
            return f"person:{key}"
    keys = strong_keys(meta)
    return f"person:{keys[0]}" if keys else ""


def from_party(p: Party) -> dict[str, Any]:
    """The meta of someone first seen in a record."""
    m = blank()
    kind, _, v = p.key.partition(":")
    if kind == "slack":
        m["slack"] = [{"id": v, "shown": "", "real": ""}]
    elif kind == "email":
        m["emails"] = [v]
    elif kind == "github":
        m["github"] = [v]
    m["names"] = _uniq_names([p.name])
    return m


def record(p: dict[str, Any]) -> dict[str, Any]:
    """The store item for a person (kind person; no ts: it keeps the one it has). Their
    addresses are only in meta."""
    meta = meta_of(p)
    known = meta["names"]
    title = str(meta.get("name") or (known[0] if known else ""))
    if not title:
        keys = strong_keys(meta)
        title = keys[0].partition(":")[2] if keys else "Someone"
        if "@" in title:
            title = title.split("@")[0]
    n = len(meta["emails"])
    bits = [f"{n} email address{'es' * (n != 1)}"] if n else []
    bits += [f"Slack {s['shown'] or s['real'] or s['id']}" for s in meta["slack"]]
    bits += [f"GitHub {g}" for g in meta["github"]]
    return {
        "id": canonical(meta),
        "kind": KIND,
        "title": title,
        "body": f"{title}: " + ("; ".join(bits) or "seen in Scout's records"),
        "who": ", ".join(known)[:300],
        "meta": meta,
    }


def build(
    found: dict[str, dict[str, Any]],
    slack_users: Sequence[dict[str, Any]] = (),
    github: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """What the app's pass knows: `found` is Contacts' answer for each address ({email:
    {id, name, emails, phones, me}}: the card it's on, with every address and number on
    it; the user's own card, "me", is nobody's), `slack_users` Slack's users.list members, `github` the GitHub login each
    Slack user's profile gives (slack_github). A Slack user joins the contact their
    profile email belongs to; one who joins none is listed on their own (with their
    profile email and login), for the store to join to what it has. A name two Slack
    users share isn't one to find them by."""
    github = github or {}
    people: dict[str, dict[str, Any]] = {}
    cards = [(norm_email(email), c) for email, c in sorted(found.items()) if c.get("id") and not c.get("me")]
    # An address or number on two cards (a shared office line) is neither's.
    on: dict[str, set[str]] = {}
    for email, c in cards:
        for k in {f"email:{email}", *card_keys(c)}:
            on.setdefault(k, set()).add(str(c["id"]))
    for email, c in cards:
        p = people.setdefault(
            str(c["id"]),
            {
                "contact": str(c["id"]),
                "name": str(c.get("name") or ""),
                "emails": [],
                "phones": [],
                "slack": [],
            },
        )
        for k in [f"email:{email}", *card_keys(c)]:
            kind, _, v = k.partition(":")
            got = p["emails"] if kind == "email" else p["phones"]
            if len(on[k]) == 1 and v not in got:
                got.append(v)
    shown_by = [_fold(slack_shown(u)) for u in slack_users]
    by_email = {e: p for p in people.values() for e in p["emails"]}
    alone: list[dict[str, Any]] = []
    for u in slack_users:
        if u.get("deleted") or u.get("is_bot") or not u.get("id"):
            continue
        prof = u.get("profile") or {}
        email = norm_email(str(prof.get("email") or ""))
        shown = slack_shown(u)
        entry = {
            "id": str(u.get("id") or ""),
            "shown": shown if shown and shown_by.count(_fold(shown)) == 1 else "",
            "real": str(prof.get("real_name") or u.get("real_name") or ""),
        }
        login = github.get(entry["id"], "")
        owner = by_email.get(email) if email else None
        if owner is not None:
            owner["slack"].append(entry)
            if login:
                owner.setdefault("github", []).append(login)
        else:
            alone.append({"contact": "", "name": entry["real"] or entry["shown"], "emails": [email] if email else [],
                          "slack": [entry], "github": [login] if login else []})  # fmt: skip
    return sorted(people.values(), key=lambda p: p["contact"]) + alone


def card_keys(c: dict[str, Any]) -> list[str]:
    """The identity keys a contact card gives (its email addresses and phone numbers)."""
    out = [f"email:{e}" for x in c.get("emails") or [] if (e := norm_email(str(x)))]
    out += [f"phone:{n}" for x in c.get("phones") or [] if (n := norm_phone(str(x)))]
    return list(dict.fromkeys(out))


def linked(lns: Iterable[dict[str, Any]]) -> list[str]:
    """From a person's links (Index.links), the records that are theirs."""
    return list(dict.fromkeys(
        str(ln["id"]) for ln in lns
        if (ln["dir"] == "in" and ln["rel"] in RELS) or (ln["dir"] == "out" and ln["rel"] == "same_person")
    ))  # fmt: skip


def candidate_emails(rows: Iterable[tuple[str, str, str]], limit: int = MAX_EMAILS) -> list[str]:
    """The addresses in (kind, who, meta json) rows, newest first, each once."""
    out: dict[str, None] = {}
    for kind, who, meta in rows:
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            m = {}
        for e in emails_in(kind, who, m if isinstance(m, dict) else {}):
            if _EMAIL.fullmatch(e) and not automated(e):
                out.setdefault(e, None)
                if len(out) >= limit:
                    return list(out)
    return list(out)


# --- a GitHub login, told or from a profile ---------------------------------------------------

_GH_WORD = r"git\s?hub(?:\s(?:login|username|user\s?name|handle|account|user|id|name))?"
_TOLD = (
    re.compile(rf"^(?P<name>.+?)(?:'s|s')\s{_GH_WORD}\sis\s(?P<not>not\s)?(?P<login>.+)$", re.I),
    re.compile(r"^(?P<name>.+?)\sis\s(?P<not>not\s)?(?P<login>.+?)\son\sgit\s?hub$", re.I),
    re.compile(r"^(?P<login>\S+)\son\sgit\s?hub\sis\s(?P<not>not\s)?(?P<name>.+)$", re.I),
)
_NOT_LOGINS = frozenset(
    {"a", "an", "the", "it", "that", "this", "unknown", "private", "none", "empty", "same"}
)
_NOT_NAMES = frozenset(
    {"my", "me", "i", "your", "you", "his", "her", "their", "our", "its", "it", "that", "this"}
)


class Told(NamedTuple):
    """A fact naming someone's GitHub login: "Brian's GitHub is briankwest" (folded name,
    login), or "... is not ..." (negative: a no to a suggestion)."""

    name: str
    login: str
    negative: bool


def github_login(raw: Any) -> str:
    """A GitHub login from how it's written: "briankwest", "@BrianKWest",
    "https://github.com/briankwest/", "brian k west" (said aloud); "" when it can't be one."""
    from .memory import _STOP

    t = str(raw or "").strip().strip(".,!?\"'").strip()
    t = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", t, flags=re.I).strip("/")
    t = t.lstrip("@")
    words = t.split()
    if not words or (len(words) > 1 and any(w.lower() in _STOP for w in words)) or t.lower() in _NOT_LOGINS:
        return ""
    login = "".join(words).lower()
    return login if _LOGIN.fullmatch(login) and not login.endswith("-") else ""


def told_github(fact: str) -> Told | None:
    """The GitHub login a fact gives someone, or None when it isn't one."""
    t = " ".join(str(fact or "").replace("\u2019", "'").split()).strip().rstrip(".!")
    if t.endswith("?"):
        return None
    for pat in _TOLD:
        m = pat.match(t)
        if m is None:
            continue
        name = _fold(m.group("name")).strip("\"'")
        login = github_login(m.group("login"))
        if (
            not name
            or not login
            or name in _NOT_NAMES
            or name.split()[0] in ("what", "who", "whats", "what's")
        ):
            return None
        return Told(name, login, bool(m.group("not")))
    return None


# Slack's custom profile field for GitHub, when the workspace has one (team.profile.get).
SLACK_FIELD_EVERY_S = 86400.0  # whether there's a field, asked again this often
SLACK_PROFILE_TTL_S = 7 * 86400.0  # one user's profile, read again this rarely
MAX_SLACK_PROFILES = 100  # users.profile.get calls a pass
_GH_LABEL = re.compile(r"\bgit\s?hub\b", re.I)


def slack_github(
    get: Any, users: Sequence[dict[str, Any]], state: dict[str, Any], now: float
) -> dict[str, str]:
    """{Slack user id: GitHub login} from the workspace's GitHub profile field. Without
    such a field (or the users.profile:read scope that reads it), that's recorded in
    `state` (why: "none", or Slack's error such as "missing_scope") and nothing more is
    asked until it's checked again a day later. Profiles are read MAX_SLACK_PROFILES a
    pass and kept SLACK_PROFILE_TTL_S."""
    from .slack import SlackError

    f: dict[str, Any] = state.setdefault("slack_field", {})
    if now - float(f.get("checked") or 0) >= SLACK_FIELD_EVERY_S:
        try:
            fields = ((get("team.profile.get", {}).get("profile") or {}).get("fields")) or []
        except SlackError as exc:
            f.update(id="", why=str(exc).rpartition(": ")[2] or "error", checked=now)
        else:
            hit = next(
                (x for x in fields if isinstance(x, dict) and _GH_LABEL.search(str(x.get("label") or ""))),
                None,
            )
            f.update(id=str(hit.get("id") or "") if hit else "", why="" if hit else "none", checked=now)
        log.info("people: Slack's GitHub profile field: %s", "found" if f["id"] else f["why"])
    fid = str(f.get("id") or "")
    if not fid:
        return {}
    known: dict[str, Any] = state.setdefault("slack_github", {})
    due = [str(u["id"]) for u in users if u.get("id") and now - float((known.get(str(u["id"])) or {}).get("checked") or 0)
           >= SLACK_PROFILE_TTL_S][:MAX_SLACK_PROFILES]  # fmt: skip
    for uid in due:
        try:
            prof = get("users.profile.get", {"user": uid}).get("profile") or {}
        except SlackError as exc:
            if "user_not_found" in str(exc):
                known[uid] = {"login": "", "checked": now}
                continue
            log.info("people: Slack profiles stopped this pass (%s)", str(exc).rpartition(": ")[2])
            break
        val = ((prof.get("fields") or {}).get(fid) or {}) if isinstance(prof.get("fields"), dict) else {}
        login = github_login(val.get("value")) or github_login(val.get("alt"))
        known[uid] = {"login": login, "checked": now}
    return {uid: str(v["login"]) for uid, v in known.items() if isinstance(v, dict) and v.get("login")}


# --- the app's pass ---------------------------------------------------------------------------


STATE = Path("state") / "people.json"  # under DATA: the Slack GitHub field and profiles read


async def _slack_users() -> tuple[list[dict[str, Any]] | None, dict[str, str]]:
    """Slack's members with their profiles, or None when Slack can't be read (not set up),
    and the GitHub login each one's profile field gives (slack_github)."""
    from . import slack_send
    from .config import DATA

    try:
        get = slack_send._fetch()
        people, _ = await asyncio.to_thread(slack_send.directory, get)
    except Exception as exc:  # no token, offline, refused: no Slack this pass
        log.info("people: no Slack users this pass (%s)", type(exc).__name__)
        return None, {}
    path = DATA / STATE
    state = _read_json(path)
    try:
        logins = await asyncio.to_thread(slack_github, get, list(people), state, time.time())
    except Exception as exc:  # offline mid-pass: the users without their logins
        log.info("people: no Slack GitHub field this pass (%s)", type(exc).__name__)
        logins = {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except OSError as exc:
        log.info("people: %s not saved (%s)", path.name, type(exc).__name__)
    return list(people), logins


async def _resolve(emails: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Contacts' {email: {id, name, emails, phones, me?}} (the helper's `cards`: the card
    each address is on) for the addresses it has; {} without the permission (never asks
    for it)."""
    from . import contacts_mac
    from .mac import ToolError

    h = contacts_mac.HELPER
    if not emails or not h.binary.exists() or not await contacts_mac._granted(h):
        return {}
    found: dict[str, dict[str, Any]] = {}
    cmd, key = "cards", "cards"
    for i in range(0, len(emails), 200):
        args = [a for e in emails[i : i + 200] for a in ("--handle", e)]
        try:
            try:
                got = (await contacts_mac.run(cmd, *args, helper=h)).get(key) or {}
            except ToolError as exc:
                if cmd != "cards" or "unknown command" not in str(exc):
                    raise
                cmd, key = "resolve", "names"  # a helper built before `cards`: names only
                got = (await contacts_mac.run(cmd, *args, helper=h)).get(key) or {}
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
    users, logins = await _slack_users()
    seen = await asyncio.to_thread(store.people_candidates, MAX_EMAILS)
    emails = list(
        dict.fromkeys(
            [*seen, *(norm_email(str((u.get("profile") or {}).get("email") or "")) for u in users or [])]
        )
    )
    emails = [e for e in emails if e][: MAX_EMAILS * 2]
    found = await _resolve(emails)
    if not found and users is None:
        return {"emails": len(emails), "people": 0}
    people = build(found, users or [], logins)
    # Contacts' answer is the whole list only when it answered: a person it no longer
    # knows loses the contact (and keeps their other identities and links).
    got = await asyncio.to_thread(store.put_people, people, bool(found))
    keys = sum(len(p["emails"]) + len(p.get("phones") or []) for p in people if p.get("contact"))
    log.info("people: %d addresses, %d people known, %d links to them, %d keys from their contact cards",
             len(emails), got.get("people", 0), got.get("links", 0), keys)  # fmt: skip
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
    when the task was found in a record of theirs (linked to them). Without Contacts, the
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
            theirs = set(linked(await asyncio.to_thread(store.links, pid)))
        srcs = {
            ln["id"] for ln in await asyncio.to_thread(store.links, f"task:{t.n}") if ln["rel"] == "found_in"
        }
        if srcs & theirs:
            out.append(t)
    return out
