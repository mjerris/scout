"""The user's own MCP servers (extmcp.py): tools.toml, the rules, the room's and the
phone's sessions, the phone's two steps, and `scout status`. The host's client is a fake
here; tests/test_extmcp_live.py runs the real CLI against tests/fake_mcp_server.py."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from test_phone import AUTH, KEY, PUBLIC, ADDRESS, Rig, _ai, _functions, paired, uvars
from test_routing import make, run

import scout.phone_service as ps
from scout import extmcp, trust
from scout.assistant import PHONE
from scout.brain import PHONE_PREPARED, Brain, PhoneBrain
from scout.config import ClaudeConfig, PhoneConfig
from scout.mac import ToolError
from scout.memory import Memory
from scout.phone_devices import CallCheck
from scout.rules import Rules
from scout.tools import Timers, build_server

_ = paired  # the fixture, used by name below

URL = "https://desk.example.com/mcp"

FILE = """\
[[server]]
from_claude = "acme"
channels = ["room", "phone"]
private = true
hint = "Acme's help desk: tickets and customers."

  [[server.tool]]
  name = "ticket_create"
  asks = true
  phone = true
  readback = "File a ticket titled {title}?"

  [[server.tool]]
  name = "ticket_close"
  asks = true
  asks_when = "confirm"

  [[server.tool]]
  destructive = true
  deny = true

  [[server.tool]]
  pattern = "ticket_(show|search)"
  phone = true

[[server]]
name = "widgets"
command = "widgets-mcp"
args = ["--fast"]
env_file = "widgets.env"
env = { WIDGET_KEY = "${WIDGET_KEY}" }
"""

TOOLS = {
    "ticket_show": {"readOnly": True},
    "ticket_search": {"readOnly": True},
    "ticket_create": {},
    "ticket_close": {},
    "ticket_purge": {"destructive": True},
    "ticket_note": {},
    "queue_stats": {"readOnly": True},
}


def write_private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A data folder with tools.toml and an env file, and a Claude Code config with acme."""
    write_private(tmp_path / "tools.toml", FILE)
    write_private(tmp_path / "widgets.env", "# the key\nWIDGET_KEY='sekrit-123'\n")
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"acme": {"type": "http", "url": URL}}}))
    return tmp_path


def catalog(home: Path) -> extmcp.Catalog:
    return extmcp.load(home / "tools.toml", home / "claude.json", home, home)


