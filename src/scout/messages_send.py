"""Texting someone from the Mac's Messages, only after a yes (docs/contacts.md, Text).

Two tools, like Slack's draft and send:

    message_draft {to, text}   who: a name from Contacts ("Pat"), a phone number or an
                               email address. One person: a draft with an id, to the
                               handle picked for them (below). Several: their names, to
                               ask the user which. Sends nothing.
    message_send {draft}       sends that draft, once (shared_tools: asks=True). The room
                               reads it back ("Text Pat Doe: I'm running late?") and
                               waits for a spoken or clicked yes; other sessions are
                               asked in the room; the phone in two steps (prepare, then
                               confirm_action after the caller's yes). "Yes, always"
                               never applies (trust.NEVER).

Which handle: of the person's phone numbers and email addresses, the one with the most
recent conversation in Messages (the Messages helper's `chat`, newest message first),
on the service that conversation used (iMessage, or SMS / RCS through the iPhone);
with no history, their iPhone or mobile number, else another number, else an email
address, over iMessage.

How it's sent: AppleScript to Messages.app (`send` to the `participant` of the iMessage
or SMS account), run with osascript from the app like Mail's scripts. The first send
shows macOS's Automation prompt (Scout wants to control Messages); until it's allowed
nothing is sent and the answer says where to turn it on. The script is a constant; the
handle and text are its arguments, never spliced into it.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import contacts_mac, mac, messages_mac, speech
from .mac import ToolError

DRAFT_TTL_S = 1800.0
MAX_CHARS = 2000
SPOKEN_CHARS = 300
HISTORY_DAYS = 365
SEND_TIMEOUT_S = 30.0
NOT_ALLOWED = (
    "Not allowed to control Messages yet. Allow it in the prompt on the Mac's screen "
    "(Scout wants access to control Messages), or in System Settings, Privacy and Security, "
    "Automation. Nothing was sent."
)

# Everything before `send` is in the try: an error there (no such account, no
# permission) means nothing went out, and the script says so instead of failing.
SEND_SCRIPT = """
on run argv
    set theHandle to item 1 of argv
    set theService to item 2 of argv
    set theText to item 3 of argv
    tell application "Messages"
        try
            if theService is "SMS" then
                set theAccount to first account whose service type is SMS
            else
                set theAccount to first account whose service type is iMessage
            end if
            set theBuddy to participant theHandle of theAccount
        on error errText number errNum
            return "notsent:" & errNum & ":" & errText
        end try
        send theText to theBuddy
    end tell
    return "sent"
end run
"""


@dataclass
class Draft:
    id: str
    name: str  # "Pat Doe", or the handle when Contacts has no name for it
    handle: str  # "+15550102000" or "pat@example.com"
    service: str  # "iMessage" or "SMS"
    label: str  # "mobile", "work", "email"...
    text: str
    made: float
    sent: bool = False


Sender = Callable[[str, str, str], Awaitable[str]]


async def _osascript(handle: str, service: str, text: str) -> str:
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise RuntimeError("tests must not send texts")
    return await mac._run(
        "/usr/bin/osascript", "-e", SEND_SCRIPT, "--", handle, service, text, timeout=SEND_TIMEOUT_S
    )


SEND: Sender = _osascript  # tests replace it; nothing else sends

_drafts: dict[str, Draft] = {}
_ids = itertools.count(1)
_lock = threading.RLock()


async def last_seen(handle: str) -> tuple[str, str] | None:
    """(when, service) of the newest message in the one-to-one conversation with
    `handle`, from the Messages helper; None when there's none or the helper can't
    say. Never starts the helper's Full Disk Access setup."""
    try:
        data = await messages_mac._send(
            messages_mac.HELPER, "chat", {"chat": handle, "limit": 1, "since_days": HISTORY_DAYS}
        )
    except ToolError:
        return None
    msgs = (data.get("messages") or []) if data.get("ok") else []
    if not msgs:
        return None
    return str(msgs[0].get("date") or ""), str(msgs[0].get("service") or "")


def _service(seen: str) -> str:
    return "SMS" if seen.upper() in ("SMS", "RCS") else "iMessage"


