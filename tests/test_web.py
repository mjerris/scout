"""The web page server and the MCP client, driven against a stub assistant that
implements the web contract (no audio, no Claude). Each test runs its own server on
a free 127.0.0.1 port and shuts it down."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import make_mocked_request

import scout.mcp_server as mcp_mod
import scout.web as web_mod
from scout import phone_devices
from scout.config import Config, PhoneConfig, WebConfig

TOKEN = "t" * 32
CLIENT = "page0001abcd"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# --- a stub assistant that keeps the web contract ------------------------------------


class StubSpeaker:
    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None  # when set, synthesis waits for it
        self.fail: set[str] = set()

    async def synth_wav(self, text: str, voice: str | None = None) -> bytes:
        if self.gate is not None:
            await self.gate.wait()
        if text in self.fail:
            raise RuntimeError("synthesis failed")
        return text.encode()


class StubRules:
    def __init__(self) -> None:
        self.items = [{"id": "r-1", "text": "git status", "added": None}]

    def listing(self) -> list[dict[str, Any]]:
        return list(self.items)


class StubApp:
    def __init__(self) -> None:
        self.speaker = StubSpeaker()
        self.rules = StubRules()
        self.history: deque[dict[str, Any]] = deque(maxlen=300)
        self.listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self.calls: list[tuple[Any, ...]] = []
        self.floor: dict[str, Any] = {"holder": None, "speaking": False, "held_for_s": 0, "queue": []}
        self.confirm_id: int | None = None
        self.text_gate: asyncio.Event | None = None  # when set, submit_text waits for it

    def emit(self, kind: str, **data: Any) -> None:
        ev = {"type": kind, "ts": time.time(), **data}
        if kind not in ("state", "say"):
            self.history.append(ev)
        for q in list(self.listeners):
            q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.listeners.discard(q)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": "idle",
            "mic_muted": False,
            "confirming": self.confirm_id is not None,
            "confirm_id": self.confirm_id,
            "floor": dict(self.floor),
        }

    def voice_status(self) -> dict[str, Any]:
        return {"state": "idle", "floor": dict(self.floor)}

    async def discuss(
        self,
        agent: str,
        message: str | None,
        listen: bool = True,
        timeout: float = 30.0,
        hold: bool = False,
        voice: str | None = None,
        wait_for_floor: float = 0.0,
    ) -> dict[str, Any]:
        self.calls.append(("discuss", agent, message, listen, timeout, hold, voice, wait_for_floor))
        return {"status": "ok", "text": "sure"}

    async def run_shared_tool(
        self, agent: str, name: str, args: dict[str, Any], preview: bool = False, confirmed: bool = False
    ) -> dict[str, Any]:
        self.calls.append(
            ("tool", agent, name, args)
            + ((("preview",) if preview else ()) + (("confirmed",) if confirmed else ()))
        )
        return {"status": "ok", "text": "No events in that range."}

    async def ask(self, agent: str, text: str, deadline: float = 20.0) -> dict[str, Any]:
        self.calls.append(("ask", agent, text))
        return {"status": "ok", "text": "It's sunny.", "tier": "1"}

    async def submit_text(self, text: str, speak: bool = True, client: str | None = None) -> str | None:
        self.calls.append(("say", text, speak, client))
        if self.text_gate is not None:
            await self.text_gate.wait()
        if client:
            self.emit("say", text=f"reply to {text}", to=client)
        return None

    async def submit_audio(self, pcm: bytes, speak: bool = True, client: str | None = None) -> dict[str, Any]:
        self.calls.append(("audio", len(pcm), speak, client))
        return {"text": "heard you"}

    def answer_confirm(self, approved: bool | str, confirm_id: int | None = None) -> None:
        self.calls.append(("confirm", approved, confirm_id))

    def remove_rule(self, rule_id: str) -> None:
        self.calls.append(("remove_rule", rule_id))

    async def stop(self) -> None:
        self.calls.append(("stop",))
        self.emit("stopped")

    async def reset(self, speak: bool = True, client: str | None = None) -> None:
        self.calls.append(("reset", speak, client))
        self.emit("reset")

    def set_mic_muted(self, muted: bool) -> None:
        self.calls.append(("mute", muted))


@dataclass
class Server:
    url: str
    runner: aioweb.AppRunner
    app: StubApp

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def ws_url(self, client: str = CLIENT) -> str:
        return self.url.replace("http://", "ws://") + f"/ws?client={client}"


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "web_token"
    path.parent.mkdir()
    path.write_text(TOKEN + "\n")
    monkeypatch.setattr(web_mod, "TOKEN_PATH", path)
    monkeypatch.setattr(web_mod, "REFRESH_SECONDS", 0.05)
    return path


@asynccontextmanager
async def serve(app: StubApp | None = None, phone: PhoneConfig | None = None) -> AsyncIterator[Server]:
    stub = app or StubApp()
    runner = await web_mod.start(WebConfig(hosts=["127.0.0.1"], port=0), stub, phone)
    try:
        host, port = runner.addresses[0][:2]
        yield Server(f"http://{host}:{port}", runner, stub)
    finally:
        await runner.cleanup()


async def recv(
    ws: aiohttp.ClientWebSocketResponse, pred: Callable[[dict[str, Any]], bool], timeout: float = 3.0
) -> dict[str, Any]:
    """The first event matching pred (others are skipped)."""
    deadline = time.monotonic() + timeout
    while True:
        ev: dict[str, Any] = await asyncio.wait_for(ws.receive_json(), max(0.01, deadline - time.monotonic()))
        if pred(ev):
            return ev


def of(kind: str) -> Callable[[dict[str, Any]], bool]:
    return lambda ev: ev.get("type") == kind


async def nothing_of(ws: aiohttp.ClientWebSocketResponse, kind: str, wait: float = 0.3) -> bool:
    try:
        await recv(ws, of(kind), wait)
    except TimeoutError:
        return True
    return False


# --- token ---------------------------------------------------------------------------


def test_empty_or_short_token_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "web_token"
    path.write_text("")
    token = web_mod.load_token(path)
    assert len(token) >= 16
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("short\n")
    assert web_mod.load_token(path) not in ("", "short")
    assert web_mod.load_token(path) == path.read_text().strip()  # a good token is kept


def test_missing_token_file_is_created(tmp_path: Path) -> None:
    path = tmp_path / "state" / "web_token"
    token = web_mod.load_token(path)
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_empty_token_never_matches() -> None:
    assert not web_mod.token_ok("", "")
    assert not web_mod.token_ok("", TOKEN)
    assert not web_mod.token_ok("abc", "abc")  # too short to be a real token
    assert web_mod.token_ok(TOKEN, TOKEN)
    assert not web_mod.token_ok("é" * 32, TOKEN)


def test_api_auth_and_token_only_in_page_url(token_file: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        caplog.set_level(logging.INFO, logger="scout.web")
        async with serve() as srv, aiohttp.ClientSession() as s:
            async with s.get(srv.url + "/api/status") as r:
                assert r.status == 401
            async with s.get(srv.url + f"/api/status?token={TOKEN}") as r:
                assert r.status == 401  # the query token is for opening the page only
            async with s.get(srv.url + "/api/status", headers=srv.auth) as r:
                assert r.status == 200
            async with s.get(srv.url + "/") as r:
                assert r.status == 401

    run(main())
    assert TOKEN not in caplog.text
    assert "web UI:" in caplog.text


def test_page_cookie_and_headers(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            async with s.get(srv.url + f"/?token={TOKEN}") as r:
                assert r.status == 200
                cookie = r.cookies["cv_token"]
                assert cookie.value == TOKEN
                assert cookie["httponly"]
                assert cookie["samesite"] == "Strict"
                assert not cookie["secure"]  # plain http: a Secure cookie would never come back
                assert r.headers["Referrer-Policy"] == "no-referrer"
                assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
            # Tailscale Serve terminates https and proxies from 127.0.0.1.
            async with s.get(srv.url + f"/?token={TOKEN}", headers={"X-Forwarded-Proto": "https"}) as r:
                assert r.cookies["cv_token"]["secure"]

    run(main())


# --- origin checks -------------------------------------------------------------------


def _req(headers: dict[str, str], remote: str = "127.0.0.1") -> aioweb.Request:
    return make_mocked_request("GET", "/ws", headers=headers).clone(remote=remote)


def test_origin_must_be_the_page_itself() -> None:
    host = {"Host": "192.168.1.5:8765"}
    assert web_mod.origin_ok(_req(host))  # no Origin: the MCP server, curl
    assert web_mod.origin_ok(_req({**host, "Origin": "http://192.168.1.5:8765"}))
    assert not web_mod.origin_ok(_req({**host, "Origin": "http://192.168.1.5:3000"}))
    assert not web_mod.origin_ok(_req({"Host": "localhost:8765", "Origin": "http://localhost:3000"}))
    assert not web_mod.origin_ok(_req({**host, "Origin": "null"}))
    ts = {"Host": "mini.tail1234.ts.net", "Origin": "https://mini.tail1234.ts.net"}
    assert web_mod.origin_ok(_req(ts))
    fwd = {"Host": "127.0.0.1:8765", "X-Forwarded-Host": "mini.ts.net", "Origin": "https://mini.ts.net"}
    assert web_mod.origin_ok(_req(fwd))
    assert not web_mod.origin_ok(_req(fwd, remote="192.168.1.9"))  # only the local proxy may forward


def test_cross_origin_ws_and_api_are_refused(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await s.ws_connect(srv.ws_url(), headers=srv.auth, origin="http://localhost:3000")
            assert exc.value.status == 403
            async with s.post(
                srv.url + "/api/discuss", headers={**srv.auth, "Origin": "http://evil.example"}, json={}
            ) as r:
                assert r.status == 403
            host = srv.url.removeprefix("http://")
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth, origin=f"http://{host}")
            assert (await ws.receive_json())["type"] == "hello"
            await ws.close()

    run(main())


# --- /api/discuss ----------------------------------------------------------------------


def test_discuss_validates_body(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession(headers=srv.auth) as s:
            url = srv.url + "/api/discuss"
            async with s.post(url, data='{"message": "hi"}', headers={"Content-Type": "text/plain"}) as r:
                assert r.status == 415  # a plain form post can't make it speak
            bad: list[object] = [[], 5, "x", {"message": 5}, {"listen_timeout": "nan"}, {"hold_floor": "yes"}]
            for body in bad:
                async with s.post(url, json=body) as r:
                    assert r.status == 400, body
                    assert (await r.json())["status"] == "error"
            async with s.post(url, data="{not json", headers={"Content-Type": "application/json"}) as r:
                assert r.status == 400
            assert srv.app.calls == []
            good = {"agent": "proj#123", "message": "hi", "listen_timeout": 9999, "wait_for_floor": -5}
            async with s.post(url, json=good) as r:
                assert r.status == 200
                assert await r.json() == {"status": "ok", "text": "sure"}
            assert srv.app.calls == [("discuss", "proj#123", "hi", True, 300.0, False, None, 0.0)]

    run(main())


# --- /api/tool ----------------------------------------------------------------------------


def test_tool_endpoint_needs_auth_and_validates_body(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            url = srv.url + "/api/tool"
            async with s.post(url, json={"name": "calendar_events"}) as r:
                assert r.status == 401  # no token, no calendar
            async with s.post(
                url, data='{"name": "mail_send"}', headers={**srv.auth, "Content-Type": "text/plain"}
            ) as r:
                assert r.status == 415
            bad: list[object] = [[], {"name": 5}, {"name": "x", "args": []}, {"name": "x", "agent": 3}]
            for b in bad:
                async with s.post(url, json=b, headers=srv.auth) as r:
                    assert r.status == 400, b
            assert srv.app.calls == []
            body = {"agent": "proj#9", "name": "calendar_events", "args": {"start": "2026-10-08"}}
            async with s.post(url, json=body, headers=srv.auth) as r:
                assert await r.json() == {"status": "ok", "text": "No events in that range."}
            assert srv.app.calls == [("tool", "proj#9", "calendar_events", {"start": "2026-10-08"})]

    run(main())


def test_ask_endpoint_needs_auth_and_validates_body(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            url = srv.url + "/api/ask"
            async with s.post(url, json={"agent": "phone", "text": "weather?"}) as r:
                assert r.status == 401
            bad: list[object] = [
                [],
                {"text": ""},
                {"text": 5},
                {"text": "x", "agent": 3},
                {"text": "x" * 2001},
            ]
            for b in bad:
                async with s.post(url, json=b, headers=srv.auth) as r:
                    assert r.status == 400, b
            assert srv.app.calls == []
            async with s.post(url, json={"agent": "phone", "text": "weather?"}, headers=srv.auth) as r:
                assert await r.json() == {"status": "ok", "text": "It's sunny.", "tier": "1"}
            assert srv.app.calls == [("ask", "phone", "weather?")]

    run(main())


# --- the websocket -----------------------------------------------------------------------


def test_malformed_messages_dont_kill_the_socket(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            for raw in ("[]", "5", "null", '"x"', "{bad", "[" * 100_000):
                await ws.send_str(raw)
            for m in (
                {"type": "say", "text": 5},
                {"type": "remove_rule", "index": "x"},
                {"type": "remove_rule", "id": 5},
                {"type": "audio", "data": 7},
                {"type": "mute", "value": "no"},
            ):
                await ws.send_json(m)
            await ws.send_json({"type": "say", "text": "still here", "output": "mini"})
            await ws.send_json({"type": "reset", "output": "device"})
            await asyncio.sleep(0.2)
            assert not ws.closed
            assert ("say", "still here", True, None) in srv.app.calls
            assert ("mute", False) in srv.app.calls
            assert ("reset", False, CLIENT) in srv.app.calls  # the confirmation is spoken on this page
            assert not [c for c in srv.app.calls if c[0] == "remove_rule"]
            await ws.close()

    run(main())


def test_approvals_carry_the_question_id(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        stub.confirm_id = 7
        async with serve(stub) as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            hello = await recv(ws, of("hello"))
            assert hello["confirm_id"] == 7
            await ws.send_json({"type": "confirm", "approved": True})  # no id: refused
            err = await recv(ws, of("error"))
            assert "question id" in err["text"]
            await ws.send_json({"type": "confirm", "id": True, "approved": True})  # a bool isn't an id
            await ws.send_json({"type": "confirm", "id": 7, "approved": True})
            await ws.send_json({"type": "confirm", "id": 7, "approved": True})  # double tap: same id
            await ws.send_json({"type": "confirm", "id": 8, "approved": True, "always": True})
            await ws.send_json({"type": "confirm", "id": 9, "approved": "yes"})  # not true: a deny
            await ws.send_json({"type": "remove_rule", "id": "r-1"})
            await asyncio.sleep(0.2)
            assert [c for c in srv.app.calls if c[0] in ("confirm", "remove_rule")] == [
                ("confirm", True, 7),
                ("confirm", True, 7),
                ("confirm", "always", 8),
                ("confirm", False, 9),
                ("remove_rule", "r-1"),
            ]
            await ws.close()

    run(main())


def test_reply_audio_follows_the_page_across_reconnects(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            await ws.send_json({"type": "say", "text": "hello", "output": "device"})
            audio = await recv(ws, of("audio_reply"))
            assert audio["data"] == "cmVwbHkgdG8gaGVsbG8="  # b"reply to hello"
            assert ("say", "hello", False, CLIENT) in srv.app.calls
            await ws.close()
            await asyncio.sleep(0.1)
            # The phone locked: the rest of the turn is emitted while no socket is open.
            srv.app.emit("say", text="second sentence", to=CLIENT)
            srv.app.emit("say", text="for someone else", to="otherpage99")
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            audio = await recv(ws, of("audio_reply"))
            assert audio["data"] == "c2Vjb25kIHNlbnRlbmNl"  # b"second sentence"
            assert await nothing_of(ws, "audio_reply")
            await ws.close()

    run(main())


def test_a_request_finishes_after_the_page_disconnects(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        stub.text_gate = asyncio.Event()
        async with serve(stub) as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            await ws.send_json({"type": "say", "text": "lights", "output": "device"})
            await asyncio.sleep(0.1)
            await ws.close()  # the phone locked mid-request
            await asyncio.sleep(0.1)
            stub.text_gate.set()
            await asyncio.sleep(0.1)
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            assert (await recv(ws, of("audio_reply")))["data"] == "cmVwbHkgdG8gbGlnaHRz"
            await ws.close()

    run(main())


def test_newest_socket_of_a_page_gets_the_audio(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            old = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(old, of("hello"))
            new = await s.ws_connect(srv.ws_url(), headers=srv.auth)  # reconnected before the old one died
            await recv(new, of("hello"))
            srv.app.emit("say", text="one", to=CLIENT)
            assert (await recv(new, of("audio_reply")))["data"] == "b25l"
            assert await nothing_of(old, "audio_reply")
            await old.close()
            # Audio still being made for a socket when the page reconnects moves to the new one.
            srv.app.speaker.gate = asyncio.Event()
            srv.app.emit("say", text="two", to=CLIENT)
            await asyncio.sleep(0.1)
            newer = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(newer, of("hello"))
            srv.app.speaker.gate.set()
            assert (await recv(newer, of("audio_reply")))["data"] == "dHdv"
            assert await nothing_of(new, "audio_reply")
            await new.close()
            await newer.close()

    run(main())


def test_stop_drops_queued_reply_audio(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        stub.speaker.gate = asyncio.Event()
        async with serve(stub) as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            stub.emit("say", text="first", to=CLIENT)
            stub.emit("say", text="second", to=CLIENT)
            await asyncio.sleep(0.1)  # "first" is being synthesized
            await ws.send_json({"type": "stop"})
            await recv(ws, of("stopped"))
            stub.speaker.gate.set()
            assert await nothing_of(ws, "audio_reply")
            # Kept audio for a disconnected page is dropped by a stop too.
            await ws.close()
            await asyncio.sleep(0.1)
            stub.emit("say", text="late", to=CLIENT)
            stub.emit("stopped")
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            assert await nothing_of(ws, "audio_reply")
            await ws.close()

    run(main())


def test_audio_synthesis_does_not_hold_up_events(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        stub.speaker.gate = asyncio.Event()
        stub.speaker.fail = {"broken"}
        async with serve(stub) as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            stub.emit("say", text="slow", to=CLIENT)
            stub.emit("claude", text="text arrives first")
            ev = await recv(ws, lambda e: e["type"] in ("claude", "audio_reply"))
            assert ev["type"] == "claude"
            stub.emit("say", text="broken", to=CLIENT)
            stub.emit("say", text="after", to=CLIENT)
            stub.speaker.gate.set()
            got = [(await recv(ws, of("audio_reply")))["data"] for _ in range(2)]
            assert got == ["c2xvdw==", "YWZ0ZXI="]  # in order; a failed sentence doesn't stop the rest
            await ws.close()

    run(main())


def test_clips_are_handled_off_the_receive_loop_and_capped(
    token_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decode_gate = asyncio.Event()
    pcm = b"\0\0" * 16000

    async def fake_decode(data: bytes) -> bytes:
        await decode_gate.wait()
        return pcm

    monkeypatch.setattr(web_mod, "decode_to_pcm", fake_decode)

    async def main() -> None:
        nonlocal pcm
        async with serve() as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            await recv(ws, of("hello"))
            await ws.send_json({"type": "audio", "data": "AAAA", "output": "mini"})
            await ws.send_json({"type": "stop"})  # must not wait behind the clip
            await recv(ws, of("stopped"))
            assert ("stop",) in srv.app.calls
            decode_gate.set()
            assert (await recv(ws, of("ptt_result")))["text"] == "heard you"
            pcm = b"\0\0" * int(16000 * (web_mod.MAX_CLIP_SECONDS + 1))
            await ws.send_json({"type": "audio", "data": "AAAA"})
            err = await recv(ws, of("error"))
            assert "longer than" in err["text"]
            await ws.send_json({"type": "audio", "data": "not base64!"})
            assert "Couldn't use that recording" in (await recv(ws, of("error")))["text"]
            assert [c[0] for c in srv.app.calls].count("audio") == 1
            await ws.close()

    run(main())


def test_one_subscription_however_many_pages(token_file: Path) -> None:
    async def main() -> None:
        async with serve() as srv, aiohttp.ClientSession() as s:
            for i in range(5):
                ws = await s.ws_connect(srv.ws_url(f"page{i:08d}"), headers=srv.auth)
                await ws.close()  # some close before reading the hello
            await asyncio.sleep(0.1)
            assert len(srv.app.listeners) == 1
        assert srv.app.listeners == set()

    run(main())


def test_floor_lapse_reaches_the_page(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        stub.floor = {"holder": "proj#1", "speaking": False, "held_for_s": 9.5, "queue": []}
        async with serve(stub) as srv, aiohttp.ClientSession() as s:
            ws = await s.ws_connect(srv.ws_url(), headers=srv.auth)
            assert (await recv(ws, of("hello")))["floor"]["holder"] == "proj#1"
            stub.floor = {"holder": None, "speaking": False, "held_for_s": 0, "queue": []}  # hold lapsed
            ev = await recv(ws, of("state"))
            assert ev["floor"]["holder"] is None
            await ws.close()

    run(main())


def test_slow_page_is_disconnected_not_buffered(token_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_mod, "MAX_OUTBOX", 3)

    async def main() -> None:
        class StuckWS:
            closed = False

            def __init__(self) -> None:
                self.close_codes: list[int] = []

            async def close(self, code: int = 1000, message: bytes = b"") -> bool:
                self.close_codes.append(code)
                return True

        stuck = StuckWS()
        conn = web_mod._Conn(cast(aioweb.WebSocketResponse, stuck), CLIENT)
        for i in range(5):
            conn.push({"type": "claude", "text": str(i)})
        await asyncio.sleep(0)
        assert conn.closed
        assert conn.outbox.qsize() == 3
        assert stuck.close_codes == [aiohttp.WSCloseCode.TRY_AGAIN_LATER]

    run(main())


def test_shutdown_closes_open_pages_quickly(token_file: Path) -> None:
    async def main() -> None:
        stub = StubApp()
        runner = await web_mod.start(WebConfig(hosts=["127.0.0.1"], port=0), stub)
        host, port = runner.addresses[0][:2]
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://{host}:{port}/ws", headers={"Authorization": f"Bearer {TOKEN}"})
            await recv(ws, of("hello"))
            began = time.monotonic()
            await runner.cleanup()
            assert time.monotonic() - began < 5
            await ws.receive()
            assert ws.closed

    run(main())


def test_tailscale_found_under_homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/tailscale")
    assert web_mod._tailscale_exes() == ["/opt/homebrew/bin/tailscale"]


# --- the MCP client ------------------------------------------------------------------


def test_mcp_result_text() -> None:
    d = mcp_mod.describe
    assert "mic is muted" in d({"status": "muted"}, True)
    assert d({"status": "floor_busy", "holder": None, "queue": ["a#1", "b#2"]}, True) == (
        "(not spoken: others are queued for the floor ahead of you: a#1, b#2)"
    )
    assert "None" not in d({"status": "floor_busy", "holder": None, "queue": []}, True)
    assert (
        d({"status": "floor_busy", "holder": "x#3", "queue": []}, True) == "(not spoken: x#3 has the floor)"
    )
    assert d({"status": "ok", "text": "yes"}, True) == 'Heard: "yes"'
    assert d({"status": "ok", "spoke": True}, False) == "(spoken)"


def test_mcp_agent_names_stay_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_AGENT", "a-really-long-project-directory-name-here-and-more")
    name = mcp_mod._agent()
    assert name.endswith(f"#{os.getpid()}")
    assert len(name) <= 30 + 1 + len(str(os.getpid()))


def test_mcp_timeout_covers_long_messages() -> None:
    short = mcp_mod.call_timeout("hi", 30, 15, True)
    long = mcp_mod.call_timeout("word " * 2000, 30, 15, True)  # minutes of speech
    assert long - short >= 10000 / mcp_mod.SPEECH_CHARS_PER_SECOND - 1
    assert mcp_mod.call_timeout("hi", 30, 15, False) < short


def _mcp_config(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    cfg = Config()
    cfg.web.port = port
    monkeypatch.setattr(mcp_mod, "load", lambda: cfg)
    monkeypatch.delenv("SCOUT_ROOM", raising=False)


def test_mcp_token_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mcp_config(monkeypatch, 1)
    path = tmp_path / "web_token"
    monkeypatch.setattr(mcp_mod, "TOKEN_PATH", path)
    r = run(mcp_mod._call("GET", "/api/status"))
    assert "hasn't started here yet, or the token file was deleted" in r["error"]
    path.write_text("\n")
    r = run(mcp_mod._call("GET", "/api/status"))
    assert "is empty" in r["error"]


def test_mcp_reports_a_changed_token_and_server_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "web_token"
    path.write_text("x" * 32)
    monkeypatch.setattr(mcp_mod, "TOKEN_PATH", path)

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        async def status(req: aioweb.Request) -> aioweb.StreamResponse:
            raise aioweb.HTTPUnauthorized()

        async def discuss(req: aioweb.Request) -> aioweb.StreamResponse:
            return aioweb.json_response({"status": "error", "error": "agent must be a string"}, status=400)

        app = aioweb.Application()
        app.add_routes([aioweb.get("/api/status", status), aioweb.post("/api/discuss", discuss)])
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            _mcp_config(monkeypatch, runner.addresses[0][1])
            return await mcp_mod._call("GET", "/api/status"), await mcp_mod._call("POST", "/api/discuss", {})
        finally:
            await runner.cleanup()

    unauthorized, bad = run(main())
    assert "the file changed after the app started; restart the app" in unauthorized["error"]
    assert bad["error"] == "HTTP 400: agent must be a string"


def test_mcp_talks_to_the_real_web_server(token_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_mod, "TOKEN_PATH", token_file)

    async def main() -> str:
        async with serve() as srv:
            _mcp_config(monkeypatch, int(srv.url.rsplit(":", 1)[1]))
            return await mcp_mod.discuss("Should I push?", listen_timeout=5)

    assert run(main()) == 'Heard: "sure"'


def test_page_script_sends_ids() -> None:
    """The page answers by question id and removes rules by rule id."""
    page = (Path(web_mod.__file__).parent / "web.html").read_text()
    assert 'type: "confirm", id: confirmId' in page
    assert 'type: "remove_rule", id: r.id' in page
    assert '"/ws?client="' in page
    assert "pointercancel" in page


def test_an_address_that_isnt_up_yet_doesnt_stop_the_server(token_file: Path) -> None:
    async def main() -> None:
        runner = await web_mod.start(WebConfig(hosts=["127.0.0.1", "192.0.2.1"], port=0), StubApp())
        try:
            assert len(runner.addresses) == 1
            assert len(runner.sites) == 1
        finally:
            await runner.cleanup()

    run(main())


# --- /phone: the browser test page, pairing and SignalWire tokens -------------------------

ADDRESS = "/public/scout"
NODE = Path("/opt/homebrew/opt/node@24/bin/node")


def _phone(**kw: Any) -> PhoneConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "space": "example.signalwire.com",
        "project_id": "proj-1",
        "api_token": "PTsecret",
        "address": ADDRESS,
    }
    return PhoneConfig(**{**base, **kw})


@pytest.fixture
def devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "phone_devices.json"
    monkeypatch.setattr(phone_devices, "PATH", path)
    return path


def test_phone_page_and_scripts_are_served_behind_the_token(token_file: Path) -> None:
    async def main() -> None:
        jar = aiohttp.CookieJar(unsafe=True)  # keep cookies for an IP host
        async with serve(phone=_phone()) as srv, aiohttp.ClientSession(cookie_jar=jar) as s:
            async with s.get(srv.url + "/phone") as r:
                assert r.status == 401
            async with s.get(srv.url + "/phone/phone.js") as r:
                assert r.status == 401
            async with s.get(srv.url + f"/phone?token={TOKEN}") as r:
                assert r.status == 200
                html = await r.text()
                assert r.headers["Cache-Control"] == "no-store"
                csp = r.headers["Content-Security-Policy"]
                assert "frame-ancestors 'none'" in csp and "script-src 'self' https://cdn.jsdelivr.net" in csp
                assert r.cookies["cv_token"].value == TOKEN
            assert "https://cdn.jsdelivr.net/npm/@signalwire/js@4.0.0-rc.4/dist/browser.umd.js" in html
            assert 'integrity="sha384-' in html
            for name in ("phone.js", "phone_sign.js"):  # the cookie the page set opens its scripts
                async with s.get(srv.url + f"/phone/{name}") as r:
                    assert r.status == 200 and r.content_type == "text/javascript"
                    assert "use strict" in await r.text()
            async with s.get(srv.url + "/phone/web.py") as r:
                assert r.status == 404

    run(main())


def test_phone_script_dials_with_signed_variables() -> None:
    js = (Path(web_mod.__file__).parent / "phone.js").read_text()
    assert "new SW.SignalWire(new SW.StaticCredentialProvider({ token })" in js
    assert "client.dial(p.address" in js and "userVariables," in js
    assert "ScoutSign.callVariables(p)" in js
    assert '"/api/phone/pair"' in js and '"/api/phone/token"' in js
    assert "remoteStream$" in js and ".hangup()" in js


def test_pair_endpoint(token_file: Path, devices: Path) -> None:
    async def main() -> None:
        async with serve(phone=_phone()) as srv, aiohttp.ClientSession() as s:
            url = srv.url + "/api/phone/pair"
            async with s.post(url, json={"name": "Browser"}) as r:
                assert r.status == 401
            async with s.post(
                url, json={"name": "Browser"}, headers={**srv.auth, "Origin": "https://evil.example"}
            ) as r:
                assert r.status == 403
            async with s.post(
                url, data='{"name": "x"}', headers={**srv.auth, "Content-Type": "text/plain"}
            ) as r:
                assert r.status == 415
            bad_bodies: list[object] = [[], {}, {"name": ""}, {"name": 3}, {"name": "x" * 65}]
            for bad in bad_bodies:
                async with s.post(url, json=bad, headers=srv.auth) as r:
                    assert r.status == 400, bad
            assert not devices.exists()
            async with s.post(url, json={"name": "Mike laptop"}, headers=srv.auth) as r:
                assert r.status == 200
                assert r.headers["Cache-Control"] == "no-store"
                got = await r.json()
        assert set(got) == {"device", "secret", "address"} and got["address"] == ADDRESS
        stored = phone_devices.load_devices(devices)[got["device"]]
        assert stored["name"] == "Mike laptop" and stored["secret"] == got["secret"] and not stored["revoked"]
        # A call signed with what the page got passes the service's check.
        check = phone_devices.CallCheck(ADDRESS, 60, devices)
        now = int(time.time())
        sig = phone_devices.sign(got["secret"], got["device"], now, "nonce-0001", ADDRESS)
        assert check.check({"device": got["device"], "ts": now, "nonce": "nonce-0001", "sig": sig}).ok

    run(main())


def test_pair_needs_an_address(token_file: Path, devices: Path) -> None:
    async def main() -> None:
        async with (
            serve(phone=_phone(address="")) as srv,
            aiohttp.ClientSession() as s,
            s.post(srv.url + "/api/phone/pair", json={"name": "B"}, headers=srv.auth) as r,
        ):
            assert r.status == 409
            assert "[phone] address" in (await r.json())["error"]
        assert not devices.exists()

    run(main())


def test_token_request_shape() -> None:
    url, headers, body = web_mod.phone_token_request(_phone(space="https://example.signalwire.com/"), 1000.0)
    assert url == "https://example.signalwire.com/api/fabric/subscribers/tokens"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"proj-1:PTsecret").decode()
    assert body == {"reference": "scout-owner", "expire_at": 1000 + web_mod.SW_TOKEN_TTL_S}
    _, _, body = web_mod.phone_token_request(_phone(subscriber="me@example.com"), 1000.0)
    assert body["reference"] == "me@example.com"


@pytest.mark.parametrize(
    ("cfg", "want"),
    [
        (_phone(enabled=False), "enabled = true"),
        (_phone(space=""), "space"),
        (_phone(project_id="", api_token=""), "project_id, api_token"),
        (_phone(subscriber=" "), "subscriber"),
        (_phone(space="not a host"), "not a host name"),
    ],
)
def test_token_request_needs_config(cfg: PhoneConfig, want: str) -> None:
    with pytest.raises(web_mod.PhoneTokenError, match=re.escape(want)) as exc:
        web_mod.phone_token_request(cfg, 0.0)
    assert exc.value.status == 409


class FakeSignalWire:
    """Stands in for web._post_json: records the request, answers as configured."""

    def __init__(self, status: int = 200, data: Any = None, exc: BaseException | None = None) -> None:
        self.status, self.data, self.exc = status, data, exc
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self.requests.append((url, headers, body))
        if self.exc is not None:
            raise self.exc
        return self.status, self.data


def test_token_endpoint(token_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSignalWire(data={"subscriber_id": "s-1", "token": "SAT.jwt", "refresh_token": "R.jwt"})
    monkeypatch.setattr(web_mod, "_post_json", fake)

    async def main() -> None:
        async with serve(phone=_phone()) as srv, aiohttp.ClientSession() as s:
            url = srv.url + "/api/phone/token"
            async with s.post(url) as r:
                assert r.status == 401
            assert fake.requests == []
            before = int(time.time())
            async with s.post(url, headers=srv.auth) as r:
                assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
                got = await r.json()
        assert got == {"token": "SAT.jwt", "expires_at": got["expires_at"]}  # no refresh token
        [(req_url, headers, body)] = fake.requests
        assert req_url == "https://example.signalwire.com/api/fabric/subscribers/tokens"
        assert headers["Authorization"] == "Basic " + base64.b64encode(b"proj-1:PTsecret").decode()
        assert body["reference"] == "scout-owner"
        assert before + 3600 <= body["expire_at"] == got["expires_at"] <= int(time.time()) + 3600

    run(main())


@pytest.mark.parametrize(
    ("fake", "status", "want"),
    [
        (FakeSignalWire(401, {"errors": [{"code": "unauthorized"}]}), 502, "project_id / api_token"),
        (
            FakeSignalWire(422, {"errors": [{"code": "x", "detail": "Insufficient balance"}]}),
            502,
            "Insufficient balance",
        ),
        (FakeSignalWire(200, {"no": "token"}), 502, "HTTP 200"),
        (FakeSignalWire(500, "<html>oops</html>"), 502, "oops"),
        (FakeSignalWire(exc=TimeoutError()), 504, "didn't answer"),
        (FakeSignalWire(exc=aiohttp.ClientConnectionError("no route")), 502, "no route"),
    ],
)
def test_token_endpoint_errors(
    token_file: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeSignalWire, status: int, want: str
) -> None:
    monkeypatch.setattr(web_mod, "_post_json", fake)

    async def main() -> None:
        async with (
            serve(phone=_phone()) as srv,
            aiohttp.ClientSession() as s,
            s.post(srv.url + "/api/phone/token", headers=srv.auth) as r,
        ):
            assert r.status == status
            err = (await r.json())["error"]
        assert want in err and "PTsecret" not in err

    run(main())


def test_token_endpoint_when_phone_is_off(token_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSignalWire()
    monkeypatch.setattr(web_mod, "_post_json", fake)

    async def main() -> None:
        async with (
            serve() as srv,  # no [phone] passed: off
            aiohttp.ClientSession() as s,
            s.post(srv.url + "/api/phone/token", headers=srv.auth) as r,
        ):
            assert r.status == 409
            assert "enabled = true" in (await r.json())["error"]
        assert fake.requests == []

    run(main())


@pytest.mark.skipif(not NODE.exists(), reason="node not installed")
def test_page_signature_matches_python() -> None:
    """phone_sign.js (what the browser runs) signs exactly like phone_devices.sign."""
    sign_js = Path(web_mod.__file__).parent / "phone_sign.js"
    cases = [
        ("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", "d0123456789abcdef", 1760000000,
         "abcDEF_-12345678", "/public/scout"),
        ("_-_-" * 10 + "_-z", "dfeedfacecafebeef", 1, "n" * 16, "/public/other?channel=audio"),
    ]  # fmt: skip
    script = (
        "const s = require(process.argv[1]); const cases = JSON.parse(process.argv[2]);"
        "(async () => { const out = [];"
        " for (const c of cases) out.push(await s.sign(...c));"
        " const v = await s.callVariables({device: 'dabc', secret: cases[0][0], address: '/public/scout'}, 1760000000999);"
        " out.push(v);"
        " console.log(JSON.stringify(out)); })();"
    )
    proc = subprocess.run(  # noqa: S603  a fixed node binary, our own script
        [str(NODE), "-e", script, str(sign_js), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    *sigs, v = json.loads(proc.stdout)
    assert sigs == [phone_devices.sign(*c) for c in cases]
    assert v["device"] == "dabc" and v["ts"] == 1760000000
    assert len(v["nonce"]) == 16 and phone_devices._NONCE.fullmatch(v["nonce"])
    assert v["sig"] == phone_devices.sign(cases[0][0], "dabc", 1760000000, v["nonce"], "/public/scout")


def test_post_json_sends_json_with_the_headers() -> None:
    """The real client half of the token call, against a local stand-in for SignalWire."""
    seen: list[tuple[str, str | None, Any]] = []

    async def handler(req: aioweb.Request) -> aioweb.StreamResponse:
        seen.append((req.path, req.headers.get("Authorization"), await req.json()))
        return aioweb.json_response({"token": "T"})

    async def main() -> tuple[int, Any]:
        app = aioweb.Application()
        app.router.add_post("/api/fabric/subscribers/tokens", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            host, port = runner.addresses[0][:2]
            url = f"http://{host}:{port}/api/fabric/subscribers/tokens"
            return await web_mod._post_json(url, {"Authorization": "Basic eA=="}, {"reference": "r"})
        finally:
            await runner.cleanup()

    assert run(main()) == (200, {"token": "T"})
    assert seen == [("/api/fabric/subscribers/tokens", "Basic eA==", {"reference": "r"})]
