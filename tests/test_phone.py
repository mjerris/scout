"""scout-phone: pairing, the per-call check, the agent's webhooks, and Scout's ask path.

The agent is the real SignalWire AgentBase app, driven through FastAPI's TestClient
with SignalWire-style signatures computed here; Scout's local API is a fake."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import logging
import secrets
import stat
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from aiohttp import web as aioweb
from fastapi.testclient import TestClient
from test_routing import make, run

import scout.phone_service as ps
import scout.web as web_mod
from scout import ledger, phone_devices
from scout.assistant import ASK_STILL_WORKING, PHONE, PHONE_TOOLS
from scout.brain import PHONE_REFUSED, Brain, PhoneBrain
from scout.config import ClaudeConfig, Config, PhoneConfig, load
from scout.memory import Memory
from scout.phone_devices import CallCheck
from scout.rules import Rules
from scout.tools import Timers, build_server

PUBLIC = "https://mac.example.ts.net:8443"
KEY = "PSK_test_signing_key"
ADDRESS = "/public/scout"
AUTH = ("scout-test", "test-password")


# --- config and state files ---------------------------------------------------------------


def test_phone_config_defaults_and_new_keys(tmp_path: Path) -> None:
    d = PhoneConfig()
    assert (d.enabled, d.host, d.port, d.public_url, d.address, d.call_skew_s) == (
        False, "127.0.0.1", 8768, "", "", 60,
    )  # fmt: skip
    p = tmp_path / "config.toml"
    p.write_text(f'[phone]\nenabled = true\npublic_url = "{PUBLIC}"\nport = 9000\ncall_skew_s = 30\n')
    cfg = load(p).phone
    assert (cfg.enabled, cfg.public_url, cfg.port, cfg.call_skew_s) == (True, PUBLIC, 9000, 30)


def test_config_problems_refuse_to_start() -> None:
    ok = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC)
    assert ps.config_problem(ok) is None
    assert "enabled" in (ps.config_problem(PhoneConfig()) or "")
    assert "signing_key" in (ps.config_problem(PhoneConfig(enabled=True, public_url=PUBLIC)) or "")
    assert "public_url" in (ps.config_problem(PhoneConfig(enabled=True, signing_key=KEY)) or "")
    plain = PhoneConfig(enabled=True, signing_key=KEY, public_url="http://mac:8768")
    assert "https" in (ps.config_problem(plain) or "")


def test_main_refuses_without_signing_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = Config()
    cfg.phone.enabled = True
    cfg.phone.public_url = PUBLIC
    monkeypatch.setattr(ps, "load", lambda: cfg)
    monkeypatch.setattr(ps, "build", lambda *a, **k: pytest.fail("must not start"))
    with caplog.at_level(logging.ERROR, logger="scout.phone"):
        ps.main()
    assert "scout-phone not started" in caplog.text and "signing_key" in caplog.text


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_state_files_are_private_and_stable(tmp_path: Path) -> None:
    auth, secret, devices = tmp_path / "phone.auth", tmp_path / "phone.secret", tmp_path / "devices.json"
    user, password = ps.credentials(auth)
    assert user and len(password) >= 24 and ps.credentials(auth) == (user, password)
    s = ps.swaig_secret(secret)
    assert len(s) >= 32 and ps.swaig_secret(secret) == s
    phone_devices.pair("Test phone", devices)
    assert _mode(auth) == _mode(secret) == _mode(devices) == 0o600


def test_pair_list_forget(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    device, secret = phone_devices.pair("Mike's phone", path)
    assert len(phone_devices._key(secret)) == 32
    [row] = phone_devices.listing(path)
    assert row == {"device": device, "name": "Mike's phone", "paired_at": row["paired_at"], "revoked": False}
    assert "secret" not in row
    assert phone_devices.forget(device, path) and not phone_devices.forget("dnope", path)
    stored = phone_devices.load_devices(path)[device]
    assert stored["revoked"] is True and stored["secret"] == ""
    p = phone_devices.payload(device, secret, "")
    assert (
        p["device"] == device and p["secret"] == secret and p["address"] == phone_devices.ADDRESS_PLACEHOLDER
    )


def test_scoutctl_pair_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(phone_devices, "PATH", tmp_path / "devices.json")
    assert phone_devices.main(["pair", "Test", "phone"]) == 0
    out = capsys.readouterr().out
    data = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    assert set(data) >= {"device", "secret", "address"}
    assert phone_devices.main(["list"]) == 0 and "Test phone" in capsys.readouterr().out
    assert phone_devices.main(["forget", data["device"]]) == 0
    assert phone_devices.main(["bogus"]) == 2


# --- the per-call check -------------------------------------------------------------------


@pytest.fixture
def paired(tmp_path: Path) -> tuple[Path, str, str]:
    path = tmp_path / "devices.json"
    device, secret = phone_devices.pair("Test phone", path)
    return path, device, secret


def uvars(
    device: str, secret: str, ts: int | None = None, nonce: str | None = None, address: str = ADDRESS
) -> dict[str, Any]:
    ts = int(time.time()) if ts is None else ts
    nonce = nonce or secrets.token_urlsafe(12)
    return {
        "device": device,
        "ts": ts,
        "nonce": nonce,
        "sig": phone_devices.sign(secret, device, ts, nonce, address),
    }


def test_call_check(paired: tuple[Path, str, str]) -> None:
    path, device, secret = paired
    check = CallCheck(ADDRESS, 60, path)
    good = uvars(device, secret)
    assert check.check(good).ok and check.check(good).reason == "nonce replayed"
    assert check.check({**uvars(device, secret), "sig": "0" * 64}).reason == "bad signature"
    assert check.check(uvars(device, secret, address="/public/other")).reason == "bad signature"
    assert "off" in check.check(uvars(device, secret, ts=int(time.time()) - 120)).reason
    assert "off" in check.check(uvars(device, secret, ts=int(time.time()) + 120)).reason
    assert check.check(uvars("dunknown123", secret)).reason == "unknown device"
    assert check.check({}).reason == "no device id"
    assert check.check({**uvars(device, secret), "ts": "soon"}).reason == "no timestamp"
    assert check.check({**uvars(device, secret), "nonce": "x"}).reason == "no nonce"
    as_text = uvars(device, secret)  # ts sent as a string is fine
    assert check.check({**as_text, "ts": str(as_text["ts"])}).ok
    phone_devices.forget(device, path)
    assert check.check(uvars(device, secret)).reason == "device revoked"
    assert not check.device_ok(device)


# --- the agent over HTTP --------------------------------------------------------------------


class FakeScout:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.reply: dict[str, Any] = {"status": "ok", "text": "Sunny, 70 degrees."}

    async def __call__(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append((path, body, timeout))
        return self.reply


class Rig:
    def __init__(self, paired: tuple[Path, str, str]) -> None:
        self.path, self.device, self.secret = paired
        self.scout = FakeScout()
        cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
        self.agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, self.path), self.scout, AUTH, "s" * 43)
        self.client = TestClient(self.agent.get_app())

    def post(self, target: str, body: dict[str, Any], signed: bool = True) -> Any:
        raw = json.dumps(body)
        sig = hmac.new(KEY.encode(), (PUBLIC + target + raw).encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json"}
        if signed:
            headers["X-SignalWire-Sha256-Signature"] = sig
        return self.client.post(target, content=raw, headers=headers, auth=AUTH)

    def swml(self, call_id: str, uv: dict[str, Any]) -> dict[str, Any]:
        r = self.post("/", {"call": {"call_id": call_id}, "call_id": call_id, "vars": {"userVariables": uv}})
        assert r.status_code == 200, r.text
        return cast(dict[str, Any], r.json())

    def swaig(self, swml: dict[str, Any], call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = next(f for f in _ai(swml)["SWAIG"]["functions"] if f["function"] == name)
        u = urlsplit(fn["web_hook_url"])
        assert u.username == AUTH[0] and f"{u.scheme}://{u.hostname}:{u.port}" == PUBLIC
        target = f"{u.path}?{u.query}"
        r = self.post(target, {"function": name, "call_id": call_id, "argument": {"parsed": [args]}})
        assert r.status_code == 200, r.text
        return cast(dict[str, Any], r.json())


def _ai(swml: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], next(v["ai"] for v in swml["sections"]["main"] if "ai" in v))


def _functions(swml: dict[str, Any]) -> set[str]:
    return {f["function"] for f in _ai(swml).get("SWAIG", {}).get("functions", [])}


@pytest.fixture
def rig(paired: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    monkeypatch.setenv("SWML_PROXY_URL_BASE", PUBLIC)
    r = Rig(paired)
    yield r
    r.client.close()


def test_verified_call_gets_scout_tools(rig: Rig) -> None:
    swml = rig.swml("call-1", uvars(rig.device, rig.secret))
    names = _functions(swml)
    want = {t.name for t in ps.PHONE_TOOLS} | {"ask_scout"}
    assert want <= names
    assert not names & {
        "mail_send",
        "calendar_create_event",
        "reminder_add",
        "reminder_complete",
        "note_delete",
    }
    assert _ai(swml)["global_data"] == {"device": rig.device, "verified": True}
    verbs = [next(iter(v)) for v in swml["sections"]["main"]]
    assert verbs == ["answer", "ai"]  # no goodbye
    assert rig.agent.calls.device("call-1") == rig.device


def test_unverified_call_gets_no_tools_and_a_goodbye(rig: Rig) -> None:
    bad = {**uvars(rig.device, rig.secret), "sig": "0" * 64}
    swml = rig.swml("call-2", bad)
    assert not _functions(swml) & ({t.name for t in ps.PHONE_TOOLS} | {"ask_scout"})
    main = swml["sections"]["main"]
    verbs = [next(iter(v)) for v in main]
    assert verbs[:3] == ["answer", "play", "hangup"]
    assert main[1]["play"]["url"] == f"say:{ps.GOODBYE}"
    assert "global_data" not in _ai(swml)
    assert rig.agent.calls.device("call-2") is None


def test_replayed_setup_is_refused(rig: Rig) -> None:
    uv = uvars(rig.device, rig.secret)
    assert "ask_scout" in _functions(rig.swml("call-3", uv))
    assert "ask_scout" not in _functions(rig.swml("call-4", uv))  # same nonce, new call


def test_unsigned_webhooks_are_rejected(rig: Rig) -> None:
    body = {"call": {"call_id": "c"}, "vars": {"userVariables": uvars(rig.device, rig.secret)}}
    assert rig.post("/", body, signed=False).status_code == 403
    assert rig.client.post(
        "/", json=body, headers={"X-SignalWire-Sha256-Signature": "0" * 64}
    ).status_code in (401, 403)
    assert rig.agent.calls.device("c") is None


def test_tool_call_forwards_to_scout(rig: Rig) -> None:
    swml = rig.swml("call-5", uvars(rig.device, rig.secret))
    out = rig.swaig(swml, "call-5", "weather", {"place": "Luther, Michigan"})
    assert out["response"] == "Sunny, 70 degrees."
    assert rig.scout.calls == [
        (
            "/api/tool",
            {"agent": "phone", "name": "weather", "args": {"place": "Luther, Michigan"}},
            ps.TOOL_TIMEOUT_S,
        )
    ]
    rig.scout.reply = {"status": "error", "error": "calendar access not allowed"}
    out = rig.swaig(swml, "call-5", "calendar_events", {"start": "2026-10-12"})
    assert out["response"] == "Not done: calendar access not allowed"


def test_ask_scout_forwards_to_scout(rig: Rig) -> None:
    swml = rig.swml("call-6", uvars(rig.device, rig.secret))
    rig.scout.reply = {"status": "pending", "text": ASK_STILL_WORKING}
    out = rig.swaig(swml, "call-6", "ask_scout", {"request": "what did I say about the tires?"})
    assert out["response"] == ps.STILL_LOOKING  # the answer is pushed when Scout has it
    assert rig.scout.calls[-1] == (
        "/api/ask",
        {"agent": "phone", "text": "what did I say about the tires?"},
        ps.ASK_TIMEOUT_S,
    )


def test_tool_handlers_refuse_unbound_calls(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    async def go() -> list[str]:
        handle = rig.agent._tool_handler("weather")
        forged = {"call_id": "never-verified", "global_data": {"device": rig.device, "verified": True}}
        a = await handle({"place": "x"}, forged)
        b = await rig.agent._ask({"request": "hi"}, forged)
        return [a.to_dict()["response"], b.to_dict()["response"]]

    with caplog.at_level(logging.WARNING, logger="scout.phone"):
        assert run(go()) == [ps.REFUSED, ps.REFUSED]
    assert rig.scout.calls == [] and "unverified call never-verified" in caplog.text


def test_forgetting_a_phone_ends_its_tools_mid_call(rig: Rig) -> None:
    swml = rig.swml("call-7", uvars(rig.device, rig.secret))
    phone_devices.forget(rig.device, rig.path)
    fn = next(f for f in _ai(swml)["SWAIG"]["functions"] if f["function"] == "weather")
    u = urlsplit(fn["web_hook_url"])
    r = rig.post(
        f"{u.path}?{u.query}", {"function": "weather", "call_id": "call-7", "argument": {"parsed": [{}]}}
    )
    assert r.status_code == 200 and "Sunny" not in r.text
    assert rig.scout.calls == []


def test_summary_and_call_end_write_ledger_rows(rig: Rig) -> None:
    rig.swml("call-8", uvars(rig.device, rig.secret))
    rig.agent.on_summary({"text": "asked about the weather"}, {"call_id": "call-8"})
    rig.agent._call_ended([{"role": "user", "content": "hi"}], {"call_id": "call-8"})
    rows = ledger.rows(0)
    assert [r["event"] for r in rows] == ["summary", "call_end"]
    assert all(r["tier"] == "phone" and r["client"] == "phone" and r["verified"] for r in rows)
    assert rows[1]["entries"] == 1 and rows[1]["latency_s"] >= 0
    assert "weather" not in json.dumps(rows)  # no call content in the ledger
    assert rig.agent.calls.device("call-8") is None  # unbound at the end
    assert ledger.summary(0)["requests"] == 0  # call events are not requests


# --- the local API client ------------------------------------------------------------------


def test_call_scout_posts_with_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "web_token"
    token.write_text("t" * 32 + "\n")
    monkeypatch.setattr(web_mod, "TOKEN_PATH", token)
    seen: list[tuple[str, str, Any]] = []

    async def handler(req: aioweb.Request) -> aioweb.Response:
        seen.append((req.path, req.headers.get("Authorization", ""), await req.json()))
        return aioweb.json_response({"status": "ok", "text": "done"})

    async def go() -> dict[str, Any]:
        app = aioweb.Application()
        app.router.add_post("/api/tool", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        cfg = Config()
        cfg.web.port = port
        monkeypatch.setattr(ps, "load", lambda: cfg)
        try:
            return await ps.call_scout("/api/tool", {"agent": "phone", "name": "weather", "args": {}}, 5)
        finally:
            await runner.cleanup()

    assert run(go()) == {"status": "ok", "text": "done"}
    assert seen == [("/api/tool", "Bearer " + "t" * 32, {"agent": "phone", "name": "weather", "args": {}})]


# --- Scout's side: /api/ask through the assistant ------------------------------------------


class FakeBrain:
    def __init__(self, reply: str = "The tires were rotated in May.", delay: float = 0.0) -> None:
        self.reply, self.delay = reply, delay
        self.asked: list[str] = []

    async def ask(self, text: str) -> AsyncIterator[tuple[str, Any]]:
        self.asked.append(text)
        await asyncio.sleep(self.delay)
        yield "speak", self.reply
        yield "spoken", self.reply
        yield "result", None

    async def reset(self) -> None:
        pass


def test_ask_answers_locally_without_speaking_or_taking_the_floor() -> None:
    async def go() -> tuple[dict[str, Any], list[str], str | None, str]:
        r = make()
        out = await r.a.ask(PHONE, "what time is it")
        return out, r.spk.said, r.a.floor.owner(), r.a.state

    out, said, owner, state = run(go())
    assert out["status"] == "ok" and out["text"].startswith("It's ") and out["tier"] == "0"
    assert said == [] and owner is None and state == "idle"
    [row] = ledger.rows(0)
    assert row["tier"] == "0" and row["client"] == "phone"


def test_ask_uses_claude_on_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> tuple[dict[str, Any], list[str], list[str]]:
        r = make()
        room_asked: list[str] = []

        async def room_ask(text: str) -> AsyncIterator[tuple[str, Any]]:
            room_asked.append(text)
            yield "result", None

        monkeypatch.setattr(r.a.brain, "ask", room_ask)
        fake = FakeBrain()
        r.a._phone_brain = cast(Brain, fake)
        out = await r.a.ask(PHONE, "when did I rotate the tires")
        return out, fake.asked, room_asked + r.spk.said

    out, asked, room = run(go())
    assert out == {"status": "ok", "text": "The tires were rotated in May.", "tier": "claude"}
    assert len(asked) == 1 and asked[0].endswith("when did I rotate the tires")
    assert room == []  # the room's Claude session and speaker were never used


def test_ask_does_not_set_timers_from_the_phone() -> None:
    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        fake = FakeBrain("I can't set timers from the phone.")
        r.a._phone_brain = cast(Brain, fake)
        out = await r.a.ask(PHONE, "set a timer for 5 minutes")
        return out, r.a.timers.listing()

    out, timers = run(go())
    assert out["tier"] == "claude" and timers == []


def test_ask_past_the_deadline_keeps_the_answer() -> None:
    async def go() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
        r = make()
        fake = FakeBrain(delay=0.3)
        r.a._phone_brain = cast(Brain, fake)
        first = await r.a.ask(PHONE, "what's the plan for Friday", deadline=0.05)
        again = await r.a.ask(
            PHONE, "What's the plan for  Friday", deadline=0.05
        )  # same request, still running
        await asyncio.sleep(0.4)
        done = await r.a.ask(PHONE, "what's the plan for Friday", deadline=0.05)
        return first, again, done, len(fake.asked)

    first, again, done, asked = run(go())
    assert first == again == {"status": "pending", "text": ASK_STILL_WORKING}
    assert done["status"] == "ok" and done["text"] == "The tires were rotated in May."
    assert asked == 1  # asking again joined the running request


def test_uncollected_answer_comes_with_the_next_reply() -> None:
    async def go() -> dict[str, Any]:
        r = make()
        r.a._phone_brain = cast(Brain, FakeBrain(delay=0.2))
        await r.a.ask(PHONE, "first question", deadline=0.01)
        await asyncio.sleep(0.3)
        r.a._phone_brain = cast(Brain, FakeBrain("Second answer."))
        return await r.a.ask(PHONE, "second question")

    out = run(go())
    assert out["text"].startswith("Second answer.") and "'first question'" in out["text"]


def test_phone_cannot_run_asking_tools_through_the_local_api() -> None:
    async def go() -> list[dict[str, Any]]:
        r = make()
        return [
            await r.a.run_shared_tool(PHONE, "reminder_add", {"title": "x"}),
            await r.a.run_shared_tool(PHONE, "mail_send", {"to": ["a@b.c"]}),
        ]

    for out in run(go()):
        assert out["status"] == "error" and "phone" in out["error"]


def test_phone_brain_refuses_what_would_ask(tmp_path: Path) -> None:
    server, names = build_server(Timers(lambda label: None), memory=Memory(tmp_path / "memory.json"))
    b = PhoneBrain(ClaudeConfig(), server, names, PHONE_TOOLS, Rules(tmp_path / "rules.json"))
    assert b._forbidden("mcp__voice_app__weather", {}) is None
    assert b._forbidden("mcp__voice_app__recall", {}) is None
    assert b._forbidden("Read", {"file_path": "/etc/hosts"}) is None
    assert b._forbidden("Bash", {"command": "date"}) is None  # a harmless single command
    refused: list[tuple[str, dict[str, Any]]] = [
        ("mcp__voice_app__open_url", {"url": "https://example.com"}),
        ("mcp__voice_app__set_timer", {"seconds": 60}),
        ("mcp__voice_app__mail_send", {"to": ["a@b.c"]}),
        ("mcp__voice_app__reminder_add", {"title": "x"}),
        ("Bash", {"command": "rm -rf build"}),
        ("WebFetch", {"url": "https://example.com"}),
    ]
    for name, args in refused:
        assert b._forbidden(name, args) == PHONE_REFUSED, name
    opts = b._options()
    assert "mcp__voice_app__open_url" in opts.disallowed_tools
    assert "mcp__voice_app__weather" not in opts.disallowed_tools
    assert "remote-control" not in opts.extra_args
    assert run(b._ask("Bash", {"command": "ls -la /"})) is False


def test_a_verified_call_knows_the_date(rig: Rig) -> None:
    swml = rig.swml("call-date", uvars(rig.device, rig.secret))
    sections = {s["title"]: s["body"] for s in _ai(swml)["prompt"]["pom"]}
    assert "It is now" in sections["Now"]
    assert re.search(r"\d{4}-\d{2}-\d{2}", sections["Now"])


def test_now_line_names_the_day_and_zone() -> None:
    import datetime as dt
    from zoneinfo import ZoneInfo

    now = dt.datetime(2026, 10, 12, 23, 50, tzinfo=ZoneInfo("America/Detroit"))
    assert ps.now_line(now).startswith("It is now Monday, October 12, 2026, 23:50 (EDT).")


def test_every_phone_tool_has_fillers_for_its_wait(rig: Rig) -> None:
    swml = rig.swml("call-fill", uvars(rig.device, rig.secret))
    funcs = {f["function"]: f for f in _ai(swml)["SWAIG"]["functions"]}
    for t in ps.PHONE_TOOLS:
        assert funcs[t.name]["fillers"]["default"], t.name
    assert funcs["calendar_events"]["fillers"]["default"][0] == "Looking at your calendar."
    assert funcs["weather"]["fillers"]["default"][0] == "Getting the weather."


# --- restarts and slow lookups --------------------------------------------------------------


def test_a_verified_call_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    ps.Calls(path=path).bind("call-r", "dev1")
    assert ps.Calls(path=path).device("call-r") == "dev1"  # a new process reads it back
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = ps.Calls(path=path)
    again.end("call-r")
    assert ps.Calls(path=path).device("call-r") is None


def test_an_expired_call_is_not_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    path.write_text(json.dumps({"old": ["dev1", time.time() - ps.BIND_TTL_S - 5]}))
    assert ps.Calls(path=path).device("old") is None


def test_a_nonce_stays_used_across_a_restart(paired: tuple[Path, str, str], tmp_path: Path) -> None:
    path, device, secret = paired
    nonces = tmp_path / "nonces.json"
    good = uvars(device, secret)
    assert CallCheck(ADDRESS, 60, path, nonces=nonces).check(good).ok
    assert CallCheck(ADDRESS, 60, path, nonces=nonces).check(good).reason == "nonce replayed"


def test_build_keeps_call_state_beside_the_devices(paired: tuple[Path, str, str]) -> None:
    path, _, _ = paired
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.build(cfg, FakeScout(), devices=path)
    assert agent.calls.path == path.parent / "phone_calls.json"
    assert agent.check.nonces == path.parent / "phone_nonces.json"


class SlowScout(FakeScout):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def __call__(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append((path, body, timeout))
        await self.release.wait()
        return {"status": "ok", "text": "Two truck stops near Luther."}


def test_a_slow_tool_answers_in_time_and_keeps_going(
    paired: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "TOOL_DEADLINE_S", 0.05)
    scout = SlowScout()
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), scout, AUTH, "s" * 43)
    agent.calls.bind("call-s", paired[1])
    handle = agent._tool_handler("places_near")
    args = {"what": "diesel", "near": "Luther, Michigan"}

    async def run() -> tuple[str, str]:
        first = await handle(args, {"call_id": "call-s"})
        scout.release.set()
        second = await handle(args, {"call_id": "call-s"})
        return first.to_dict()["response"], second.to_dict()["response"]

    first, second = asyncio.run(run())
    assert first == ps.STILL_LOOKING
    assert second == "Two truck stops near Luther."
    assert len(scout.calls) == 1  # the second ask picked up the same lookup


def test_a_late_answer_is_pushed_into_the_call(
    paired: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "TOOL_DEADLINE_S", 0.05)
    scout = SlowScout()
    pushed: list[tuple[str, str]] = []

    async def push(call_id: str, text: str) -> bool:
        pushed.append((call_id, text))
        return True

    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), scout, AUTH, "s" * 43, push=push)
    agent.calls.bind("call-p", paired[1])
    handle = agent._tool_handler("places_near")

    async def run() -> str:
        first = await handle({"what": "diesel"}, {"call_id": "call-p"})
        scout.release.set()
        for _ in range(50):
            if pushed:
                break
            await asyncio.sleep(0.01)
        return str(first.to_dict()["response"])

    assert asyncio.run(run()) == ps.STILL_LOOKING
    assert pushed == [
        ("call-p", ps.LATE_ANSWER.format(what="places near", text="Two truck stops near Luther."))
    ]
    assert not agent._lookups


def test_a_repeat_call_takes_the_answer_and_nothing_is_pushed(
    paired: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "TOOL_DEADLINE_S", 0.05)
    scout = SlowScout()
    pushed: list[tuple[str, str]] = []

    async def push(call_id: str, text: str) -> bool:
        pushed.append((call_id, text))
        return True

    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), scout, AUTH, "s" * 43, push=push)
    agent.calls.bind("call-r", paired[1])
    handle = agent._tool_handler("places_near")

    async def run() -> str:
        await handle({"what": "diesel"}, {"call_id": "call-r"})
        waiting = asyncio.ensure_future(handle({"what": "diesel"}, {"call_id": "call-r"}))
        await asyncio.sleep(0.01)
        scout.release.set()
        second = await waiting
        await asyncio.sleep(0.05)
        return str(second.to_dict()["response"])

    assert asyncio.run(run()) == "Two truck stops near Luther."
    assert pushed == []


def test_the_pusher_needs_the_rest_credentials() -> None:
    assert ps.pusher(PhoneConfig(space="x.signalwire.com", project_id="p")) is None
    assert ps.pusher(PhoneConfig(space="x.signalwire.com", project_id="p", api_token="t"))  # noqa: S106 is not None


def test_a_late_ask_is_pushed_into_the_call(
    paired: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "ASK_POLL_S", 0.0)
    answers = [{"status": "pending", "text": "still"}, {"status": "pending", "text": "still"}]
    asked: list[str] = []

    async def scout(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        asked.append(body["text"])
        return answers.pop(0) if answers else {"status": "ok", "text": "You parked by the barn."}

    pushed: list[tuple[str, str]] = []

    async def push(call_id: str, text: str) -> bool:
        pushed.append((call_id, text))
        return True

    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    agent = ps.PhoneAgent(cfg, CallCheck(ADDRESS, 60, paired[0]), scout, AUTH, "s" * 43, push=push)
    agent.calls.bind("call-a", paired[1])

    async def run() -> str:
        first = await agent._ask({"request": "where did I park"}, {"call_id": "call-a"})
        for _ in range(50):
            if pushed:
                break
            await asyncio.sleep(0.01)
        return str(first.to_dict()["response"])

    assert asyncio.run(run()) == ps.STILL_LOOKING
    assert pushed == [("call-a", ps.LATE_ANSWER.format(what="Scout", text="You parked by the barn."))]
    assert asked == ["where did I park"] * 3
    assert not agent._asks
