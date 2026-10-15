"""Per-action trust: actions that change something start by asking every time, and the
user can promote one specific action to "just do it" once it has earned trust, and
take it back. Never a global switch.

    ask   -> confirmed by voice every time (the default)
    auto  -> done without asking (promoted by the user, logged)

Sending email, a text or a Slack message, or starting a call, can't be promoted: it leaves the Mac under the user's name. A "yes,
always" to a confirmation promotes that action; after OFFER_AFTER yeses in a row Scout
mentions it once. Every decision is kept in state/trust.json's action log.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA

log = logging.getLogger(__name__)

PATH = DATA / "state" / "trust.json"
OFFER_AFTER = 5
LOG_KEEP = 500

# Actions that can be promoted, by their shared-tool name, with how they're said.
PROMOTABLE = {
    "reminder_add": "adding reminders",
    "reminder_complete": "marking reminders done",
    "calendar_create_event": "adding calendar events",
    "note_delete": "deleting notes",
}
NEVER = {
    "mail_send": "sending email",
    "slack_send": "sending Slack messages",
    "message_send": "sending texts",
    "call_start": "starting calls",
}

# How people name them in speech.
_SAID = [
    (r"(?:adding |add |new )?reminders?(?: adds?)?", "reminder_add"),
    (r"(?:marking |mark |completing |complete |finishing |checking off )reminders?(?: (?:done|off|complete))?", "reminder_complete"),
    (r"(?:adding |add |creating |new )?(?:calendar )?(?:events?|appointments?|meetings?)(?: to (?:my |the )?calendar)?|calendar", "calendar_create_event"),
    (r"(?:deleting |delete |removing )notes?", "note_delete"),
    (r"(?:sending |send )?(?:e-?mails?|mail)", "mail_send"),
    (r"(?:sending |send )?slack(?: messages?)?", "slack_send"),
    (r"(?:sending |send )?(?:texts?|text messages?|imessages?|sms)", "message_send"),
    (r"(?:starting |start |making |placing )?(?:phone )?calls?|calling(?: people)?", "call_start"),
]  # fmt: skip


def base(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def action_for(phrase: str) -> str | None:
    p = " ".join(phrase.lower().split()).strip(" .?!")
    p = re.sub(r"^(?:the |my |me )", "", p)
    for pattern, name in _SAID:
        if re.fullmatch(pattern, p):
            return name
    return None


class Trust:
    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.tools: dict[str, dict[str, Any]] = {}
        self.log: list[dict[str, Any]] = []
        try:
            data = json.loads(path.read_text())
            self.tools = dict(data.get("tools") or {})
            self.log = list(data.get("log") or [])
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("trust file unreadable (%s); everything asks", exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"tools": self.tools, "log": self.log[-LOG_KEEP:]}, indent=1))
        tmp.replace(self.path)

    def tier(self, name: str) -> str:
        b = base(name)
        if b not in PROMOTABLE:
            return "ask"
        return str(self.tools.get(b, {}).get("tier", "ask"))

    def record(self, name: str, how: str, ok: bool | None, detail: str = "", by: str = "") -> str | None:
        """Log one decision (how: asked or auto). Returns a one-time offer to stop asking,
        when the user has said yes OFFER_AFTER times in a row."""
        b = base(name)
        with self._lock:
            self.log.append(
                {"ts": time.time(), "tool": b, "how": how, "ok": ok, "detail": detail[:200], "by": by}
            )
            offer = None
            if b in PROMOTABLE and how == "asked":
                t = self.tools.setdefault(b, {"tier": "ask"})
                t["streak"] = int(t.get("streak", 0)) + 1 if ok else 0
                if t["streak"] >= OFFER_AFTER and t.get("tier") == "ask" and not t.get("offered"):
                    t["offered"] = time.time()
                    offer = (
                        f"You've okayed {PROMOTABLE[b]} {t['streak']} times in a row. If you'd like, say "
                        f"'always allow {PROMOTABLE[b]}' and I'll stop asking."
                    )
            self._save()
        return offer

    def promote(self, name: str) -> str:
        b = base(name)
        if b in NEVER:
            return f"{NEVER[b][0].upper()}{NEVER[b][1:]} always asks; that one can't be turned off."
        if b not in PROMOTABLE:
            return "That kind of action always asks."
        with self._lock:
            self.tools.setdefault(b, {})["tier"] = "auto"
            self.tools[b]["promoted_at"] = time.time()
            self.log.append({"ts": time.time(), "tool": b, "how": "promoted", "ok": True})
            self._save()
        return f"Okay, I'll go ahead with {PROMOTABLE[b]} without asking. Say 'ask me before {PROMOTABLE[b]}' to undo that."

    def demote(self, name: str) -> str:
        b = base(name)
        label = PROMOTABLE.get(b) or NEVER.get(b) or "that"
        with self._lock:
            if b in self.tools:
                self.tools[b].update(tier="ask", streak=0)
                self.tools[b].pop("offered", None)
            self.log.append({"ts": time.time(), "tool": b, "how": "demoted", "ok": True})
            self._save()
        return f"Okay, I'll ask before {label} again."

    def auto_actions(self) -> list[str]:
        return [PROMOTABLE[b] for b, t in self.tools.items() if t.get("tier") == "auto" and b in PROMOTABLE]

    def speak_status(self) -> str:
        auto = self.auto_actions()
        if not auto:
            return "Everything that changes something asks you first."
        return "I go ahead without asking for " + ", ".join(auto) + ". Everything else asks first."


_trust: Trust | None = None


def default() -> Trust:
    global _trust
    if _trust is None:
        _trust = Trust(PATH)
    return _trust
