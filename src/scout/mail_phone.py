"""Email by phone (docs/phone-design.md, "Email by phone").

The phone agent (scout-phone) reaches Mail through Scout's local API as agent "phone";
run_shared_tool sends its mail_draft and mail_send here instead of to mail_mac's
room versions. The rules:

- mail_draft saves the message in Mail's Drafts with no window (nobody is at the Mac
  to see one), in one step: it sends nothing.
- mail_send takes two steps (phone_service: prepare, then confirm_action after the
  caller's yes, behind SignalWire's semantic gate). preview() is step one: it resolves
  names to addresses through Contacts and returns the read-back (each exact address,
  the subject and the body, for the agent to say in one sentence) and the resolved
  arguments, which are what step two sends (send()). A send goes out at most once.
- Who to: one to three addresses in To (each one said by the caller and read back);
  no Cc, Bcc or attachments. Each must be in the user's Contacts or one the user has
  sent mail to before (Mail's index, through the scout-messages helper: mail_sent_to).
  When neither can be checked (Contacts off, the helper without Full Disk Access),
  the address counts as unknown: refused, never let through.
- A body longer than a few sentences (SEND_MAX_CHARS or SEND_MAX_SENTENCES) isn't sent
  by phone: it's saved to Drafts and the agent says "I left it in your Drafts to look
  over."
- reply_to (an inbox message id) replies to that message's sender with its subject
  ("Re: ..."); the given to and subject are replaced, and the read-back says so. The
  original's text is never returned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

from . import contacts_mac, mail_mac, messages_mac
from .mac import ToolError

log = logging.getLogger(__name__)

MAX_TO = 3
# A spoken message is short: a body over either limit goes to Drafts instead. 400
# characters is about four sentences of everyday mail (~100 each), half a minute aloud.
SEND_MAX_CHARS = 400
SEND_MAX_SENTENCES = 4
ONCE_S = 600.0  # the same message (to, subject, body) is sent or saved once in this long
LEFT_IN_DRAFTS = "I left it in your Drafts to look over."
NOT_BY_PHONE = {
    "cc": "No Cc by phone; put each person in to (at most 3), or leave it in Drafts with mail_draft.",
    "bcc": "No Bcc by phone; leave it in Drafts with mail_draft.",
    "attachments": "No attachments by phone.",
}
_SENTENCE = re.compile(r"[^.!?]*[A-Za-z0-9][^.!?]*(?:[.!?]+|$)")
_RE = re.compile(r"^\s*re\s*:", re.I)

_done: dict[str, tuple[float, str]] = {}  # message key -> (when, what was said)
_lock = threading.Lock()


def reset() -> None:
    """Forget what was sent and saved (tests)."""
    with _lock:
        _done.clear()


def sentences(body: str) -> int:
    return len([s for s in _SENTENCE.findall(body) if s.strip()])


def too_long(body: str) -> bool:
    """Longer than a few sentences: drafted, not sent, by phone."""
    return len(body.strip()) > SEND_MAX_CHARS or sentences(body) > SEND_MAX_SENTENCES


def _key(kind: str, m: dict[str, Any]) -> str:
    raw = json.dumps([kind, [a.lower() for a in m["to"]], m["subject"], m["body"]])
    return hashlib.sha256(raw.encode()).hexdigest()


def _seen(key: str) -> str | None:
    now = time.time()
    with _lock:
        for k in [k for k, (at, _) in _done.items() if now - at > ONCE_S]:
            del _done[k]
        got = _done.get(key)
    return got[1] if got else None


def _mark(key: str, text: str) -> None:
    with _lock:
        _done[key] = (time.time(), text)


def _limits(a: dict[str, Any]) -> None:
    for k, why in NOT_BY_PHONE.items():
        if a.get(k):
            raise ToolError(why)


def _to(a: dict[str, Any]) -> list[str]:
    v = a.get("to")
    items = [x.strip() for x in v.split(",")] if isinstance(v, str) else [str(x).strip() for x in v or []]
    items = [x for x in items if x]
    if not items:
        raise ToolError("to needs at least one person or email address")
    if len(items) > MAX_TO:
        raise ToolError(f"at most {MAX_TO} people by phone, each said by the user and read back")
    return items


async def _reply(a: dict[str, Any]) -> dict[str, Any]:
    """reply_to as its sender and "Re: <subject>"; only the headers are used."""
    m = await mail_mac.message(a.get("reply_to"), run=mail_mac._jxa)
    sender = mail_mac._address(str(m.get("sender") or ""))
    if not sender:
        raise ToolError("that message has no sender address to reply to")
    subject = str(m.get("subject") or "").strip()
    subject = subject if _RE.match(subject) else f"Re: {subject}".strip()
    return {**a, "to": [sender], "subject": subject}


async def _compose(a: dict[str, Any], resolve: bool) -> dict[str, Any]:
    """The phone's message, checked: {to, subject, body}. resolve: names become their
    address from Contacts (step one); otherwise to must be addresses (step two sends
    exactly what was read back)."""
    _limits(a)
    if a.get("reply_to") is not None:
        if not resolve:
            raise ToolError("a reply is read back first; send the to and subject it gave")
        a = await _reply(a)
    to = _to(a)
    if resolve:
        to = await contacts_mac.addresses_for(to)
    to = mail_mac.addresses(to, "to", required=True)
    to = list(dict.fromkeys(to))
    subject = mail_mac._clean(a.get("subject"), "subject", 200)
    body = mail_mac._clean(a.get("body"), "body", 20000)
    if not subject and not body:
        raise ToolError("give a subject or a body")
    return {"to": to, "subject": subject, "body": body}


async def unknown(addresses: list[str]) -> list[str]:
    """The addresses that are neither in Contacts nor ones the user has sent mail to."""
    names = {k.lower() for k in await contacts_mac.names_for(addresses)}
    out = []
    for a in addresses:
        if a.lower() in names:
            continue
        try:
            sent = int((await messages_mac.mail("mail_sent_to", {"address": a})).get("sent") or 0)
        except (ToolError, ValueError, TypeError) as exc:
            log.info("mail by phone: can't check sent mail: %s", exc)
            sent = 0
        if sent <= 0:
            out.append(a)
    return out


def _unknown_text(bad: list[str]) -> str:
    who = ", ".join(bad)
    return (
        f"Not sent: {who} isn't in the user's Contacts and isn't someone they've emailed before, "
        "so it can't be sent by phone. Offer to leave it in their Drafts instead (mail_draft)."
    )


async def _save(m: dict[str, Any]) -> str:
    """Saved in Drafts with no window, once per ONCE_S for the same message."""
    key = _key("draft", m)
    if (said := _seen(key)) is not None:
        return said
    to = await mail_mac.save_draft(m, run=mail_mac._jxa)
    said = f"Saved in the user's Drafts in Mail, to {', '.join(to)}; nothing was sent."
    _mark(key, said)
    return said


async def draft(a: dict[str, Any]) -> str:
    """mail_draft by phone: saved in Drafts, no window. To anyone (the user reviews it)."""
    m = await _compose(a, resolve=True)
    return f"{await _save(m)} Tell the user: {LEFT_IN_DRAFTS}"


async def _names(addresses: list[str]) -> str:
    named = {k.lower(): v for k, v in (await contacts_mac.names_for(addresses)).items()}
    return " and ".join(f"{a} ({named[a.lower()]})" if a.lower() in named else a for a in addresses)


async def preview(a: dict[str, Any]) -> dict[str, Any]:
    """Step one of mail_send by phone. {"status": "ok", "text": read-back, "args": what
    step two sends}; {"status": "ok", "final": True, "text"} when it went to Drafts
    instead (nothing to confirm); {"status": "error", "error"} when it can't go."""
    try:
        m = await _compose(a, resolve=True)
        if bad := await unknown(m["to"]):
            return {"status": "error", "error": _unknown_text(bad)}
        if too_long(m["body"]):
            saved = await _save(m)
            return {
                "status": "ok",
                "final": True,
                "text": (
                    f"Not sent: it's longer than a few sentences, which isn't sent by phone. {saved} "
                    f"Tell the user: {LEFT_IN_DRAFTS}"
                ),
            }
        who = await _names(m["to"])
    except ToolError as exc:
        return {"status": "error", "error": str(exc)}
    what = f"a reply to {who}" if a.get("reply_to") is not None else f"an email to {who}"
    text = (
        f"Send {what}, subject {m['subject'] or '(none)'!r}. The message: {m['body']!r}. "
        "Read the user each address exactly, the subject, and the message in one sentence."
    )
    return {"status": "ok", "text": text, "args": m}


async def send(a: dict[str, Any]) -> str:
    """Step two, after the caller's yes: sends what preview read back, at most once."""
    m = await _compose(a, resolve=False)
    if bad := await unknown(m["to"]):
        raise ToolError(_unknown_text(bad))
    if too_long(m["body"]):
        raise ToolError(f"too long to send by phone; leave it in Drafts with mail_draft ({LEFT_IN_DRAFTS})")
    key = _key("send", m)
    if _seen(key) is not None:
        raise ToolError(
            "already sent (or tried) in the last few minutes; not sent again. Check Sent in Mail."
        )
    # Marked before sending, and kept if Mail fails: Mail may have sent it anyway (a
    # timeout), and at most once matters more than a retry by phone.
    _mark(key, "sent")
    return await mail_mac.send(m, run=mail_mac._jxa)


# What run_shared_tool runs for the phone instead of the room's versions.
BY_PHONE = {"mail_draft": draft, "mail_send": send}
