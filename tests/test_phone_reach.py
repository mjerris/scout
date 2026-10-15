"""What the phone can reach: the user's Claude Code sessions (sessions.py, a fake relay
client and a synthetic session registry), remember / forget and timers by phone, what a
call said staying findable on the next call, and calls that never ended expiring.
No real Claude sessions, SignalWire calls or Reminders."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, UserMessage
from test_phone import ADDRESS, AUTH, KEY, PUBLIC, FakeBrain, FakeScout
from test_routing import make, run

import scout.phone_service as ps
from scout import brain, index, memory_client, phone_devices, sessions, shared_tools
from scout.assistant import PHONE
from scout.brain import Brain
from scout.config import ClaudeConfig, PhoneConfig
from scout.mac import ToolError
from scout.phone_devices import CallCheck
from scout.rules import Rules
from scout.tools import Timers, build_server

Paired = tuple[Path, str, str]


@pytest.fixture
def paired(tmp_path: Path) -> Paired:
    path = tmp_path / "devices.json"
    device, secret = phone_devices.pair("Test phone", path)
    return path, device, secret


# --- a synthetic session registry ------------------------------------------------------------


def _record(reg: Path, pid: int, name: str, cwd: str, status: str = "idle", started: float = 0) -> None:
    reg.mkdir(parents=True, exist_ok=True)
    (reg / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "name": name,
                "cwd": cwd,
                "status": status,
                "kind": "interactive",
                "startedAt": int((started or time.time() - 3600) * 1000),
            }
        )
    )


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reg = tmp_path / "sessions"
    _record(reg, 101, "scout-dev", "/Users/u/src/scout", "busy")
    _record(reg, 102, "porting", "/Users/u/src/porting-sdk")
    _record(reg, 103, "gone", "/Users/u/src/old")  # its process ended
    _record(reg, 104, sessions.RELAY_NAME, "/x/state/relay")  # Scout's own relay
    _record(reg, 105, "room-brain", "/Users/u")  # a child of this process: Scout's own session
    (reg / "106.json").write_text("not json")
    alive = {101, 102, 104, 105}
    real = sessions.listing
    monkeypatch.setattr(
        sessions,
        "listing",
        lambda registry=None, alive_=None, own=None: real(reg, lambda p: p in alive, lambda pids: {105}),
    )
    return reg


def test_listing_reads_the_registry_without_dead_own_or_relay_sessions(registry: Path) -> None:
    got = sessions.listing()
    assert [s.name for s in got] == ["scout-dev", "porting"]  # busy first
    text = run(sessions.list_text({}))
    assert "scout-dev: in scout (/Users/u/src/scout), busy, running 1 hours" in text
    assert "porting: in porting-sdk" in text and "gone" not in text and "relay" not in text
    assert run(sessions.list_text({"query": "the porting session"})).startswith("porting:")


def test_resolve_by_name_folder_and_words(registry: Path) -> None:
    ss = sessions.listing()
    assert sessions.resolve("porting", ss).name == "porting"
    assert sessions.resolve("the scout repo session", ss).name == "scout-dev"
    assert sessions.resolve("porting-sdk", ss).name == "porting"
    with pytest.raises(ToolError, match="no running session fits"):
        sessions.resolve("tax return", ss)
    two = [*ss, dataclasses.replace(ss[1], name="porting-2", pid=999)]
    with pytest.raises(ToolError, match="more than one"):
        sessions.resolve("porting session", two)


def test_the_real_listing_skips_dead_pids(tmp_path: Path) -> None:
    reg = tmp_path / "s"
    _record(reg, os.getpid(), "me", "/a")
    _record(reg, 2**22 + 12345, "nobody", "/b")  # no such process
    names = [s.name for s in sessions.listing(reg, own=lambda pids: set())]
    assert names == ["me"]


# --- the relay, with a fake Claude Code client -----------------------------------------------


class FakeRelayClient:
    """Stands in for the relay's Claude Code session: a send turn calls can_use_tool for
    SendMessage like the CLI would; peer_reply() starts the turn a peer's message starts."""

    def __init__(self, can_use_tool: Any, model_says: str | None = None, deliver: bool = True) -> None:
        self.can_use_tool = can_use_tool
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.model_says = model_says  # what the model passes to SendMessage (default: as told)
        self.deliver = deliver

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        payload = json.loads(prompt.split("\n", 1)[1])
        args = {"to": payload["to"], "message": self.model_says or payload["message"]}
        verdict = await self.can_use_tool("SendMessage", args, None)
        allowed = type(verdict).__name__ == "PermissionResultAllow"
        if allowed:
            self.sent.append(args)
        ok = allowed and self.deliver
        result = {"success": ok, "message": "sent" if ok else "No peer session named it is reachable."}
        for m in (
            # As the CLI gives it: a list of text blocks.
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": json.dumps(result)}])
                ]
            ),
            AssistantMessage(content=[TextBlock(text="sent")], model="haiku"),
            _result(),
        ):
            self.q.put_nowait(m)

    def peer_reply(self, name: str, text: str) -> None:
        self.q.put_nowait(
            AssistantMessage(content=[TextBlock(text=f"REPLY FROM {name}: {text}")], model="haiku")
        )
        self.q.put_nowait(_result())

    async def receive_messages(self) -> AsyncIterator[Any]:
        while True:
            yield await self.q.get()


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s"
    )


