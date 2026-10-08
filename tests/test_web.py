"""The web page server and the MCP client, driven against a stub assistant that
implements the web contract (no audio, no Claude). Each test runs its own server on
a free 127.0.0.1 port and shuts it down."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
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

import claude_voice.mcp_server as mcp_mod
import claude_voice.web as web_mod
from claude_voice.config import Config, WebConfig

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
async def serve(app: StubApp | None = None) -> AsyncIterator[Server]:
    stub = app or StubApp()
    runner = await web_mod.start(WebConfig(hosts=["127.0.0.1"], port=0), stub)
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
        caplog.set_level(logging.INFO, logger="claude_voice.web")
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
    monkeypatch.setenv("CLAUDE_VOICE_AGENT", "a-really-long-project-directory-name-here-and-more")
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
    monkeypatch.delenv("CLAUDE_VOICE_ROOM", raising=False)


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
