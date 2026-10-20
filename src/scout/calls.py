"""Calling someone (docs/contacts.md, Call).

    call_prepare {to}   who: a name from Contacts ("Pat") or a phone number. One person:
                        a call with an id, to their iPhone or mobile number first (an
                        email address only when they have no number: a FaceTime audio
                        call). Several: their names, to ask which. Starts nothing.
    call_start {call}   in the room or another session (asks=True: "Call Pat Doe at
                        5 5 5, 0 1 0, 2 0 0 0, mobile?"): opens the tel: URL on the Mac,
                        so macOS shows its call prompt and the call goes through the
                        user's iPhone (Continuity, "Calls from iPhone"). Never by phone.
    call_link {call}    on the phone (Scout's SignalWire agent can't hand a call over):
                        puts the number in the Scout reminders list as a tappable tel:
                        link, like send_link. Scout's own list, so no spoken yes.

Opening a tel: URL needs no permission: the call prompt on the Mac is a second yes
(its Call button), and the call itself is made by the iPhone."""

from __future__ import annotations

import itertools
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import contacts_mac, mac, refs, reminders_mac
from .mac import ToolError

CALL_TTL_S = 1800.0
LINK_AGAIN_S = 3600.0  # the same number isn't put in the reminders twice within this


@dataclass
class Call:
    id: str
    name: str
    number: str  # "+15550102000", or an email address for FaceTime audio
    label: str
    made: float
    started: bool = False

    @property
    def url(self) -> str:
        return f"facetime-audio:{self.number}" if "@" in self.number else f"tel:{self.number}"

    @property
    def spoken(self) -> str:
        return self.number if "@" in self.number else contacts_mac.spoken_number(self.number)


Opener = Callable[[str], Awaitable[Any]]


async def _open(url: str) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("tests must not place calls")
    await mac._run("/usr/bin/open", url)


OPEN: Opener = _open  # tests replace it; nothing else places a call

_calls: dict[str, Call] = {}
last_started = 0.0  # time.time() of the last call started here (Scout plays no test sweep near one)
_linked: dict[str, float] = {}
_ids = itertools.count(1)
_lock = threading.RLock()


async def prepare(a: dict[str, Any]) -> str:
    to = " ".join(str(a.get("to") or "").split())
    if not to:
        raise ToolError("to is required: a name from Contacts or a phone number")
    if len(to) > 100:
        raise ToolError("to is too long")
    if contacts_mac.is_handle(to):
        number = to if "@" in to else contacts_mac.dial_number(to)
        name = (await contacts_mac.names_for([to, number])).get(to) or number
        label = "email" if "@" in number else "phone"
    else:
        p = await contacts_mac.one_person(to, "call_prepare")
        if isinstance(p, str):
            return p
        return _prepared(call_for(p))
    return _prepared(_keep(name, number, label))


def call_for(p: dict[str, Any]) -> Call:
    """A call to one contact (Contacts' record): their iPhone or mobile number first, else
    another number, else a FaceTime audio call to their email address. Starts nothing:
    call_prepare for a name, and tier 0's "call Pat" (local_intents.py)."""
    name = str(p.get("name") or "them")
    phones = contacts_mac.phone_choices(p)
    emails = [str(x["value"]) for x in p.get("emails") or [] if x.get("value")]
    if phones:
        number, label = contacts_mac.dial_number(phones[0][0]), phones[0][1] or "phone"
    elif emails:
        number, label = emails[0], "FaceTime audio"
    else:
        raise contacts_mac.Unclear(f"{name} has no phone number in Contacts")
    return _keep(name, number, label)


def _keep(name: str, number: str, label: str) -> Call:
    with _lock:
        now = time.time()
        for k in [k for k, c in _calls.items() if now - c.made > CALL_TTL_S]:
            del _calls[k]
        c = Call(f"c{next(_ids)}", name, number, label, now)
        _calls[c.id] = c
    return c


def _prepared(c: Call) -> str:
    return (
        f"Call {c.id}: {c.name}, {c.label}, {c.number}. Not started. In the room, call_start with "
        f"call {c.id} starts it on the Mac through the user's iPhone (the user confirms first). On the "
        f"phone it can't be connected: offer call_link with call {c.id}, which puts the number in the "
        "user's Scout reminders as a link to tap."
    )


def _call(a: dict[str, Any]) -> Call | None:
    with _lock:
        c = _calls.get(str(a.get("call") or "").strip())
    if c is None or time.time() - c.made > CALL_TTL_S:
        return None
    return c


def describe(a: dict[str, Any]) -> tuple[str, str]:
    c = _call(a)
    if c is None or c.started:
        return "Start a call?", f"call {a.get('call')!r} (no such call)"
    label = f", {c.label}" if c.label and c.label not in ("phone", "email") else ""
    return (
        f"Call {c.name} at {c.spoken}{label}?",
        f"call {c.name} ({c.label} {c.number}) through the iPhone\n\n{c.url}",
    )


def recipient(a: dict[str, Any]) -> str:
    c = _call(a)
    return c.name if c is not None else "them"


async def start(a: dict[str, Any]) -> str:
    """Only ever after the user's yes (asks=True): open the call prompt on the Mac, once."""
    with _lock:
        c = _call(a)
        if c is None or c.started:
            raise ToolError(
                "There's no such call (or it was started or is too old): prepare it again with call_prepare."
            )
        c.started = True
    try:
        await OPEN(c.url)
    except ToolError as exc:
        with _lock:
            c.started = False
        raise ToolError(f"The Mac couldn't start the call: {exc}") from None
    global last_started
    last_started = time.time()
    return (
        f"Calling {c.name} at {c.number}: the Mac shows its call prompt (click Call); the call "
        "goes through the iPhone."
    )


async def link(a: dict[str, Any]) -> str:
    """The number as a tappable link in the Scout reminders list (for the phone)."""
    c = _call(a)
    if c is None:
        raise ToolError("There's no such call: prepare it again with call_prepare.")
    now = time.time()
    with _lock:
        for k in [k for k, t in _linked.items() if now - t > LINK_AGAIN_S]:
            del _linked[k]
        if c.url in _linked:
            return f"{c.name}'s number is already in your Scout reminders."
        _linked[c.url] = now
    try:
        await reminders_mac.add_data(
            {"title": f"Call {c.name}"[:200], "list": refs.LINK_LIST, "url": c.url},
            mirror=True,
        )
    except ToolError:
        with _lock:
            _linked.pop(c.url, None)
        raise
    return f"I put {c.name}'s number in your Scout reminders: tap it there to call."