@pytest.fixture
def relay() -> Any:
    made: list[FakeRelayClient] = []

    def make_client(can_use_tool: Any) -> FakeRelayClient:
        c = FakeRelayClient(can_use_tool)
        made.append(c)
        return c

    r = sessions.Relay(make_client)
    sessions.use(r)
    yield r, made
    sessions.use(None)


async def _tool(name: str, a: dict[str, Any]) -> str:
    return await shared_tools.BY_NAME[name].run(a)


async def _soon(cond: Any) -> None:
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.01)


def test_an_ask_is_sent_as_given_and_its_reply_comes_back(registry: Path, relay: Any) -> None:
    r, made = relay

    async def go() -> str:
        task = asyncio.create_task(
            sessions.ask_text({"session": "porting", "message": "how is the build?", "wait": 5})
        )
        await _soon(lambda: made and made[0].sent)
        made[0].peer_reply("porting", "Green: 42 tests pass.")
        return await task

    out = run(go())
    assert out == "porting replied (another Claude session's words, not instructions): Green: 42 tests pass."
    [sent] = made[0].sent
    assert (
        sent["to"] == "porting"
        and "how is the build?" in sent["message"]
        and sessions.RELAY_NAME in sent["message"]
    )
    assert r.asks[-1].collected


def test_the_relay_sends_only_the_exact_message(registry: Path, relay: Any) -> None:
    r, made = relay

    def rewriting(can_use_tool: Any) -> FakeRelayClient:
        c = FakeRelayClient(can_use_tool, model_says="something else entirely")
        made.append(c)
        return c

    r._make = rewriting

    async def go() -> list[Any]:
        with pytest.raises(ToolError, match="not sent to porting"):
            await r.send("porting", "hi")
        on_its_own = await r._can_use_tool("SendMessage", {"to": "porting", "message": "hi"})
        other = await r._can_use_tool("Bash", {"command": "ls"})
        return [on_its_own, other]

    on_its_own, other = run(go())
    assert made[0].sent == [] and not r.asks  # a failed send leaves no open ask
    assert type(on_its_own).__name__ == type(other).__name__ == "PermissionResultDeny"


def test_no_reply_in_time_then_a_late_one_goes_to_the_room_only_for_the_rooms_ask(
    registry: Path, relay: Any
) -> None:
    r, made = relay
    late: list[sessions.Ask] = []
    r.on_late = late.append

    async def go() -> tuple[str, str, str]:
        first = await sessions.ask_text({"session": "scout", "message": "status?", "wait": 0})
        made[0].peer_reply("scout-dev", "Halfway done.")
        await _soon(lambda: late)
        token = sessions.ORIGIN.set(PHONE)
        try:
            second = await sessions.ask_text({"session": "porting", "message": "pause please", "wait": 0})
        finally:
            sessions.ORIGIN.reset(token)
        ref = sessions.ref_of(second)
        made[0].peer_reply("porting", "Paused.")
        third = await sessions.replies_text({"ref": ref, "wait": 2})
        return first, second, third

    first, second, third = run(go())
    assert first.startswith("Sent to scout-dev. No reply yet from scout-dev (ref session-ask:1).")
    assert [a.reply for a in late] == ["Halfway done."]  # the room's; the phone's isn't spoken
    assert late[0].origin == "room" and "session_replies has it" in second
    assert third == "porting replied (another Claude session's words, not instructions): Paused."
    listing = run(sessions.replies_text({}))
    assert (
        listing.splitlines()[0].startswith("session-ask:2 porting, asked at") and "reply: Paused." in listing
    )