class FakeQuery:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reply: dict[str, Any] = {"content": [{"type": "text", "text": "ticket 61723 filed"}]}

    async def _send_control_request(self, request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        self.sent.append(request)
        return self.reply


class FakeClient:
    """The host's SDK client: connect, the status rows, reconnects, mcp_call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.connected = self.closed = False
        self.reconnected: list[str] = []
        self._query = FakeQuery()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.closed = True

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": self.rows}

    async def reconnect_mcp_server(self, server_name: str) -> None:
        self.reconnected.append(server_name)


def acme_row(status: str = "connected") -> dict[str, Any]:
    tools = [{"name": n, "annotations": a} for n, a in TOOLS.items()] if status == "connected" else []
    return {"name": "acme", "status": status, "tools": tools}


def external(
    home: Path, rows: list[dict[str, Any]] | None = None, mode: str = "balanced"
) -> tuple[extmcp.External, list[FakeClient], list[tuple[dict[str, Any], dict[str, str]]]]:
    made: list[FakeClient] = []
    given: list[tuple[dict[str, Any], dict[str, str]]] = []

    def factory(servers: dict[str, Any], env: dict[str, str], cwd: str) -> FakeClient:
        given.append((servers, env))
        c = FakeClient(
            rows
            if rows is not None
            else [acme_row(), {"name": "widgets", "status": "failed", "error": "spawn widgets-mcp ENOENT"}]
        )
        made.append(c)
        return c

    ext = extmcp.External(
        str(home),
        lambda: mode,
        path=home / "tools.toml",
        claude_json=home / "claude.json",
        status_path=home / "state" / "mcp_status.json",
        client_factory=cast(extmcp.ClientFactory, factory),
        data=home,
    )
    return ext, made, given


# --- the file --------------------------------------------------------------------------------


def test_a_claude_code_server_is_used_as_it_is_there(home: Path) -> None:
    cat = catalog(home)
    assert cat.problems == []
    acme = cat.get("acme")
    assert acme is not None and acme.source == "claude" and acme.private
    assert acme.config == {"type": "http", "url": URL}  # the same name and URL: its sign-in is found
    assert acme.channels == {"room", "phone"} and acme.hint.startswith("Acme's help desk")


def test_secrets_travel_in_the_environment_not_the_config(home: Path) -> None:
    w = catalog(home).get("widgets")
    assert w is not None and w.source == "scout"
    var = "SCOUT_MCP_WIDGETS_ENV_WIDGET_KEY"
    assert w.config == {
        "type": "stdio",
        "command": "widgets-mcp",
        "args": ["--fast"],
        "env": {"WIDGET_KEY": "${" + var + "}"},
    }
    assert w.env == {var: "sekrit-123"}
    assert "sekrit" not in json.dumps(w.config) and "sekrit" not in repr(w)


def test_claude_code_headers_move_into_the_environment_too(home: Path) -> None:
    (home / "claude.json").write_text(
        json.dumps(
            {"mcpServers": {"acme": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer tok"}}}}
        )
    )
    acme = catalog(home).get("acme")
    assert acme is not None
    assert acme.config["headers"] == {"Authorization": "${SCOUT_MCP_ACME_HEADERS_AUTHORIZATION}"}
    assert acme.env == {"SCOUT_MCP_ACME_HEADERS_AUTHORIZATION": "Bearer tok"}


def test_local_scope_wins_over_user_scope(home: Path) -> None:
    other = "https://local.example.com/mcp"
    data = {
        "mcpServers": {"acme": {"type": "http", "url": URL}},
        "projects": {str(home): {"mcpServers": {"acme": {"type": "http", "url": other}}}},
    }
    (home / "claude.json").write_text(json.dumps(data))
    acme = catalog(home).get("acme")
    assert acme is not None and acme.config["url"] == other


def test_problems_are_said_not_fatal(home: Path) -> None:
    write_private(
        home / "tools.toml",
        """
[[server]]
from_claude = "nobody"
[[server]]
name = "acme2"
from_claude = "acme"
[[server]]
name = "voice_app"
url = "https://x.example.com/mcp"
[[server]]
name = "nothing"
[[server]]
name = "ok"
url = "https://ok.example.com/mcp"
channels = ["room", "desk"]
[[server]]
name = "fine"
url = "https://fine.example.com/mcp"
  [[server.tool]]
  pattern = "("
  [[server.tool]]
  colour = "blue"
""",
    )
    cat = catalog(home)
    assert [s.name for s in cat.servers] == ["fine"]
    text = "\n".join(cat.problems)
    for want in (
        "nobody: not among Claude Code",
        "keeps its Claude Code name",
        "voice_app",
        "needs from_claude",
        "channels must",
        "bad pattern",
        "unknown keys",
    ):
        assert want in text, want


def test_a_readable_env_file_is_refused(home: Path) -> None:
    (home / "widgets.env").chmod(0o644)
    cat = catalog(home)
    assert cat.get("widgets") is None
    assert any("widgets: env_file not used" in p and "chmod 600" in p for p in cat.problems)


def test_a_readable_tools_file_is_reported(home: Path) -> None:
    (home / "tools.toml").chmod(0o644)
    cat = catalog(home)
    assert cat.get("acme") is not None and any("readable only by you" in p for p in cat.problems)


def test_no_file_no_servers(tmp_path: Path) -> None:
    cat = extmcp.load(tmp_path / "tools.toml", tmp_path / "claude.json")
    assert cat.servers == [] and cat.problems == []


# --- the rules ------------------------------------------------------------------------------


def test_defaults_ask_unless_read_only_and_nothing_on_the_phone(home: Path) -> None:
    acme = catalog(home).get("acme")
    assert acme is not None
    d = extmcp.decide
    assert d(acme, "queue_stats", TOOLS["queue_stats"], "room") == extmcp.Decision(True, False)
    assert d(acme, "ticket_note", {}, "room").asks  # no hints: treated as a write
    assert not d(acme, "queue_stats", TOOLS["queue_stats"], "phone").allowed  # not marked phone
    assert d(acme, "ticket_show", TOOLS["ticket_show"], "phone") == extmcp.Decision(True, False)


def test_rules_deny_ask_and_read_back(home: Path) -> None:
    acme = catalog(home).get("acme")
    assert acme is not None
    purge = extmcp.decide(acme, "ticket_purge", TOOLS["ticket_purge"], "room")
    assert not purge.allowed and "turned off" in purge.why
    create = extmcp.decide(acme, "ticket_create", {}, "phone")
    assert create.allowed and create.asks and create.readback == "File a ticket titled {title}?"
    # asks_when: the server's own confirm step asks; its dry run doesn't
    assert not extmcp.decide(acme, "ticket_close", {}, "room", {"number": 5}).asks
    assert extmcp.decide(acme, "ticket_close", {}, "room", {"number": 5, "confirm": True}).asks


def test_strict_privacy_turns_a_private_server_off(home: Path) -> None:
    ext, _, _ = external(home, mode="strict")
    assert [s.name for s in ext.servers("room")] == ["widgets"]
    d = ext.decision("mcp__acme__queue_stats", "room")
    assert d is not None and not d.allowed and "strict privacy" in d.why


def test_read_backs() -> None:
    assert (
        extmcp.describe("acme", "ticket_create", {"title": "Login  broken"}, "File {title}, about {thing}?")[
            0
        ]
        == "File Login broken, about ?"
    )
    spoken, detail = extmcp.describe("acme", "ticket_note", {"number": 7, "confirm": True})
    assert spoken == "Use ticket note on acme, number 7?" and '"confirm": true' in detail
    assert extmcp.describe("acme", "x", {}, "{repo|the usual repo}: {title}")[0] == "the usual repo:"
    assert extmcp.describe("acme", "x", {"repo": "a/b"}, "{repo|the usual repo}")[0] == "a/b"


def test_result_text() -> None:
    assert extmcp.result_text({"content": '{"result":"42 widgets"}'}) == "42 widgets"
    assert (
        extmcp.result_text(
            {"content": [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]}
        )
        == "a\nb"
    )
    with pytest.raises(ToolError, match="no such ticket"):
        extmcp.result_text({"content": [{"type": "text", "text": "no such ticket"}], "isError": True})
    long = "\n".join(f"line {i}" for i in range(500))
    cut = extmcp.spoken(long, 100)
    assert len(cut) < 160 and "more characters not read" in cut


# --- the host ------------------------------------------------------------------------------


def test_the_host_records_each_server_and_writes_status(home: Path) -> None:
    ext, made, given = external(home)
    run(ext.start())
    servers, env = given[0]
    assert set(servers) == {"acme", "widgets"} and env == {"SCOUT_MCP_WIDGETS_ENV_WIDGET_KEY": "sekrit-123"}
    assert ext.status["acme"] == {"state": "up", "tools": len(TOOLS), "detail": ""}
    assert ext.status["widgets"]["state"] == "failing" and "ENOENT" in ext.status["widgets"]["detail"]
    saved = json.loads((home / "state" / "mcp_status.json").read_text())
    assert saved["servers"]["acme"]["state"] == "up" and "sekrit" not in json.dumps(saved)
    assert ext.denied("room") == ["mcp__acme__ticket_purge"]  # known now: hidden from Claude
    assert set(ext.denied("phone")) == {f"mcp__acme__{t}" for t in TOOLS} - {
        "mcp__acme__ticket_create",
        "mcp__acme__ticket_show",
        "mcp__acme__ticket_search",
    }
    run(ext.check())
    assert made[0].reconnected == ["widgets"]  # a failing server gets a reconnect
    run(ext.restart())
    assert made[0].closed and len(made) == 2
    run(ext.close())
    assert made[1].closed


def test_auth_needed_says_how_to_sign_in(home: Path) -> None:
    ext, _, _ = external(home, rows=[acme_row("needs-auth")])
    run(ext.start())
    assert ext.status["acme"]["state"] == "auth needed"
    assert "claude mcp login acme" in ext.status["acme"]["detail"]
    assert ext.status["widgets"]["state"] == "failing"  # not reported: never silent


def test_a_host_that_cannot_start_is_reported(home: Path) -> None:
    def broken(servers: dict[str, Any], env: dict[str, str], cwd: str) -> FakeClient:
        raise OSError("no claude binary")

    ext = extmcp.External(
        str(home),
        path=home / "tools.toml",
        claude_json=home / "claude.json",
        status_path=home / "s.json",
        client_factory=cast(extmcp.ClientFactory, broken),
        data=home,
    )
    run(ext.start())
    assert all(v["state"] == "failing" and "no claude binary" in v["detail"] for v in ext.status.values())


def test_call_runs_on_the_host(home: Path) -> None:
    ext, made, _ = external(home)
    with pytest.raises(ToolError, match="aren't connected"):
        run(ext.call("mcp__acme__ticket_create", {"title": "x"}))

    async def go() -> str:
        await ext.start()
        return await ext.call("mcp__acme__ticket_create", {"title": "x"})

    assert run(go()) == "ticket 61723 filed"
    assert made[0]._query.sent == [
        {"subtype": "mcp_call", "tool": "mcp__acme__ticket_create", "arguments": {"title": "x"}}
    ]


def test_status_lines(home: Path) -> None:
    ext, _, _ = external(home)
    lines = extmcp.status_lines(
        home / "tools.toml",
        home / "state" / "mcp_status.json",
        cwd=home,
        claude_json=home / "claude.json",
        data=home,
    )
    assert lines[0].startswith("acme: not checked yet")
    run(ext.start())
    lines = extmcp.status_lines(
        home / "tools.toml",
        home / "state" / "mcp_status.json",
        cwd=home,
        claude_json=home / "claude.json",
        data=home,
    )
    assert lines[0] == f"acme: up, {len(TOOLS)} tools (phone, room)"
    assert lines[1] == "widgets: failing (room): spawn widgets-mcp ENOENT"
    assert extmcp.status_lines(home / "none.toml") == ["none (no tools.toml)"]


# --- the room's session ---------------------------------------------------------------------


async def no_answer(spoken: str, detail: str) -> bool | None:
    return None


def room(
    home: Path, tmp_path: Path, answers: list[bool | str | None] | None = None
) -> tuple[Brain, list[str]]:
    ext, _, _ = external(home)
    run(ext.start())
    asked: list[str] = []

    async def confirm(spoken: str, detail: str) -> bool | str | None:
        asked.append(spoken)
        return (answers or [True]).pop(0)

    server, names = build_server(Timers(lambda label: None))
    return Brain(
        ClaudeConfig(), confirm, server, names, Rules(tmp_path / "r.json"), lambda k, t: None, ext
    ), asked


def test_the_room_session_gets_the_servers(home: Path, tmp_path: Path) -> None:
    b, _ = room(home, tmp_path)
    opts = b._options()
    servers = cast(dict[str, Any], opts.mcp_servers)
    assert servers["acme"] == {"type": "http", "url": URL} and "voice_app" in servers
    assert opts.env["SCOUT_MCP_WIDGETS_ENV_WIDGET_KEY"] == "sekrit-123"  # environment, not argv
    settings = json.loads(cast(str, opts.settings))
    assert {"mcp__acme", "mcp__widgets"} <= set(settings["permissions"]["ask"])
    assert "mcp__acme__ticket_purge" in opts.disallowed_tools
    prompt = cast(dict[str, Any], opts.system_prompt)["append"]
    assert "acme (tools mcp__acme__*): Acme's help desk" in prompt


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(signal=None, suggestions=[])


def test_room_reads_run_writes_ask_denied_never(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "_trust", trust.Trust(tmp_path / "trust.json"))
    b, asked = room(home, tmp_path, [True, "always", False])
    allow = run(b._can_use_tool("mcp__acme__queue_stats", {}, _ctx()))
    assert isinstance(allow, PermissionResultAllow) and asked == []
    assert isinstance(
        run(b._can_use_tool("mcp__acme__ticket_create", {"title": "Login broken"}, _ctx())),
        PermissionResultAllow,
    )
    assert asked == ["File a ticket titled Login broken?"]
    # "always" is just this once: the next one asks again
    assert isinstance(
        run(b._can_use_tool("mcp__acme__ticket_note", {"number": 3}, _ctx())), PermissionResultAllow
    )
    assert isinstance(
        run(b._can_use_tool("mcp__acme__ticket_note", {"number": 3}, _ctx())), PermissionResultDeny
    )
    assert len(asked) == 3
    denied = run(b._can_use_tool("mcp__acme__ticket_purge", {}, _ctx()))
    assert isinstance(denied, PermissionResultDeny) and "turned off" in denied.message and len(asked) == 3
    logged = [e["tool"] for e in trust.default().log]
    assert logged == ["acme.ticket_create", "acme.ticket_note", "acme.ticket_note"]


def test_strict_hook_follows_the_same_rules(home: Path, tmp_path: Path) -> None:
    b, asked = room(home, tmp_path)
    allow = run(b._pre_tool_use({"tool_name": "mcp__acme__queue_stats", "tool_input": {}}, None, None))
    assert allow["hookSpecificOutput"]["permissionDecision"] == "allow" and asked == []
    deny = run(b._pre_tool_use({"tool_name": "mcp__acme__ticket_purge", "tool_input": {}}, None, None))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_fresh_room_session_rereads_the_file(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout import brain as brain_mod

    class Client:
        def __init__(self, opts: Any) -> None:
            self.opts = opts

        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    monkeypatch.setattr(brain_mod, "ClaudeSDKClient", Client)
    b, _ = room(home, tmp_path)
    assert b.external is not None

    async def go() -> Any:
        await b._ensure()
        b._used(now=0.0, context_tokens=10)  # long idle: stale
        write_private(home / "tools.toml", FILE.replace('hint = "Acme', 'hint = "New: Acme'))
        client = await b._ensure()
        assert b._restart is not None
        await b._restart
        return client

    client = run(go())
    assert "New: Acme" in client.opts.system_prompt["append"]


# --- the phone ------------------------------------------------------------------------------


def phone_brain(home: Path, tmp_path: Path) -> PhoneBrain:
    ext, _, _ = external(home)
    run(ext.start())
    server, names = build_server(Timers(lambda label: None), memory=Memory(tmp_path / "memory.json"))
    return PhoneBrain(ClaudeConfig(), server, names, set(), Rules(tmp_path / "rules.json"), ext, only="acme")


def test_the_phone_session_has_only_that_servers_phone_tools(home: Path, tmp_path: Path) -> None:
    b = phone_brain(home, tmp_path)
    opts = b._options()
    assert set(cast(dict[str, Any], opts.mcp_servers)) == {"voice_app", "acme"}  # widgets isn't on the phone
    assert "mcp__voice_app__weather" in opts.disallowed_tools  # none of Scout's own
    assert "mcp__acme__queue_stats" in opts.disallowed_tools  # not marked phone
    assert "mcp__acme__ticket_show" not in opts.disallowed_tools
    assert "Phone, acme" in cast(dict[str, Any], opts.system_prompt)["append"]
    assert b._forbidden("mcp__acme__ticket_show", {"number": 1}) is None
    assert "isn't offered on the phone" in (b._forbidden("mcp__acme__queue_stats", {}) or "")
    assert b._forbidden("mcp__acme__ticket_create", {"title": "Login broken"}) == PHONE_PREPARED
    assert b.prepared == (
        "mcp__acme__ticket_create",
        {"title": "Login broken"},
        "File a ticket titled Login broken?",
    )


def test_ask_via_a_server_returns_the_action(home: Path, tmp_path: Path) -> None:
    class Prepares:
        prepared: tuple[str, dict[str, Any], str] | None = None

        async def ask(self, text: str) -> Any:
            self.prepared = ("mcp__acme__ticket_create", {"title": "x"}, "File a ticket titled x?")
            yield "text", "I'd file it."
            yield "result", None

        async def reset(self) -> None:
            pass

    async def go() -> list[dict[str, Any]]:
        r = make()
        ext, _, _ = external(home)
        await ext.start()
        r.a.external = ext
        r.a._server_brains["acme"] = cast(PhoneBrain, Prepares())
        return [
            await r.a.ask(PHONE, "file a ticket called x", via="acme"),
            await r.a.ask(PHONE, "hi", via="widgets"),
        ]

    out, refused = run(go())
    assert out == {
        "status": "ok",
        "text": "File a ticket titled x?",
        "action": {
            "name": "mcp__acme__ticket_create",
            "args": {"title": "x"},
            "said": "File a ticket titled x?",
        },
    }
    assert refused["status"] == "error" and "isn't offered on the phone" in refused["error"]


def test_a_confirmed_phone_action_runs_on_the_host(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "_trust", trust.Trust(tmp_path / "trust.json"))

    async def go() -> tuple[list[dict[str, Any]], FakeClient]:
        r = make()
        ext, made, _ = external(home)
        await ext.start()
        r.a.external = ext
        name, args = "mcp__acme__ticket_create", {"title": "x"}
        return [
            await r.a.run_shared_tool(PHONE, name, args, preview=True),
            await r.a.run_shared_tool(PHONE, name, args),  # no yes: refused
            await r.a.run_shared_tool("desk#1", name, args, confirmed=True),  # not the phone
            await r.a.run_shared_tool(
                PHONE, "mcp__acme__queue_stats", {}, confirmed=True
            ),  # not on the phone
            await r.a.run_shared_tool(PHONE, name, args, confirmed=True),
        ], made[0]

    (preview, unasked, desk, off, done), client = run(go())
    assert preview == {"status": "ok", "text": "File a ticket titled x?"}
    assert unasked["status"] == desk["status"] == off["status"] == "error"
    assert done == {"status": "ok", "text": "ticket 61723 filed"}
    assert len(client._query.sent) == 1  # only the confirmed one reached the server
    assert [e["how"] for e in trust.default().log] == ["phone"]


def test_private_server_answers_are_private(home: Path) -> None:
    async def go() -> tuple[bool, bool, bool]:
        r = make()
        ext, _, _ = external(home)
        r.a.external = ext
        return (
            r.a._private_tool("mcp__acme__ticket_show"),
            r.a._private_tool("mcp__widgets__x"),
            r.a._private_tool("mail_recent"),
        )

    assert run(go()) == (True, False, True)


@pytest.fixture
def ext_rig(paired: tuple[Path, str, str], home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    monkeypatch.setenv("SWML_PROXY_URL_BASE", PUBLIC)
    r = Rig(paired)
    r.client.close()
    cfg = PhoneConfig(enabled=True, signing_key=KEY, public_url=PUBLIC, address=ADDRESS)
    servers = [s for s in catalog(home).servers if s.name == "acme"]
    r.agent = ps.PhoneAgent(
        cfg, CallCheck(ADDRESS, 60, r.path), r.scout, AUTH, "s" * 43, servers=lambda: servers
    )
    from fastapi.testclient import TestClient

    r.client = TestClient(r.agent.get_app())
    yield r
    r.client.close()


def test_a_call_gets_a_function_per_phone_server(ext_rig: Rig) -> None:
    swml = ext_rig.swml("call-x1", uvars(ext_rig.device, ext_rig.secret))
    assert "mcp_acme" in _functions(swml)
    fn = next(f for f in _ai(swml)["SWAIG"]["functions"] if f["function"] == "mcp_acme")
    assert "Acme's help desk" in fn["description"] and "confirm_action" in fn["description"]


def test_the_phone_function_asks_scout_via_the_server(ext_rig: Rig) -> None:
    swml = ext_rig.swml("call-x2", uvars(ext_rig.device, ext_rig.secret))
    ext_rig.scout.reply = {"status": "ok", "text": "3 open tickets."}
    out = ext_rig.swaig(swml, "call-x2", "mcp_acme", {"request": "how many open tickets"})
    assert out["response"] == "3 open tickets."
    assert ext_rig.scout.calls[-1] == (
        "/api/ask",
        {"agent": "phone", "text": "how many open tickets", "via": "acme"},
        ps.ASK_TIMEOUT_S,
    )


def test_a_write_by_phone_takes_two_steps(ext_rig: Rig) -> None:
    swml = ext_rig.swml("call-x3", uvars(ext_rig.device, ext_rig.secret))
    action = {"name": "mcp__acme__ticket_create", "args": {"title": "x"}, "said": "File a ticket titled x?"}
    ext_rig.scout.reply = {"status": "ok", "text": action["said"], "action": action}
    out = ext_rig.swaig(swml, "call-x3", "mcp_acme", {"request": "file a ticket called x"})
    assert out["response"].startswith("Not done yet. Would do: File a ticket titled x?")
    code, name, args, _ = ext_rig.agent._pending["call-x3"]
    assert name == "mcp__acme__ticket_create" and args == {"title": "x"}
    assert out["action"][0]["set_global_data"]["semantic_state"] == {
        "pending_action": action["said"],
        "pending_code": code,
    }

    async def confirm() -> Any:
        return await ext_rig.agent._act("call-x3", code, name, args)

    ext_rig.scout.reply = {"status": "ok", "text": "filed"}
    assert asyncio.run(confirm())["text"] == "filed"
    assert ext_rig.scout.calls[-1][1] == {
        "agent": "phone",
        "name": "mcp__acme__ticket_create",
        "args": {"title": "x"},
        "confirmed": True,
    }


def test_a_private_servers_function_keeps_the_call_private(ext_rig: Rig) -> None:
    ext_rig.swml("call-x4", uvars(ext_rig.device, ext_rig.secret))
    log = [
        {"role": "user", "content": "how many open tickets"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "mcp_acme", "arguments": "{}"}}],
        },
        {"role": "assistant", "content": "Three."},
    ]
    [x] = ps.call_exchanges(cast(list[dict[str, Any]], log))
    assert x["private"]


def test_phone_servers_reads_the_config(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.config import Config

    monkeypatch.setattr(ps, "load", lambda: Config())
    monkeypatch.setattr(extmcp, "PATH", home / "tools.toml")
    monkeypatch.setattr(extmcp, "CLAUDE_JSON", home / "claude.json")
    monkeypatch.setattr(ps, "DATA", home)
    assert [s.name for s in ps.phone_servers()] == ["acme"]  # widgets has no phone rule
    assert os.environ.get("SCOUT_MCP_WIDGETS_ENV_WIDGET_KEY") is None  # never put in this process's env


def test_the_example_file_loads(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "tools.example.toml"
    write_private(tmp_path / "tools.toml", example.read_text())
    write_private(tmp_path / "widgets.env", "WIDGETS_KEY=k\n")
    write_private(tmp_path / "wiki.env", "WIKI_TOKEN=t\n")
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"acme": {"type": "http", "url": URL}}}))
    cat = extmcp.load(tmp_path / "tools.toml", tmp_path / "claude.json", tmp_path, tmp_path)
    assert cat.problems == [] and [s.name for s in cat.servers] == ["acme", "widgets", "wiki"]
    acme = cat.get("acme")
    assert acme is not None
    assert extmcp.describe(
        "acme",
        "ticket_create",
        {"title": "Late order"},
        extmcp.decide(acme, "ticket_create", {}, "room").readback,
    )[0] == ("File an Acme ticket for no customer titled Late order?")
