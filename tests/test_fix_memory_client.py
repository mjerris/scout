"""Review fixes in the memory client: calls to 127.0.0.1 never go through HTTP_PROXY
(the token and payloads stay on the Mac), and a stuck service fails fast."""

import json
import shutil
import socket
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scout import memory_client
from scout.mac import ToolError

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp" / "fix-memory-tests"


@pytest.fixture
def token() -> Iterator[Path]:
    d = SCRATCH / uuid.uuid4().hex[:12]
    d.mkdir(parents=True)
    (d / "memory.token").write_text("secret")
    yield d / "memory.token"
    shutil.rmtree(d, ignore_errors=True)


def recorder() -> tuple[ThreadingHTTPServer, list[dict[str, Any]]]:
    """A local server that answers {"count": 3} and records who asked what."""
    seen: list[dict[str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"count": 3}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_calls_to_the_service_bypass_http_proxy(token: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, to_service = recorder()
    proxy, to_proxy = recorder()
    try:
        for k in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
        monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen's opener reads the env afresh
        c = memory_client.Client(f"http://127.0.0.1:{service.server_address[1]}", token)
        assert memory_client.Store(c).count() == 3
        assert to_proxy == []  # neither the token nor the payload went to the proxy
        assert to_service == [{"path": "/v1/docs/count", "auth": "Bearer secret"}]
    finally:
        service.shutdown()
        proxy.shutdown()
        service.server_close()
        proxy.server_close()


def test_a_stuck_service_fails_fast(token: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stuck = socket.socket()
    stuck.bind(("127.0.0.1", 0))
    stuck.listen(1)  # accepts the connection, never answers
    try:
        monkeypatch.setattr(memory_client, "TIMEOUT", 0.3)
        c = memory_client.Client(f"http://127.0.0.1:{stuck.getsockname()[1]}", token)
        t0 = time.monotonic()
        with pytest.raises(ToolError, match="memory service isn't running"):
            memory_client.RemoteMemory(c).relevant("who is my dentist")
        assert time.monotonic() - t0 < 2
    finally:
        stuck.close()


def test_long_calls_get_a_longer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: dict[str, float | None] = {}

    def call(self: Any, path: str, payload: Any = None, timeout: float | None = None) -> dict[str, Any]:
        timeouts[f"{path} {payload.get('semantic', '')}".strip()] = timeout
        return {"changed": 0, "removed": 0, "hits": [], "facts": []}

    monkeypatch.setattr(memory_client.Client, "call", call)
    c = memory_client.Client("http://127.0.0.1:1", Path("unused"))
    store, mem = memory_client.Store(c), memory_client.RemoteMemory(c)
    store.sync("mail", [])
    store.search("x", semantic=True)
    store.search("x", semantic=None)
    store.search("x", semantic=False)
    mem.relevant("x")
    assert timeouts == {
        "/v1/docs/sync": memory_client.SLOW_TIMEOUT,
        "/v1/search True": memory_client.SLOW_TIMEOUT,
        "/v1/search None": memory_client.SLOW_TIMEOUT,
        "/v1/search False": None,  # the short default
        "/v1/memories/relevant": None,
    }
