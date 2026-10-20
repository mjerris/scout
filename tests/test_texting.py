"""Texting and calling someone (messages_send.py, calls.py): who a name means (Contacts,
faked at contacts_mac.search_data), which handle and service, the read-back, sending at
most once and only after a yes (room, other sessions, the phone's two steps), and the
call prompt on the Mac or a tappable link by phone. The send and call boundaries
(messages_send.SEND, calls.OPEN) are fakes: nothing is sent and no call is placed."""

from __future__ import annotations

from typing import Any

import pytest
from test_routing import make, run

from scout import (
    brain,
    calls,
    contacts_mac,
    messages_send,
    phone_service,
    refs,
    reminders_mac,
    shared_tools,
    trust,
)
from scout.assistant import PHONE
from scout.mac import ToolError

PEOPLE = [
    {"id": "c1", "name": "Pat Doe", "organization": "Acme", "score": 0,
     "phones": [{"label": "work", "value": "555-010-3000"}, {"label": "mobile", "value": "+1 (555) 010-2000"}],
     "emails": [{"label": "home", "value": "pat@doe.example"}]},
    {"id": "c2", "name": "Patricia Moss", "score": 1, "phones": [{"label": "home fax", "value": "555 010 4999"},
     {"label": "home", "value": "555 010 4000"}], "emails": []},
    {"id": "c3", "name": "Sam Roe", "score": 0, "phones": [], "emails": [{"label": "work", "value": "sam@corp.example"}]},
    {"id": "c4", "name": "Nobody Reachable", "score": 0, "phones": [], "emails": []},
    {"id": "c5", "name": "Pat Lee", "score": 0, "phones": [{"label": "iPhone", "value": "555 010 5000"}], "emails": []},
]  # fmt: skip


async def fake_search(query: Any, limit: Any = None, group: Any = None, helper: Any = None) -> dict[str, Any]:
    q = str(query).lower()
    found = [p for p in PEOPLE if any(w.startswith(q.split()[0]) for w in str(p["name"]).lower().split())]
    if " " in q:
        found = [p for p in found if str(p["name"]).lower() == q] or found
    return {"contacts": found, "total": len(found)}


class Sends:
    def __init__(self, answer: str = "sent", error: str = "") -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.answer, self.error = answer, error

    async def __call__(self, handle: str, service: str, text: str) -> str:
        self.sent.append((handle, service, text))
        if self.error:
            raise ToolError(self.error)
        return self.answer


@pytest.fixture
def texts(monkeypatch: pytest.MonkeyPatch) -> Sends:
    s = Sends()
    monkeypatch.setattr(contacts_mac, "search_data", fake_search)
    monkeypatch.setattr(messages_send, "SEND", s)
    monkeypatch.setattr(messages_send, "_drafts", {})

    async def no_names(handles: list[str], helper: Any = None) -> dict[str, str]:
        return {}

    async def never_seen(handle: str) -> None:
        return None

    monkeypatch.setattr(contacts_mac, "names_for", no_names)
    monkeypatch.setattr(messages_send, "last_seen", never_seen)
    return s


def draft_id(out: str) -> str:
    return out.split()[1].rstrip(":")


# --- handles ------------------------------------------------------------------------------


