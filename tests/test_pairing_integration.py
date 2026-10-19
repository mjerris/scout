"""End to end against a running Scout web server in its own process (tests/pairing_server.py)
on a free port, never 8765 (the live app's): a DNS-rebinding request is refused before it
can ask to pair, and a real request is answered with `scoutctl phone pair list | approve |
deny` (python -m scout.phone_devices) in yet another process. The server is stopped after."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
MAC = "mini.tail1234.ts.net"
OWNER = "mike@example.com"
PHONE_IP = "100.101.102.103"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    assert port != 8765
    return port


@pytest.fixture
def scout(tmp_path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    home = REPO / ".tmp" / "test-home" / f"integration-{os.getpid()}-{time.monotonic_ns()}"
    home.mkdir(parents=True)
    port = _free_port()
    (home / "config.toml").write_text(f"[web]\nport = {port}\n")
    env = {**os.environ, "SCOUT_HOME": str(home)}
    log = (home / "server.log").open("w")
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, str(REPO / "tests" / "pairing_server.py"), str(port)],
        env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while "READY" not in (home / "server.log").read_text():  # its log goes there too
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError("the server didn't start:\n" + (home / "server.log").read_text())
            time.sleep(0.05)
        yield port, env
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        import shutil

        shutil.rmtree(home, ignore_errors=True)


def _post(
    port: int, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    req = urllib.request.Request(  # a fixed http://127.0.0.1 URL
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        text = exc.read()
        try:
            return exc.code, json.loads(text or b"null")
        except ValueError:
            return exc.code, text.decode(errors="replace")


def _scoutctl(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scout.phone_devices", *args],
        env=env, cwd=REPO, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def test_rebinding_is_refused_and_a_real_request_is_answered_from_the_cli(
    scout: tuple[int, dict[str, str]],
) -> None:
    port, env = scout
    devices = Path(env["SCOUT_HOME"]) / "state" / "phone_devices.json"
    evil = f"evil.example:{port}"
    forged = {"Host": evil, "Origin": f"http://{evil}", "X-Forwarded-Proto": "https", "X-Forwarded-Host": MAC,
              "X-Forwarded-For": PHONE_IP, "Tailscale-User-Login": OWNER}  # fmt: skip
    status, _ = _post(port, "/api/phone/pair/request", {"name": "Owner phone"}, forged)
    assert status == 421
    assert "No device is asking to pair." in _scoutctl(env, "pair", "list").stdout
    assert not devices.exists()

    # Through Tailscale Serve (as the fake tailscaled vouches): named as the owner, but waiting.
    served = {"Host": MAC, "X-Forwarded-Host": MAC, "X-Forwarded-Proto": "https", "X-Forwarded-For": PHONE_IP,
              "Tailscale-User-Login": OWNER, "User-Agent": "Scout/1.0"}  # fmt: skip
    status, started = _post(port, "/api/phone/pair/request", {"name": "Mike's iPhone"}, served)
    assert status == 200 and set(started) == {"request", "match", "expires_at"}
    listed = _scoutctl(env, "pair", "list")
    assert listed.returncode == 0
    line = listed.stdout.strip()
    assert "Mike's iPhone" in line and f"code {started['match']}" in line and f"{OWNER} (yours)" in line
    rid = line.split()[0]
    approved = _scoutctl(env, "pair", "approve", rid)
    assert approved.returncode == 0 and "Approved: paired as d" in approved.stdout
    status, got = _post(port, "/api/phone/pair/status", {"request": started["request"], "wait": 5})
    assert got["status"] == "approved" and got["secret"]
    assert json.loads(devices.read_text())[got["device"]]["name"] == "Mike's iPhone"
    again = _scoutctl(env, "pair", "deny", rid)
    assert again.returncode == 1 and "Already answered (approved by cli)" in again.stdout

    # A second one, denied.
    status, started = _post(port, "/api/phone/pair/request", {"name": "Guest"})
    rid = _scoutctl(env, "pair", "list").stdout.split()[0]
    assert _scoutctl(env, "pair", "deny", rid).stdout.strip() == "Denied."
    assert _post(port, "/api/phone/pair/status", {"request": started["request"]})[1]["status"] == "denied"
    assert len(json.loads(devices.read_text())) == 1