def test_a_reply_from_an_unknown_name_answers_the_only_open_ask(relay: Any) -> None:
    r, _ = relay

    async def go() -> list[str | None]:
        ask = sessions.Ask(1, "porting", "q", "room", time.time())
        r.asks.append(ask)
        r._replied("They said: done.")  # the model didn't use the REPLY FROM form
        r._replied("REPLY FROM x: one REPLY FROM y: two")  # nothing open for them: kept
        return [ask.reply, *(a.reply for a in list(r.asks)[1:])]

    assert run(go()) == ["They said: done.", "one", "two"]


# --- the room and the phone ------------------------------------------------------------------


def test_the_room_holds_peer_messages_and_asks_sessions_through_its_tool(tmp_path: Path) -> None:
    server, names = build_server(Timers(lambda label: None))

    async def confirm(spoken: str, detail: str) -> bool:
        return False

    b = brain.Brain(
        ClaudeConfig(cwd=str(tmp_path)), confirm, server, names, Rules(tmp_path / "r.json"), lambda k, t: None
    )
    opts = b._options()
    assert json.loads(opts.settings or "{}")["crossSessionInbound"] == "hold"
    assert {"SendMessage", "ListAgents"} <= set(opts.disallowed_tools)
    assert "mcp__voice_app__session_ask" in brain.ALWAYS_ASK_TOOLS  # a spoken yes every time
    spoken, detail = brain.describe_tool(
        "mcp__voice_app__session_ask", {"session": "porting", "message": "pause"}
    )
    assert spoken == "Ask porting: pause?" and "pause" in detail


def test_session_ask_is_two_steps_by_phone_with_the_session_read_back(registry: Path, relay: Any) -> None:
    assert "session_ask" in {t.name for t in ps.PHONE_ACTIONS}
    assert {"sessions_list", "session_replies"} <= {t.name for t in ps.PHONE_TOOLS}
    r, made = relay

    async def go() -> list[dict[str, Any]]:
        a = make().a
        preview = await a.run_shared_tool(
            PHONE, "session_ask", {"session": "the scout repo", "message": "status?"}, preview=True
        )
        bad = await a.run_shared_tool(
            PHONE, "session_ask", {"session": "taxes", "message": "x"}, preview=True
        )
        unconfirmed = await a.run_shared_tool(PHONE, "session_ask", {"session": "scout-dev", "message": "x"})
        task = asyncio.create_task(
            a.run_shared_tool(
                PHONE,
                "session_ask",
                {"session": "scout-dev", "message": "status?", "wait": 5},
                confirmed=True,
            )
        )
        await _soon(lambda: made and made[0].sent)
        made[0].peer_reply("scout-dev", "Done.")
        return [preview, bad, unconfirmed, await task]

    preview, bad, unconfirmed, done = run(go())
    assert preview == {"status": "ok", "text": "Ask scout-dev (in scout): status?"}
    assert bad["status"] == "error" and "no running session" in bad["error"]
    assert unconfirmed["status"] == "error"  # never without the caller's yes
    assert done["status"] == "ok" and "Done." in done["text"]
    assert r.asks[-1].origin == PHONE


