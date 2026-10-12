"""Privacy views: a photos-only conversation in balanced mode, and strict mode's
searches, which must not reveal message text through which messages match.
Fake Mail, a fake local model and synthetic data only."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest

from scout import index, mac, mail_mac, mailindex, privacy

NOW = dt.datetime.now().astimezone()


@pytest.fixture(autouse=True)
def _no_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may reach osascript: any call is refused."""

    async def run(*argv: str, timeout: float = 10.0) -> str:
        raise AssertionError(f"osascript reached: {argv[:1]}")

    monkeypatch.setattr(mac, "_run", run)


class Model:
    ready = True

    async def complete(self, system: str, user: str, max_tokens: int) -> str:
        return "notice" if system == privacy.LABEL_SYSTEM else "a short summary"


# --- #28 ----------------------------------------------------------------------------------


def test_a_photos_only_conversation_does_not_crash_balanced_mode() -> None:
    data = {
        "conversation": "Sam",
        "messages": [
            {"id": 1, "text": "", "attachment": True, "date": "2026-10-01T10:00:00Z", "from_me": False,
             "sender": "Sam"},
        ],
    }  # fmt: skip
    out = asyncio.run(privacy.Policy("balanced", Model()).messages_view(data, "messages with Sam"))
    assert "(text withheld)" in out and "Summaries by" not in out


# --- #32 ----------------------------------------------------------------------------------


def test_strict_mail_search_does_not_match_message_text() -> None:
    """The index has a message whose text says 'biopsy' (subject 'Results'); a strict
    search for 'biopsy' must not find it, or Claude learns what the text says."""
    msg = {
        "id": 41,
        "date": NOW.isoformat(),
        "sender": "Clinic <c@example.com>",
        "subject": "Results",
        "read": False,
    }
    mailindex.remember([{**msg, "body": "Your biopsy came back."}], with_body=True)
    assert mailindex.search("biopsy", 5, 30) is not None  # balanced and open still search the text
    asked: list[str] = []

    async def run(script: str, *args: str) -> str:
        assert script == mail_mac._LIST
        asked.append(args[2])  # the query: Mail matches subjects and senders
        return json.dumps({"messages": []})

    pol = privacy.Policy("strict", Model())
    out = asyncio.run(pol.mail_list(5, query="biopsy", run=run))
    assert asked == ["biopsy"] and "id 41" not in out
    assert index.default().get("mail:41") is not None


def test_strict_messages_text_search_withholds_the_matches() -> None:
    data = {
        "messages": [
            {"id": 7, "text": "your password reset code is 1234", "date": "2026-10-01T10:00:00Z",
             "from_me": False, "sender": "+15555550100"},
        ],
        "scanned": 40, "scan_capped": False, "since_days": 90,
    }  # fmt: skip
    out = asyncio.run(privacy.Policy("strict").messages_view(data, "messages containing 'reset code'"))
    assert out == privacy.STRICT_SEARCH
    assert "5550100" not in out and "1234" not in out
    plain = {k: v for k, v in data.items() if k not in ("scanned", "scan_capped")}
    shown = asyncio.run(privacy.Policy("strict").messages_view(plain, "recent messages"))
    assert shown.startswith(privacy.STRICT_MESSAGES)  # other lookups still show who and when
