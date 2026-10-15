"""The host session for real: Claude Code's bundled CLI (through the Agent SDK) against
tests/fake_mcp_server.py over stdio and over HTTP with a bearer token from an env file.
No model turn runs and nothing reaches the network: the CLI only connects to the fake
servers. Each test has its own CLI config folder (CLAUDE_CONFIG_DIR, under the test's
tmp_path), its own free port, and stops every process it starts."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scout import extmcp
from scout.mac import ToolError

FAKE = Path(__file__).with_name("fake_mcp_server.py")
TOKEN = "fake-token-for-tests"  # noqa: S105 (the fake server's, made up)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def http_server() -> Iterator[int]:
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 (this interpreter, the test's own server)
        [sys.executable, str(FAKE), "http", str(port), TOKEN],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if proc.poll() is not None:
                pytest.fail("the fake HTTP server exited")
            time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_the_host_connects_reports_and_calls(tmp_path: Path, http_server: int) -> None:
    calls_log = tmp_path / "calls.log"
    (tmp_path / "web.env").write_text(f"FAKE_TOKEN={TOKEN}\n")
    (tmp_path / "web.env").chmod(0o600)
    (tmp_path / "tools.toml").write_text(
        f"""
[[server]]
name = "fakeio"
command = "{sys.executable}"
args = ["{FAKE}", "stdio"]
env = {{ FAKE_MCP_LOG = "{calls_log}" }}
  [[server.tool]]
  destructive = true
  deny = true

[[server]]
name = "fakeweb"
url = "http://127.0.0.1:{http_server}/mcp"
env_file = "web.env"
headers = {{ Authorization = "Bearer ${{FAKE_TOKEN}}" }}

[[server]]
name = "fakeopen"
url = "http://127.0.0.1:{http_server}/mcp"

[[server]]
name = "fakegone"
command = "{tmp_path / "no-such-server"}"
"""
    )
    (tmp_path / "tools.toml").chmod(0o600)
    cli_home = tmp_path / "cli"
    cli_home.mkdir()

    def factory(servers: dict[str, Any], env: dict[str, str], cwd: str) -> Any:
        return extmcp.sdk_client(servers, {**env, "CLAUDE_CONFIG_DIR": str(cli_home)}, cwd)

    ext = extmcp.External(
        str(tmp_path),
        path=tmp_path / "tools.toml",
        claude_json=tmp_path / "claude.json",
        status_path=tmp_path / "state" / "mcp_status.json",
        client_factory=factory,
        data=tmp_path,
    )
    assert ext.catalog.problems == []

    async def go() -> tuple[str, str, str]:
        try:
            await ext.start()
            local = await ext.call("mcp__fakeio__lookup", {"id": "7"})
            web = await ext.call("mcp__fakeweb__count", {})
            with pytest.raises(ToolError, match="fakeopen"):
                await ext.call("mcp__fakeopen__count", {})
            seen = await ext.call("mcp__fakeweb__calls", {})
            return local, web, seen
        finally:
            await ext.close()

    local, web, seen = asyncio.run(go())
    assert local == "widget 7: blue, 3 parts"
    assert web == "42 widgets" and "count" in seen  # the bearer token from the env file got there
    st = ext.status
    assert st["fakeio"]["state"] == st["fakeweb"]["state"] == "up"
    assert st["fakeio"]["tools"] == 5
    assert st["fakeopen"]["state"] in ("failing", "auth needed")  # 401: no token
    assert st["fakegone"]["state"] == "failing" and st["fakegone"]["detail"]
    # the servers' own hints came through: reads run, writes ask, destructive is turned off
    assert ext.decision("mcp__fakeio__count", "room") == extmcp.Decision(True, False)
    file_thing = ext.decision("mcp__fakeio__file_thing", "room")
    assert file_thing is not None and file_thing.asks
    assert ext.denied("room", "fakeio") == ["mcp__fakeio__wipe"]
    assert calls_log.read_text().split() == ["lookup"]
    saved = json.loads((tmp_path / "state" / "mcp_status.json").read_text())
    assert TOKEN not in json.dumps(saved)