def test_numbers_to_dial_and_which_phone_first() -> None:
    assert contacts_mac.dial_number("+1 (555) 010-2000") == "+15550102000"
    assert contacts_mac.dial_number("555-010-3000") == "+15550103000"
    assert contacts_mac.dial_number("1 555 010 3000") == "+15550103000"
    assert contacts_mac.dial_number("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(ToolError):
        contacts_mac.dial_number("call me")
    assert [n for n, _ in contacts_mac.phone_choices(PEOPLE[0])] == ["+1 (555) 010-2000", "555-010-3000"]
    assert contacts_mac.phone_choices(PEOPLE[1]) == [("555 010 4000", "home")]  # no fax


def test_the_handle_with_the_newest_conversation_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {
        "+15550103000": ("2026-10-01T10:00:00-04:00", "SMS"),
        "pat@doe.example": ("2026-10-14T09:00:00-04:00", "iMessage"),
        "+15550102000": ("2026-09-01T10:00:00-04:00", "iMessage"),
    }

    async def last_seen(handle: str) -> tuple[str, str] | None:
        return seen.get(handle)

    monkeypatch.setattr(messages_send, "last_seen", last_seen)
    assert run(messages_send.pick_handle(PEOPLE[0])) == ("pat@doe.example", "iMessage", "home")
    del seen["pat@doe.example"]
    assert run(messages_send.pick_handle(PEOPLE[0])) == ("+15550103000", "SMS", "work")
    seen.clear()  # no history: the mobile number, over iMessage
    assert run(messages_send.pick_handle(PEOPLE[0])) == ("+15550102000", "iMessage", "mobile")
    assert run(messages_send.pick_handle(PEOPLE[2])) == ("sam@corp.example", "iMessage", "work")
    seen["+15550104000"] = ("2026-10-02T10:00:00-04:00", "RCS")  # RCS goes out as SMS via the iPhone
    assert run(messages_send.pick_handle(PEOPLE[1])) == ("+15550104000", "SMS", "home")
    with pytest.raises(contacts_mac.Unclear, match="no phone number or email"):
        run(messages_send.pick_handle(PEOPLE[3]))


def test_last_seen_never_starts_the_messages_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import messages_mac

    asked: list[tuple[str, dict[str, Any]]] = []

    async def helper(h: Any, op: str, args: dict[str, Any]) -> dict[str, Any]:
        asked.append((op, args))
        if args["chat"] == "x@y.example":
            return {"ok": False, "code": "no_access"}
        return {"ok": True, "messages": [{"date": "2026-10-14T09:00:00", "service": "SMS"}]}

    monkeypatch.setattr(messages_mac, "_send", helper)
    monkeypatch.setattr(messages_mac, "_start_setup", lambda h: pytest.fail("setup started"))
    assert run(messages_send.last_seen("+15550102000")) == ("2026-10-14T09:00:00", "SMS")
    assert run(messages_send.last_seen("x@y.example")) is None
    assert asked[0] == ("chat", {"chat": "+15550102000", "limit": 1, "since_days": 365})


# --- texting ------------------------------------------------------------------------------


def test_draft_reads_back_and_sends_once(texts: Sends) -> None:
    out = run(messages_send.draft({"to": "Pat Doe", "text": "I'm running late"}))
    assert (
        out.startswith("Draft m") and "text Pat Doe (mobile +15550102000, iMessage): I'm running late" in out
    )
    assert texts.sent == []  # a draft sends nothing
    d = draft_id(out)
    spoken, detail = brain.describe_tool("mcp__voice_app__message_send", {"draft": d})
    assert spoken == "Send the text to Pat Doe?\nIt says: I'm running late."
    assert detail.startswith("send text to Pat Doe (mobile +15550102000, iMessage)")
    assert run(messages_send.send({"draft": d})) == "Sent to Pat Doe (+15550102000) in Messages."
    assert texts.sent == [("+15550102000", "iMessage", "I'm running late")]
    with pytest.raises(ToolError, match="no such draft"):
        run(messages_send.send({"draft": d}))  # once
    assert len(texts.sent) == 1


def test_a_name_that_fits_several_asks_which(texts: Sends) -> None:
    out = run(messages_send.draft({"to": "Pat", "text": "hi"}))
    assert "Pat Doe (Acme); Patricia Moss; Pat Lee" in out and "Ask the user which one" in out
    assert "Draft" not in out and messages_send._drafts == {}
    with pytest.raises(ToolError, match="no Zed in Contacts"):
        run(messages_send.draft({"to": "Zed", "text": "hi"}))
    with pytest.raises(ToolError, match="text is required"):
        run(messages_send.draft({"to": "Pat Doe", "text": "  "}))
    with pytest.raises(ToolError, match="control characters"):
        run(messages_send.draft({"to": "Pat Doe", "text": "a\x07b"}))


def test_a_number_or_address_is_texted_as_given(texts: Sends) -> None:
    out = run(messages_send.draft({"to": "(555) 010-7777", "text": "on my way"}))
    assert "text +15550107777 (phone +15550107777, iMessage): on my way" in out
    out = run(messages_send.draft({"to": "kim@example.com", "text": "hey"}))
    assert "text kim@example.com (email kim@example.com, iMessage)" in out


def test_nothing_sent_lets_it_be_sent_again_but_a_maybe_doesnt(texts: Sends) -> None:
    d = draft_id(run(messages_send.draft({"to": "Pat Doe", "text": "hi"})))
    texts.answer = "notsent:-1743:Not authorized to send Apple events to Messages."
    with pytest.raises(ToolError, match="Not allowed to control Messages yet"):
        run(messages_send.send({"draft": d}))
    texts.answer = "notsent:-1728:Can't get account"
    with pytest.raises(ToolError, match="Nothing was sent"):
        run(messages_send.send({"draft": d}))
    assert messages_send._draft({"draft": d}) is not None  # still a draft
    texts.answer, texts.error = "", "osascript timed out"
    with pytest.raises(ToolError, match="may have been sent"):
        run(messages_send.send({"draft": d}))
    assert messages_send._draft({"draft": d}) is None  # never twice
    texts.error = "execution error: Not authorized to send Apple events (-1743)"
    d2 = draft_id(run(messages_send.draft({"to": "Pat Doe", "text": "hi again"})))
    with pytest.raises(ToolError, match="Not allowed"):
        run(messages_send.send({"draft": d2}))
    assert messages_send._draft({"draft": d2}) is not None


def test_the_script_is_constant_and_takes_arguments() -> None:
    s = messages_send.SEND_SCRIPT
    assert "on run argv" in s and "send theText to theBuddy" in s
    assert s.index("on error") < s.index("send theText")  # nothing can fail silently after send
    with pytest.raises(RuntimeError, match="must not send"):
        run(messages_send._osascript("+15550102000", "iMessage", "x"))


def test_send_is_confirmed_by_voice_and_never_promoted(texts: Sends, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shared_tools.BY_NAME["message_send"].asks and not shared_tools.BY_NAME["message_draft"].asks
    assert "mcp__voice_app__message_send" in brain.ALWAYS_ASK_TOOLS
    assert (
        trust.default().promote("message_send") == "Sending texts always asks; that one can't be turned off."
    )
    assert trust.action_for("sending texts") == "message_send"

    async def go(answer: bool | None) -> tuple[dict[str, Any], list[str]]:
        r = make()
        asked: list[str] = []

        async def confirm(spoken: str, *rest: Any, **kw: Any) -> bool | None:
            asked.append(spoken)
            return answer

        monkeypatch.setattr(r.a, "_confirm", confirm)
        d = draft_id(
            (await r.a.run_shared_tool("desk#1", "message_draft", {"to": "Pat Doe", "text": "late"}))["text"]
        )
        return await r.a.run_shared_tool("desk#1", "message_send", {"draft": d}), asked

    no, asked = run(go(False))
    assert no["status"] == "declined" and texts.sent == []
    assert asked == ["From desk: Send the text to Pat Doe?\nIt says: late."]
    yes, _ = run(go(True))
    assert yes["status"] == "ok" and texts.sent == [("+15550102000", "iMessage", "late")]


def test_texting_from_the_phone_in_two_steps(texts: Sends) -> None:
    assert "message_send" in {t.name for t in phone_service.PHONE_ACTIONS}
    assert "message_draft" in {t.name for t in phone_service.PHONE_TOOLS}

    async def go() -> list[dict[str, Any]]:
        r = make()
        d = draft_id(
            (await r.a.run_shared_tool(PHONE, "message_draft", {"to": "Pat Doe", "text": "late"}))["text"]
        )
        refused = await r.a.run_shared_tool(PHONE, "message_send", {"draft": d})  # no yes yet
        assert texts.sent == []
        return [
            refused,
            await r.a.run_shared_tool(PHONE, "message_send", {"draft": d}, preview=True),
            await r.a.run_shared_tool(PHONE, "message_send", {"draft": d}, confirmed=True),
            await r.a.run_shared_tool(PHONE, "message_send", {"draft": d}, confirmed=True),
        ]

    refused, preview, done, again = run(go())
    assert refused["status"] == "error" and "phone" in refused["error"]
    assert preview == {"status": "ok", "text": "Text Pat Doe: late?"}
    assert done["status"] == "ok" and again["status"] == "error"
    assert texts.sent == [("+15550102000", "iMessage", "late")]


# --- calling -------------------------------------------------------------------------------


class Opens:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str) -> None:
        self.urls.append(url)


@pytest.fixture
def dial(texts: Sends, monkeypatch: pytest.MonkeyPatch) -> Opens:
    o = Opens()
    monkeypatch.setattr(calls, "OPEN", o)
    monkeypatch.setattr(calls, "_calls", {})
    monkeypatch.setattr(calls, "_linked", {})
    return o


def call_id(out: str) -> str:
    return out.split()[1].rstrip(":")


def test_call_names_the_number_and_starts_once_after_a_yes(
    dial: Opens, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = run(calls.prepare({"to": "Pat Doe"}))
    assert out.startswith("Call c") and "Pat Doe, mobile, +15550102000. Not started." in out
    assert dial.urls == []
    c = call_id(out)
    spoken, detail = brain.describe_tool("mcp__voice_app__call_start", {"call": c})
    assert spoken == "Call Pat Doe at five five five, oh one oh, two oh oh oh, mobile?"
    assert detail.endswith("tel:+15550102000")
    assert "Calling Pat Doe" in run(calls.start({"call": c}))
    assert dial.urls == ["tel:+15550102000"]
    with pytest.raises(ToolError, match="no such call"):
        run(calls.start({"call": c}))
    assert len(dial.urls) == 1
    assert "Ask the user which one" in run(calls.prepare({"to": "Pat"}))
    sam = call_id(run(calls.prepare({"to": "Sam Roe"})))  # no number: FaceTime audio
    run(calls.start({"call": sam}))
    assert dial.urls[-1] == "facetime-audio:sam@corp.example"
    with pytest.raises(ToolError, match="no phone number"):
        run(calls.prepare({"to": "Nobody Reachable"}))
    with pytest.raises(RuntimeError, match="must not place calls"):
        run(calls._open("tel:+15550102000"))


def test_call_start_asks_in_the_room_and_never_runs_from_the_phone(
    dial: Opens, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert shared_tools.BY_NAME["call_start"].asks and "mcp__voice_app__call_start" in brain.ALWAYS_ASK_TOOLS
    assert "call_start" not in {t.name for t in (*phone_service.PHONE_ACTIONS, *phone_service.PHONE_TOOLS)}
    assert {"call_prepare", "call_link"} <= {t.name for t in phone_service.PHONE_TOOLS}
    assert (
        trust.default().promote("call_start") == "Starting calls always asks; that one can't be turned off."
    )

    async def go() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        r = make()
        asked: list[str] = []

        async def confirm(spoken: str, *rest: Any, **kw: Any) -> bool | None:
            asked.append(spoken)
            return True

        monkeypatch.setattr(r.a, "_confirm", confirm)
        c = call_id((await r.a.run_shared_tool("desk#1", "call_prepare", {"to": "Pat Doe"}))["text"])
        by_phone = await r.a.run_shared_tool(PHONE, "call_start", {"call": c}, confirmed=True)
        return by_phone, await r.a.run_shared_tool("desk#1", "call_start", {"call": c}), asked

    by_phone, room, asked = run(go())
    assert by_phone["status"] == "error" and "phone" in by_phone["error"]
    assert room["status"] == "ok" and asked == [
        "From desk: Call Pat Doe at five five five, oh one oh, two oh oh oh, mobile?"
    ]
    assert dial.urls == ["tel:+15550102000"]


def test_by_phone_the_number_goes_to_the_reminders_as_a_link(
    dial: Opens, monkeypatch: pytest.MonkeyPatch
) -> None:
    added: list[dict[str, Any]] = []

    async def add(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        reminders_mac._url_arg(a.get("url"))  # the URL check the real helper call makes
        added.append({**a, "mirror": mirror})
        return {"id": "r1"}

    monkeypatch.setattr(reminders_mac, "add_data", add)
    c = call_id(run(calls.prepare({"to": "Pat Doe"})))
    assert (
        run(calls.link({"call": c}))
        == "I put Pat Doe's number in your Scout reminders: tap it there to call."
    )
    assert added == [
        {"title": "Call Pat Doe", "list": refs.LINK_LIST, "url": "tel:+15550102000", "mirror": True}
    ]
    assert "already" in run(calls.link({"call": c})) and len(added) == 1
    assert dial.urls == []  # nothing dialled on the Mac


def test_reminder_urls_take_numbers_to_call_and_nothing_else() -> None:
    assert reminders_mac._url_arg("tel:+15550102000") == "tel:+15550102000"
    assert reminders_mac._url_arg("facetime-audio:sam@corp.example")
    for bad in ("tel:+1 555", "javascript:alert(1)", "tel:", "facetime-audio://x/y", "file:///etc"):
        with pytest.raises(ToolError):
            reminders_mac._url_arg(bad)


# --- tier 0: "text Pat ..." and "call Pat" in the room (local_intents.py) -------------------


class Acts:
    """Context.act stand-in: records the asking tool it was handed, runs nothing."""

    def __init__(self, status: str = "ok") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status = status

    async def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"status": self.status, "text": "done", "error": "Messages said no"}


def _ctx(act: Acts | None) -> Any:
    from scout import local_intents
    from scout.tools import Timers

    return local_intents.Context(Timers(lambda label: None), act=act)


def _tier0(text: str, act: Acts | None, channel: str = "room") -> str | None:
    from scout import local_intents

    skip = local_intents.PHONE_SKIP if channel == PHONE else frozenset()
    out: str | None = run(local_intents.answer(text, _ctx(act), skip=skip, channel=channel))
    return out


def _drafted(act: Acts) -> list[tuple[str, str]]:
    """(who, text) of each draft the tier-0 sends were for."""
    out = []
    for name, args in act.calls:
        assert name == "message_send"
        d = messages_send._drafts[args["draft"]]
        out.append((d.name, d.text))
    return out


@pytest.mark.parametrize(
    ("said", "who", "text"),
    [
        ("Text Pat Doe I'm running late.", "Pat Doe", "I'm running late."),
        ("text pat lee: on my way, ten minutes", "Pat Lee", "on my way, ten minutes"),
        ("Can you text Sam saying see you at 5?", "Sam Roe", "see you at 5?"),
        ("message Pat Lee are we still on for lunch", "Pat Lee", "are we still on for lunch"),
        ("Please send a text to Sam Roe saying the gate code is 4 4 1 2", "Sam Roe", "the gate code is 4 4 1 2"),
        ("send a message to Pat Doe to say I'll call back", "Pat Doe", "I'll call back"),
    ],
)  # fmt: skip
def test_text_someone_drafts_it_and_sends_through_the_asking_tool(
    texts: Sends, said: str, who: str, text: str
) -> None:
    act = Acts()
    assert _tier0(said, act) == f"Sent to {who}."
    assert _drafted(act) == [(who, text)]
    assert texts.sent == []  # only message_send sends, and the fake act ran nothing


def test_call_someone_prepares_it_and_starts_through_the_asking_tool(dial: Opens) -> None:
    for said, who in (("Call Pat Doe", "Pat Doe"), ("can you call sam please", "Sam Roe"),
                      ("give Pat Lee a call", "Pat Lee")):  # fmt: skip
        act = Acts()
        assert _tier0(said, act) == f"Calling {who}."
        [(name, args)] = act.calls
        assert name == "call_start" and calls._calls[args["call"]].name == who
    assert dial.urls == []


def test_a_no_or_no_answer_does_nothing_and_says_so(texts: Sends, dial: Opens) -> None:
    assert _tier0("text Pat Doe hi", Acts("declined")) == "Okay, not sent."
    assert _tier0("call Pat Doe", Acts("declined")) == "Okay, not calling."
    assert _tier0("text Pat Doe hi", Acts("no_answer")) == ""  # "No answer, so I skipped that." was said
    assert _tier0("call Pat Doe", Acts("no_answer")) == ""
    assert _tier0("text Pat Doe hi", Acts("error")) == "Sorry, Messages said no"


def test_a_name_that_fits_several_asks_which_and_nobody_goes_to_claude(texts: Sends, dial: Opens) -> None:
    act = Acts()
    which = "3 people in your contacts match Pat: Pat Doe (Acme), Patricia Moss, and Pat Lee. Which one?"
    assert _tier0("text Pat I'm late", act) == which
    assert _tier0("call Pat", act) == which
    assert _tier0("text Zed hello there", act) is None  # nobody in Contacts: Claude takes it
    assert _tier0("call Zed", act) is None
    with pytest.raises(ToolError, match="Nobody Reachable has no phone number"):  # the room says "Sorry, ..."
        _tier0("call Nobody Reachable", act)
    assert act.calls == [] and messages_send._drafts == {} and calls._calls == {}


@pytest.mark.parametrize(
    "said",
    [
        "text Pat Doe",  # nothing to say: Claude asks what
        "message Pat Doe on Slack saying hi",  # Slack, not Messages
        "text Pat Doe on WhatsApp running late",
        "text me the link",
        "text messages from Pat Doe",
        "message from Pat Doe",
        "what did Pat Doe text me",
        "call it a day",
        "call me Mike",
        "call Pat Doe tomorrow",  # more than a name: Claude takes it
        "call Pat Doe on her cell",
        "call the restaurant",
        "call 555 010 2000",
        "call an uber",
        "calling Pat Doe",
        "who called",
        "remind me to call Pat Doe",
        "what's Pat Doe's number",  # still the number, not a call
    ],
)
def test_only_clear_text_and_call_requests_are_taken(texts: Sends, dial: Opens, said: str) -> None:
    act = Acts()
    out = _tier0(said, act)
    assert act.calls == [] and messages_send._drafts == {} and calls._calls == {}
    assert out is None or "number" in out


def test_the_phone_and_an_unwired_context_leave_it_to_claude(texts: Sends, dial: Opens) -> None:
    act = Acts()
    assert _tier0("text Pat Doe I'm late", act, channel=PHONE) is None
    assert _tier0("call Pat Doe", act, channel=PHONE) is None
    assert _tier0("call Pat Doe", None) is None and _tier0("text Pat Doe hi", None) is None
    assert act.calls == []


def test_the_room_reads_the_text_back_and_sends_only_on_a_spoken_yes(texts: Sends) -> None:
    import asyncio
    import time

    from test_routing import hear

    async def go(answer: str) -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("text Pat Doe I'm running late"))
        await asyncio.sleep(0.02)
        await hear(r, answer, started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go("no"))
    assert said[:2] == ["Send the text to Pat Doe?", "It says: I'm running late."]
    assert said[-1] == "Okay, not sent." and texts.sent == []
    said = run(go("yes"))
    assert said[-1] == "Sent to Pat Doe." and texts.sent == [("+15550102000", "iMessage", "I'm running late")]


def test_the_room_names_the_number_and_calls_only_on_a_spoken_yes(dial: Opens) -> None:
    import asyncio
    import time

    from test_routing import hear

    async def go(answer: str) -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("call Pat Doe"))
        await asyncio.sleep(0.02)
        await hear(r, answer, started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go("no"))
    assert said[0] == "Call Pat Doe at five five five, oh one oh, two oh oh oh, mobile?"
    assert said[-1] == "Okay, not calling." and dial.urls == []
    said = run(go("yes"))
    assert said[-1] == "Calling Pat Doe." and dial.urls == ["tel:+15550102000"]


def test_words_that_arent_people_never_reach_contacts(texts: Sends, dial: Opens) -> None:
    """Contacts' matching is fuzzy ("me" starts Melissa), so these never get that far: here
    anyone at all is in Contacts under exactly what was said."""
    from scout import local_intents
    from scout.tools import Timers

    asked: list[str] = []

    async def anyone(who: str) -> list[dict[str, Any]]:
        asked.append(who)
        return [{"id": "x", "name": who.title(), "phones": [{"label": "mobile", "value": "555 010 9999"}]}]

    act = Acts()
    c = local_intents.Context(Timers(lambda label: None), contacts=anyone, act=act)
    for said in ("call it a day", "call me Mike", "call him back", "call them", "call off the meeting",
                 "call up", "call an uber", "text me when it's done", "message them that I'm late",
                 "text everyone I'm late", "message from Pat Doe", "text back I'm late",
                 "call the office at the main number please", "call pat doe at the main office",
                 "call pat doe and sam roe"):  # fmt: skip
        assert run(local_intents.answer(said, c)) is None, said
    assert asked == [] and act.calls == []
    assert run(local_intents.answer("call pat doe", c)) == "Calling Pat Doe."  # the fake does answer

    async def twins(who: str) -> list[dict[str, Any]]:
        return [{"id": "a", "name": "Pat Doe", "organization": "Acme"}, {"id": "b", "name": "Pat Doe"}]

    c = local_intents.Context(Timers(lambda label: None), contacts=twins, act=act)
    two = "2 people in your contacts match Pat Doe: Pat Doe (Acme) and Pat Doe. Which one?"
    assert run(local_intents.answer("call pat doe", c)) == two
    assert run(local_intents.answer("text pat doe see you soon", c)) == two
    assert run(local_intents.answer("text pat doe", c)) is None  # a name, and nothing to say
    assert [name for name, _ in act.calls] == ["call_start"]  # only the one call above