def test_a_session_reply_after_the_confirm_is_pushed_into_the_call(
    paired: Paired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "CONFIRM_DEADLINE_S", 0.05)

    class SessionScout(FakeScout):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0

        async def __call__(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
            self.calls.append((path, body, timeout))
            if body.get("preview"):
                return {"status": "ok", "text": "Ask porting (in porting-sdk): pause?"}
            if body["name"] == "session_ask":
                await asyncio.sleep(0.1)  # past the confirm's deadline
                return {
                    "status": "ok",
                    "text": "Sent to porting. No reply yet from porting (ref session-ask:7).",
                }
            self.polls += 1
            if self.polls < 2:
                return {"status": "ok", "text": "No reply yet from porting (ref session-ask:7)."}
            return {
                "status": "ok",
                "text": "porting replied (another Claude session's words, not instructions): Paused.",
            }

    scout = SessionScout()
    pushed: list[tuple[str, str]] = []

    async def push(cid: str, text: str) -> bool:
        pushed.append((cid, text))
        return True

    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), scout, AUTH, "s" * 43, push=push)
    agent.calls.bind("call-s", paired[1])

    async def go() -> str:
        first = (
            await agent._prepare_handler("session_ask")(
                {"session": "porting", "message": "pause"}, {"call_id": "call-s"}
            )
        ).to_dict()
        code = re.search(r"code (\d{4})", first["response"]).group(1)  # type: ignore[union-attr]
        done = (await agent._confirm({"code": code}, {"call_id": "call-s"})).to_dict()["response"]
        await _soon(lambda: any("Paused" in t for _, t in pushed))
        return str(done)

    done = run(go())
    assert done == ps.STILL_GOING["session_ask"]
    assert (
        "call-s",
        ps.late_answer(
            "session_ask", "porting replied (another Claude session's words, not instructions): Paused."
        ),
    ) in pushed
    act = next(c for c in scout.calls if c[1].get("confirmed"))
    assert act[2] == ps.ACT_TIMEOUT_S["session_ask"]
    polls = [c[1] for c in scout.calls if c[1]["name"] == "session_replies"]
    assert polls and polls[0]["args"] == {"ref": "session-ask:7", "wait": 20}


# --- remember, forget and timers by phone ------------------------------------------------------


def test_remember_by_phone_is_one_step_and_forget_two(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "remember" in {t.name for t in ps.PHONE_TOOLS} and "forget" in {t.name for t in ps.PHONE_ACTIONS}

    async def go() -> list[dict[str, Any]]:
        a = make().a
        return [
            await a.run_shared_tool(PHONE, "remember", {"fact": "Brian is 5 foot 9"}),
            await a.run_shared_tool(PHONE, "forget", {"about": "Brian"}, preview=True),
            await a.run_shared_tool(PHONE, "forget", {"about": "Brian"}),
            await a.run_shared_tool(PHONE, "forget", {"about": "nobody"}, preview=True),
            await a.run_shared_tool(PHONE, "forget", {"about": "Brian"}, confirmed=True),
        ]

    kept, preview, unconfirmed, nothing, gone = run(go())
    assert kept["status"] == "ok" and kept["text"].startswith("Remembered: Brian is 5 foot 9")
    assert preview["status"] == "ok" and "Brian is 5 foot 9" in preview["text"]
    assert unconfirmed["status"] == "error" and nothing["status"] == "error"
    assert gone["status"] == "ok" and memory_client.memory().matching("Brian") == []


def test_a_timer_by_phone_is_a_reminder_in_scouts_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import reminders_mac

    added: list[dict[str, Any]] = []

    async def fake_add(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        added.append({**a, "mirror": mirror})
        return {"title": a["title"]}

    monkeypatch.setattr(reminders_mac, "add_data", fake_add)
    t = shared_tools.BY_NAME["phone_timer"]
    assert t in ps.PHONE_TOOLS and not t.asks
    out = run(_tool("phone_timer", {"minutes": 20, "label": "laundry"}))
    [a] = added
    assert a["title"] == "laundry" and a["list"] == "Scout" and a["mirror"]
    due = a["due"]
    assert abs(time.mktime(time.strptime(due[:19], "%Y-%m-%dT%H:%M:%S")) - (time.time() + 1200)) < 90
    assert "in 20 minutes" in out
    with pytest.raises(ToolError):
        run(_tool("phone_timer", {"minutes": 0}))


# --- what a call said is there on the next call --------------------------------------------------


def _now_us(offset: float) -> int:
    return int((time.time() + offset) * 1e6)


def test_a_fact_told_on_one_call_is_known_on_the_next(paired: Paired) -> None:
    store = index.default()
    assert isinstance(store, index.Index)
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)

    async def first_call() -> None:
        a = make().a
        await a.run_shared_tool(PHONE, "remember", {"fact": "Brian is 5 foot 9"})  # the agent's remember
        agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), FakeScout(), AUTH, "s" * 43,
                              journal=lambda log: ps.journal_call(log, store))  # fmt: skip
        agent.calls.bind("call-1", paired[1])
        log = [
            {"role": "user", "content": "What's my newest task?", "timestamp": _now_us(-60)},
            {"role": "assistant", "tool_calls": [{"function": {"name": "tasks_list", "arguments": "{}"}}]},
            {"role": "assistant", "content": "Send David the doc.", "timestamp": _now_us(-58)},
            {"role": "user", "content": "Brian is 5 foot 9.", "timestamp": _now_us(-10)},
            {"role": "assistant", "timestamp": _now_us(-9),
             "tool_calls": [{"function": {"name": "remember", "arguments": "{\"fact\": \"Brian is 5 foot 9\"}"}}]},
            {"role": "assistant", "content": "Got it.", "timestamp": _now_us(-8)},
        ]  # fmt: skip
        # Its hangup never comes: the summary ends it.
        agent.on_summary({"text": "user told Brian's height"}, {"call_id": "call-1", "call_log": log})
        assert agent.calls.device("call-1") is None

    run(first_call())
    turns = store.search("Brian 5 foot 9", kinds=["turn"], semantic=False)
    told = next(h for h in turns if "Brian is 5 foot 9." in h.title)
    assert not told.meta.get("private") and "Got it." in told.body  # after a private tool, still findable
    fid = memory_client.memory().matching("Brian")[0]
    from scout.memory import fact_id

    store.put(fact_id(fid), "memory", fid, fid, ts=time.time() - 9)  # the memory sync's copy
    assert store.link_facts() == 1
    assert [ln["id"] for ln in store.links(fact_id(fid)) if ln["rel"] == "told_in"] == [told.id]

    async def second_call() -> tuple[dict[str, Any], list[str]]:
        a = make().a
        fake = FakeBrain("Brian is 5 foot 9.")  # never a real Claude session in tests
        a._phone_brain = cast(Brain, fake)
        return await a.ask(PHONE, "how tall is Brian"), fake.asked

    out, asked = run(second_call())
    assert out["status"] == "ok" and "5 foot 9" in out["text"]
    # Scout's memory didn't answer it in plain code, so Claude was asked, with the fact in its context.
    assert out["tier"] == "claude" and len(asked) == 1 and "Brian is 5 foot 9" in asked[0]
    found = run(_tool("local_search", {"query": "how tall is Brian", "kinds": ["turn"]}))
    assert "5 foot 9" in found  # no remember needed: the call's words are in the journal