async def pick_handle(p: dict[str, Any]) -> tuple[str, str, str]:
    """(handle, service, label) to text a contact at (the module docstring)."""
    choices = [(contacts_mac.dial_number(n), label or "phone") for n, label in contacts_mac.phone_choices(p)]
    choices += [
        (str(x["value"]).strip(), x.get("label") or "email") for x in p.get("emails") or [] if x.get("value")
    ]
    if not choices:
        raise contacts_mac.Unclear(f"{p.get('name')} has no phone number or email address in Contacts")
    best: tuple[str, str, str, str] | None = None  # (when, handle, service, label)
    for handle, label in choices[:6]:
        got = await last_seen(handle)
        if got and got[0] and (best is None or got[0] > best[0]):
            best = (got[0], handle, _service(got[1]), str(label))
    if best is not None:
        return best[1], best[2], best[3]
    handle, label = choices[0]
    return handle, "iMessage", str(label)


def _text(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        raise ToolError("text is required")
    if len(s) > MAX_CHARS:
        raise ToolError(f"text is too long for a text message (at most {MAX_CHARS} characters)")
    if any(ord(c) < 32 and c not in "\n\t" for c in s):
        raise ToolError("text has control characters")
    return s


async def draft(a: dict[str, Any]) -> str:
    to = " ".join(str(a.get("to") or "").split())
    text = _text(a.get("text"))
    if not to:
        raise ToolError("to is required: a name from Contacts, a phone number or an email address")
    if len(to) > 100:
        raise ToolError("to is too long")
    if contacts_mac.is_handle(to):
        handle = to if "@" in to else contacts_mac.dial_number(to)
        name = (await contacts_mac.names_for([to, handle])).get(to) or handle
        seen = await last_seen(handle)
        service = _service(seen[1]) if seen else "iMessage"
        label = "email" if "@" in handle else "phone"
    else:
        p = await contacts_mac.one_person(to, "message_draft")
        if isinstance(p, str):
            return p
        handle, service, label = await pick_handle(p)
        name = str(p.get("name") or to)
    with _lock:
        now = time.time()
        for k in [k for k, d in _drafts.items() if now - d.made > DRAFT_TTL_S]:
            del _drafts[k]
        d = Draft(f"m{next(_ids)}", name, handle, service, label, text, now)
        _drafts[d.id] = d
    return (
        f"Draft {d.id}: text {d.name} ({d.label} {d.handle}, {d.service}): {d.text}\n"
        f"Not sent. To send it, call message_send with draft {d.id}; the user confirms it first."
    )


def _draft(a: dict[str, Any]) -> Draft | None:
    with _lock:
        d = _drafts.get(str(a.get("draft") or "").strip())
    if d is None or d.sent or time.time() - d.made > DRAFT_TTL_S:
        return None
    return d


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def describe(a: dict[str, Any], full: bool = False) -> tuple[str, str]:
    """The confirmation: (spoken question, then on its own line a short read-back of the
    text; the page's detail with all of it). full=True: the phone's read-back, the text
    itself up to SPOKEN_CHARS (the caller has no page to look at)."""
    d = _draft(a)
    if d is None:
        return "Send a text?", f"send text draft {a.get('draft')!r} (no such draft)"
    detail = f"send text to {d.name} ({d.label} {d.handle}, {d.service})\n\n{d.text}"
    if full:
        return f"Text {d.name}: {_clip(d.text, SPOKEN_CHARS)}?", detail
    return f"Send the text to {d.name}?\n{speech.read_back(d.text)}", detail


def recipient(a: dict[str, Any]) -> str:
    d = _draft(a)
    return d.name if d is not None else "them"


async def send(a: dict[str, Any]) -> str:
    """Only ever after the user's yes (asks=True): send the draft, once."""
    with _lock:
        d = _draft(a)
        if d is None:
            raise ToolError(
                "There's no such draft (or it was sent or is too old): make it again with message_draft."
            )
        d.sent = True  # taken now: a second call can't send it twice
    try:
        out = await SEND(d.handle, d.service, d.text)
    except ToolError as exc:
        if "-1743" in str(exc) or "Not authorized" in str(exc):
            _untake(d)
            raise ToolError(NOT_ALLOWED) from None
        raise ToolError(
            f"Messages didn't confirm it ({str(exc)[:120]}), so it may have been sent: check Messages "
            "before sending it again."
        ) from None
    if out.startswith("notsent:"):
        _untake(d)
        if "-1743" in out:
            raise ToolError(NOT_ALLOWED)
        raise ToolError(
            f"Messages couldn't text {d.handle} over {d.service} ({out.split(':', 2)[-1][:120]}). "
            "Nothing was sent."
        )
    return f"Sent to {d.name} ({d.handle}) in Messages."


def _untake(d: Draft) -> None:
    with _lock:
        d.sent = False  # nothing went out: it can be sent after all
