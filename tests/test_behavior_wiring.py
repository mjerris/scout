"""Behavior rules in every session and every way of making one: the room (tier 0 and its
Claude session), the phone agent, desk sessions (MCP), the store's records and links,
the briefing's offers and the monthly review. Synthetic data, fakes only."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from typing import Any

import pytest
from test_phone import ADDRESS, AUTH, KEY, PUBLIC, FakeScout, _ai, uvars
from test_routing import TONE, hear, make, run

import scout.phone_service as ps
from scout import behavior, brain, corrections, index, links, phone_devices, shared_tools
from scout.brain import Brain
from scout.config import ClaudeConfig, PhoneConfig
from scout.phone_devices import CallCheck
from scout.rules import Rules
from scout.tools import Timers, build_server


def rules() -> behavior.Behavior:
    return behavior.default()


# --- the room's Claude session ------------------------------------------------------------


async def never(spoken: str, detail: str) -> bool | None:
    return None


def room_brain(tmp_path: Path) -> Brain:
    server, names = build_server(Timers(lambda label: None))
    return Brain(ClaudeConfig(), never, server, names, Rules(tmp_path / "r.json"), lambda k, t: None)


def _append(opts: Any) -> str:
    return str(opts.system_prompt["append"])


def test_room_session_prompt_carries_the_rules_read_fresh(tmp_path: Path) -> None:
    b = room_brain(tmp_path)
    assert "rules for how you behave" not in _append(b._options())
    rules().add("Call me Mike")
    rules().add("Read ticket numbers digit by digit", enforced="speech.speak_ids")
    text = _append(b._options())
    assert "- Call me Mike" in text and "ticket numbers" not in text  # enforced: not injected


class FakeClient:
    def __init__(self, opts: Any) -> None:
        self.opts = opts
        self.sent: list[str] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, text: str) -> None:
        self.sent.append(text)

    async def receive_response(self) -> Any:
        if False:
            yield None


def test_a_changed_file_reaches_a_running_session_without_restarting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brain, "ClaudeSDKClient", FakeClient)
    b = room_brain(tmp_path)

    async def ask(text: str) -> None:
        async for _ in b.ask(text):
            pass

    async def go() -> list[str]:
        await ask("what's up")
        client = b.client
        time.sleep(0.01)  # a new mtime
        rules().add("Call me Mike")
        await ask("and now")
        await ask("and again")
        assert b.client is client  # same session: the rules came with the request
        return list(client.sent)  # type: ignore[union-attr]

    sent = run(go())
    assert sent[0] == "what's up"
    assert sent[1].startswith("(Your rules for how to behave changed") and "- Call me Mike" in sent[1]
    assert sent[1].endswith("and now") and sent[2] == "and again"


# --- tier 0 in the room: "from now on ..." -------------------------------------------------


def test_from_now_on_reads_the_rule_back_and_keeps_it_on_yes() -> None:
    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("from now on, call me Mike"))
        await asyncio.sleep(0.02)
        await hear(r, "yes", started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go())
    assert said[0] == "Rule: call you Mike. Keep it?"
    assert said[-1] == TONE  # kept: the confirm sound says so
    [r] = rules().rules()
    assert r.text == "Call me Mike" and r.origin == "told"


def test_no_keeps_nothing() -> None:
    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("always read emoji names out loud"))
        await asyncio.sleep(0.02)
        await hear(r, "no", started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go())
    assert said[0] == "Rule: always read emoji names out loud. Keep it?" and said[-1] == "Okay, not a rule."
    assert rules().rules() == []


def test_a_one_off_never_asks_about_a_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> bool:
        r = make()
        return await r.a._answer_locally("don't forget the milk")

    run(go())
    assert rules().rules() == []


def test_what_are_my_rules_is_answered_locally() -> None:
    rules().add("Call me Mike")

    async def go() -> list[str]:
        r = make()
        assert await r.a._answer_locally("what are my rules")
        return r.spk.said

    assert run(go())[-1].startswith("You have 1 rule: call you Mike")


# --- the shared tools: desk sessions ask the room; the phone in two steps ------------------


def _answer_after(r: Any, task: asyncio.Task[Any], words: str) -> Any:
    async def go() -> Any:
        await asyncio.sleep(0.02)
        await hear(r, words, started=time.monotonic())
        return await task

    return go()


def test_a_desk_sessions_rule_add_is_asked_in_the_room() -> None:
    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        t = asyncio.create_task(r.a.run_shared_tool("proj#7", "rule_add", {"rule": "Never use emoji"}))
        return await _answer_after(r, t, "yes"), r.spk.said

    out, said = run(go())
    assert said[0] == "From proj: Rule: never use emoji. Keep it?"
    assert (
        out["status"] == "ok"
        and "Kept the rule" in out["text"]
        and behavior.rule_id("Never use emoji") in out["text"]
    )
    assert [x.text for x in rules().rules()] == ["Never use emoji"]


def test_rule_drop_reads_back_what_it_would_drop() -> None:
    rules().add("Never use emoji")

    async def go() -> dict[str, Any]:
        r = make()
        return await r.a.run_shared_tool(ps.AGENT, "rule_drop", {"about": "emoji"}, preview=True)

    assert run(go()) == {"status": "ok", "text": "Drop the rule never use emoji?"}


def test_rule_tools_ask_every_time_and_are_offered_to_desk_sessions() -> None:
    from scout import mcp_server

    assert not shared_tools.BY_NAME["rules_list"].asks
    assert shared_tools.BY_NAME["rule_add"].asks and shared_tools.BY_NAME["rule_drop"].asks
    assert {"mcp__voice_app__rule_add", "mcp__voice_app__rule_drop"} <= set(brain.ALWAYS_ASK_TOOLS)
    offered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"rules_list", "rule_add", "rule_drop"} <= offered
    assert "rule_add" in {t.name for t in ps.PHONE_ACTIONS} and "rules_list" in {
        t.name for t in ps.PHONE_TOOLS
    }


def test_desk_sessions_are_briefed_with_the_rules() -> None:
    from scout import mcp_server

    assert "rules for how you behave" not in mcp_server.instructions()
    rules().add("Call me Mike")
    text = mcp_server.instructions()
    assert text.startswith(mcp_server.INSTRUCTIONS) and "- Call me Mike" in text


def test_the_phone_agent_gets_the_rules_as_a_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SWML_PROXY_URL_BASE", PUBLIC)
    path = tmp_path / "devices.json"
    device, secret = phone_devices.pair("Test phone", path)
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, path), FakeScout(), AUTH, "s" * 43)
    import hashlib
    import hmac
    import json

    def swml(cid: str) -> dict[str, Any]:
        raw = json.dumps(
            {"call": {"call_id": cid}, "call_id": cid, "vars": {"userVariables": uvars(device, secret)}}
        )
        sig = hmac.new(KEY.encode(), (PUBLIC + "/" + raw).encode(), hashlib.sha256).hexdigest()
        with TestClient(agent.get_app()) as c:
            r = c.post("/", content=raw, auth=AUTH,
                       headers={"Content-Type": "application/json", "X-SignalWire-Sha256-Signature": sig})  # fmt: skip
        assert r.status_code == 200, r.text
        return dict(r.json())

    titles = [s["title"] for s in _ai(swml("call-r1"))["prompt"]["pom"]]
    assert "Rules" not in titles  # none yet: nothing added to the call's prompt
    rules().add("Call me Mike")
    sections = {s["title"]: s["body"] for s in _ai(swml("call-r2"))["prompt"]["pom"]}
    assert "- Call me Mike" in sections["Rules"] and len(sections["Rules"]) < 300


def test_a_rule_said_to_the_phone_is_left_to_its_two_steps() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        return await r.a.ask(ps.AGENT, "from now on call me Mike")

    out = run(go())
    assert "rule_add" in out["text"] and "Call me Mike" in out["text"]
    assert rules().rules() == []  # nothing kept without the caller's yes


# --- the store: records and links ---------------------------------------------------------


def test_rules_are_records_and_a_told_rule_links_to_its_turn(tmp_path: Path) -> None:
    ix = index.default()
    assert isinstance(ix, index.Index) and ix.journal_root is not None
    ix.web_base = lambda: "https://scout.example"
    r, _ = rules().add("Call me Mike")
    ix.journal_add(time.time(), "room", "from now on call me Mike", "Okay, I'll keep that rule.", refs=[r.id])
    h = ix.get(r.id)
    assert h is not None and h.kind == "behavior" and h.title == "Call me Mike"
    rel = [(x["rel"], x["dir"]) for x in ix.links(r.id)]
    assert ("told_in", "out") in rel
    got = ix.resolve(r.id)
    assert (
        got is not None and got["page"].startswith("https://scout.example/r/behavior") and not got["private"]
    )
    assert got["url"].startswith("https://scout.example/r/journal")  # opens where it was told
    assert "behavior" in links.OWN_KINDS


def test_an_edit_by_hand_is_reindexed(tmp_path: Path) -> None:
    ix = index.default()
    assert isinstance(ix, index.Index)
    rules().add("Call me Mike")
    ix.sync_files()
    assert ix.count("behavior") == 1
    p = rules().path
    p.write_text(p.read_text() + "- Never use emoji\n")
    ix.sync_files()
    assert ix.count("behavior") == 2
    p.unlink()
    ix.sync_files()
    assert ix.count("behavior") == 0


def test_a_suggested_rule_links_to_the_turns_it_came_from() -> None:
    ix = index.default()
    assert isinstance(ix, index.Index)
    ts = time.time() - 60
    ix.journal_add(ts, "room", "no, read the ticket number digit by digit", "Ticket 6 1 7 2 3.")
    [turn] = [h.id for h in ix.recent(["turn"], 5)]
    r, _ = rules().add("Read ticket numbers digit by digit", origin="from corrections", turns=[turn])
    ix.sync_files()
    assert ("based_on", "out") in [(x["rel"], x["dir"]) for x in ix.links(r.id) if x["id"] == turn]


# --- the briefing's offers and the review --------------------------------------------------


def _pending(rule: str, about: str, turns: list[str]) -> None:
    c = corrections.Candidate(behavior.rule_id(rule), rule, about, turns, dt.date.today().isoformat())
    corrections.Candidates().add([c], dt.date.today())


def test_the_briefing_offers_a_suggested_rule_and_keeps_it_on_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    _pending(
        "Always read ticket numbers digit by digit", "how I read ticket numbers", ["journal:a", "journal:b"]
    )

    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._offer_rules({"mini": True, "client": None}))
        await asyncio.sleep(0.02)
        await hear(r, "yes", started=time.monotonic())
        await task
        return r.spk.said

    said = run(go())
    assert "Twice yesterday you corrected how I read ticket numbers." in said[0]
    [r] = rules().rules()
    assert r.origin == "from corrections" and r.turns == ("journal:a", "journal:b")
    assert corrections.Candidates().pending() == []


def test_a_declined_suggestion_is_not_offered_again() -> None:
    _pending("Always say the temperature first", "what I say first about the weather", ["journal:a"])

    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._offer_rules({"mini": True, "client": None}))
        await asyncio.sleep(0.02)
        await hear(r, "no", started=time.monotonic())
        await task
        await r.a._offer_rules({"mini": True, "client": None})  # nothing left to offer
        return r.spk.said

    said = run(go())
    assert sum("temperature" in s for s in said) == 1 and rules().rules() == []


def test_review_my_rules_merges_duplicates_only_on_yes() -> None:
    p = rules().path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "- Always say the weather in Fahrenheit · told · added 2026-01-01 · confirmed 2026-01-01\n"
        "- Say the weather in Fahrenheit always · told · added 2026-02-01 · confirmed 2026-02-01\n"
        "- Read ticket numbers digit by digit · from a fix · enforced: speech.speak_ids\n"
    )

    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("review my rules"))
        await asyncio.sleep(0.02)
        await hear(r, "yes", started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go())
    assert said[0].startswith("These two rules say the same thing")
    assert [r.text for r in rules().rules()] == [
        "Always say the weather in Fahrenheit",
        "Read ticket numbers digit by digit",
    ]
    assert "1 is kept by my code" in said[-1] and "merged 1" in said[-1].lower()
    assert not corrections.Candidates().review_due(dt.date.today())


def test_the_nightly_pass_runs_only_with_the_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_nightly(model: Any, day: dt.date, root: Path, barge_ins: Any = (), **kw: Any) -> list[Any]:
        seen.append((model, day, root))
        return []

    monkeypatch.setattr(corrections, "nightly", fake_nightly)

    async def go() -> None:
        r = make()
        await r.a._nightly_rules()  # no local model: nothing runs, nothing reaches Claude
        r.a.tier1 = object()  # type: ignore[assignment]  # a stand-in for the loaded model
        await r.a._nightly_rules()

    run(go())
    assert len(seen) == 1 and seen[0][1] == dt.date.today() - dt.timedelta(days=1)
    assert seen[0][2] == rules().path.parent


# --- scoutctl rules ---------------------------------------------------------------------


def test_scoutctl_rules_add_list_drop(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        behavior.main(["add", "Say phone numbers in groups", "--enforced", "contacts_mac.spoken_number"]) == 0
    )
    assert "from a fix" in capsys.readouterr().out
    assert behavior.main(["add", "Call me Mike"]) == 0
    assert "set by Mike" in capsys.readouterr().out
    assert behavior.main(["drop", "Mike"]) == 0 and len(rules().rules()) == 2  # shows only
    assert behavior.main(["drop", "Mike", "--yes"]) == 0 and len(rules().rules()) == 1
    assert behavior.main(["drop", "phone numbers", "--yes"]) == 1  # enforced: stays
    behavior.main(["list"])
    assert "enforced in code: contacts_mac.spoken_number" in capsys.readouterr().out