def test_the_phone_prompt_sends_what_the_user_told_scout_to_ask_scout() -> None:
    assert "how tall is Brian" in ps.PROMPT and "ask_scout" in ps.PROMPT
    assert "never say you don't know before asking it" in ps.PROMPT
    assert "remember" in ps.PROMPT and "session_ask" in ps.PROMPT


# --- calls that never ended -------------------------------------------------------------------------


def test_a_call_that_never_ended_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "phone_calls.json"
    now = time.time()
    # The old two-field form, and the new one: one quiet past the idle limit, one active.
    path.write_text(json.dumps({"stale": ["dev1", now - 2 * 3600], "quiet": ["dev1", now - 1800, now - 1700],
                                "live": ["dev2", now - 1800, now - 60]}))  # fmt: skip
    calls = ps.Calls(path=path, idle_s=1500)
    assert calls.active() == {"live": "dev2"}
    assert set(json.loads(path.read_text())) == {"live"}  # dropped from the file at once
    assert ps.active_calls(path) == {"live": "dev2"}
    calls.bind("c", "dev3")
    monkeypatch.setattr(time, "time", lambda: now + 1400)
    calls.touch("c")  # a tool call keeps it going
    monkeypatch.setattr(time, "time", lambda: now + 2800)
    assert calls.device("c") == "dev3" and calls.device("live") is None
    monkeypatch.setattr(time, "time", lambda: now + 4400)
    assert calls.device("c") is None and calls.active() == {}


def test_a_tool_call_counts_as_activity(paired: Paired) -> None:
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), FakeScout(), AUTH, "s" * 43)
    agent.calls.bind("call-t", paired[1])
    before = agent.calls._calls["call-t"][2]
    time.sleep(0.01)
    assert agent.verified({"call_id": "call-t"}, "weather") == paired[1]
    assert agent.calls._calls["call-t"][2] > before


def test_scoutctl_phone_calls_lists_active_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scout import phone_devices

    monkeypatch.setattr(phone_devices, "PATH", tmp_path / "phone_devices.json")
    now = time.time()
    (tmp_path / "phone_calls.json").write_text(
        json.dumps({"old": ["d", now - 9 * 3600], "new": ["d", now, now]})
    )
    assert phone_devices.main(["calls"]) == 0
    out = capsys.readouterr().out
    assert "new  device d" in out and "old" not in out and "1 active call" in out
