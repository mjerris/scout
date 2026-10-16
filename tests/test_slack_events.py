"""Slack's live events (slack_events.py, slack.handle_event), the spoken DM alerts
(slack_alerts.py) and sending with a yes (slack_send.py).

Events are stored exactly as a poll stores them (compared item for item against
slack.sync_pass on the same fake Web API), and the Socket Mode client runs against a
local WebSocket server on an ephemeral port playing Slack's side (apps.connections.open,
hello, envelopes, disconnect). Synthetic data only; nothing leaves the machine."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from test_routing import make, run
from test_slack import FakeSlack, msg

from scout import (
    brain,
    config,
    index,
    memory_service,
    phone_service,
    refs,
    shared_tools,
    slack,
    slack_alerts,
    slack_events,
    slack_send,
    syncstate,
    trust,
)
from scout.assistant import PHONE
from scout.mac import ToolError

NOW = time.time()


def ts(minutes_ago: float) -> str:
    return f"{NOW - minutes_ago * 60:.6f}"


class Fake(FakeSlack):
    """FakeSlack plus conversations.info, users.list and chat.postMessage."""

    def __init__(self) -> None:
        super().__init__()
        self.users["U3"] = {"id": "U3", "name": "lpradovera",
                            "profile": {"display_name": "", "real_name": "Luca Pradovera"}}  # fmt: skip
        self.users["U4"] = {
            "id": "U4",
            "name": "lrossi",
            "profile": {"display_name": "", "real_name": "Luca Rossi"},
        }
        self.convs = [
            {"id": "C1", "name": "launch", "is_channel": True},
            {"id": "D1", "is_im": True, "user": "U2"},
            {"id": "D3", "is_im": True, "user": "U3"},
            {"id": "G1", "name": "mpdm-pat--sam--mike-1", "is_mpim": True},
            {"id": "C2", "name": "random", "is_channel": True},
        ]
        self.history = {c["id"]: [] for c in self.convs}
        self.posted: list[dict[str, Any]] = []

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "conversations.info":
            self.calls.append((method, params))
            conv = next((c for c in self.convs if c["id"] == params["channel"]), None)
            if conv is None:
                raise slack.SlackError("conversations.info: channel_not_found")
            return {"ok": True, "channel": conv}
        if method == "users.list":
            self.calls.append((method, params))
            return self._page(list(self.users.values()), params, "members")
        if method == "chat.postMessage":
            self.calls.append((method, params))
            if params["channel"] == "CFAIL":
                raise slack.SlackError("chat.postMessage: not_in_channel")
            self.posted.append(params)
            return {"ok": True, "channel": params["channel"], "ts": "1800000000.000100"}
        return super().__call__(method, params)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.setattr(slack, "PAGE", 2)  # so everything pages
    return Fake()


def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    return s


def live(fake: Fake, path: Path, **kw: Any) -> slack.Live:
    return slack.Live(fake, slack.State(path), **kw)


def ev(channel: str, m: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {**m, "channel": channel, "event_ts": m.get("ts"), **kw}


def item(ix: index.Index, id: str) -> dict[str, Any]:
    h = ix.get(id)
    assert h is not None, id
    meta = {k: v for k, v in h.meta.items() if k != "stored_at"}
    return {
        "id": h.id,
        "kind": h.kind,
        "title": h.title,
        "body": h.body,
        "who": h.who,
        "ts": h.ts,
        "meta": meta,
    }


def polled(fake: Fake, tmp_path: Path) -> index.Index:
    """What a poll of the same fake stores, in a store of its own."""
    other = index.Index(tmp_path / "polled.db", None)
    slack.sync_pass(other, fake, 14, state_path=tmp_path / "polled.json", now=NOW)
    return other


# --- events stored like the poller stores them ------------------------------------------------


def test_a_new_dm_is_stored_as_a_poll_would(fake: Fake, tmp_path: Path) -> None:
    m = msg(ts(5), "U2", "<@U1> lunch <https://x.example/menu|tomorrow>?")
    out = slack.handle_event(
        store(), live(fake, tmp_path / "s.json"), ev("D1", m, channel_type="im"), now=NOW
    )
    assert out == []  # nobody is announced
    fake.history["D1"].append(m)
    other = polled(fake, tmp_path)
    got = item(store(), f"slack:D1:{m['ts']}")
    assert got == item(other, f"slack:D1:{m['ts']}")
    assert got["title"] == "DM with Sam" and got["body"] == "Sam: @Pat Doe lunch tomorrow?"
    assert got["meta"]["dm_user"] == "U2"  # who it's with, by id (people.py), both ways
    assert got["meta"]["permalink"] == f"https://acme.slack.com/archives/D1/p{m['ts'].replace('.', '')}"
    st = json.loads((tmp_path / "s.json").read_text())
    assert st["channels"]["D1"]["ts"] == m["ts"]  # the poll's resume point moved up
    other.close()


def test_a_thread_reply_rebuilds_its_thread_as_a_poll_would(fake: Fake, tmp_path: Path) -> None:
    root = msg(ts(30), "U1", "Launch plan?", reply_count=2, latest_reply=ts(2), thread_ts=ts(30))
    r1 = msg(ts(10), "U2", "Tuesday", thread_ts=root["ts"])
    r2 = msg(ts(2), "U1", "Shipping Monday instead", thread_ts=root["ts"])
    fake.history["C1"] = [root]
    fake.replies[("C1", root["ts"])] = [root, r1, r2]
    lv = live(fake, tmp_path / "s.json")
    slack.handle_event(store(), lv, ev("C1", r2, channel_type="channel"), now=NOW)
    other = polled(fake, tmp_path)
    tid = f"slack:C1:{root['ts']}"
    assert item(store(), tid) == item(other, tid)
    assert item(store(), tid)["body"].splitlines() == [
        "Pat Doe: Launch plan?", "Sam: Tuesday", "Pat Doe: Shipping Monday instead",
    ]  # fmt: skip
    assert store().get(f"slack:C1:{r2['ts']}") is None  # a reply lives in its thread's item
    assert lv.state.channels["C1"]["ts"] == r2["ts"]
    other.close()


def test_edits_and_deletes(fake: Fake, tmp_path: Path) -> None:
    s, lv = store(), live(fake, tmp_path / "s.json")
    m = msg(ts(5), "U2", "lunch at noon?")
    slack.handle_event(s, lv, ev("D1", m), now=NOW)
    edited = {**m, "text": "lunch at one?", "edited": {"user": "U2", "ts": ts(1)}}
    slack.handle_event(s, lv, {"type": "message", "subtype": "message_changed", "channel": "D1",
                               "message": edited, "previous_message": m}, now=NOW)  # fmt: skip
    assert item(s, f"slack:D1:{m['ts']}")["body"] == "Sam: lunch at one?"
    calls = len(fake.calls)
    hidden = {"type": "message", "subtype": "message_changed", "hidden": True, "channel": "D1",
              "message": {**edited, "reply_count": 1, "thread_ts": m["ts"]}}  # fmt: skip
    slack.handle_event(s, lv, hidden, now=NOW)
    assert len(fake.calls) == calls  # nothing read again
    slack.handle_event(s, lv, {"type": "message", "subtype": "message_deleted", "channel": "D1",
                               "deleted_ts": m["ts"], "previous_message": edited}, now=NOW)  # fmt: skip
    assert s.get(f"slack:D1:{m['ts']}") is None
    # A deleted reply: its thread is read again, without it.
    root = msg(ts(30), "U1", "Plan?", reply_count=1, latest_reply=ts(3), thread_ts=ts(30))
    reply = msg(ts(3), "U2", "oops wrong thread", thread_ts=root["ts"])
    fake.replies[("C1", root["ts"])] = [root, reply]
    slack.handle_event(s, lv, ev("C1", reply), now=NOW)
    assert "oops" in item(s, f"slack:C1:{root['ts']}")["body"]
    fake.replies[("C1", root["ts"])] = [{**root, "reply_count": 0}]
    slack.handle_event(s, lv, {"type": "message", "subtype": "message_deleted", "channel": "C1",
                               "deleted_ts": reply["ts"], "previous_message": reply}, now=NOW)  # fmt: skip
    assert item(s, f"slack:C1:{root['ts']}")["body"] == "Pat Doe: Plan?"


def test_skipped_muted_and_noise_are_left_out(fake: Fake, tmp_path: Path) -> None:
    s = store()
    fake.muted = "G1"
    lv = live(fake, tmp_path / "s.json", skip=["#random", "Luca Pradovera"])
    for cid, m in [
        ("C2", msg(ts(5), "U1", "cats")),  # skipped channel
        ("G1", msg(ts(5), "U1", "muted group")),
        ("D3", msg(ts(5), "U3", "a DM with someone skipped")),
        ("C1", msg(ts(5), "U1", "", subtype="channel_join")),
        ("C1", msg(ts(4), "U1", "", subtype="message_replied")),
    ]:
        assert slack.handle_event(s, lv, ev(cid, m), now=NOW) == []
    assert s.recent(["slack"], limit=10) == []
    assert slack.handle_event(s, lv, {"type": "reaction_added", "channel": "C1"}, now=NOW) == []


def test_an_unknown_conversation_is_looked_up_once(fake: Fake, tmp_path: Path) -> None:
    lv = live(fake, tmp_path / "s.json")
    for minutes in (5, 4):
        slack.handle_event(store(), lv, ev("C1", msg(ts(minutes), "U1", "hi")), now=NOW)
    assert fake.count("conversations.info") == 1 and fake.count("auth.test") == 1
    assert item(store(), f"slack:C1:{ts(5)}")["title"] == "#launch"
    # A pass lists conversations: the event side then needs no lookup at all.
    st = slack.State(tmp_path / "p.json")
    slack.sync_pass(store(), fake, 14, state=st, now=NOW)
    fake.calls.clear()
    slack.handle_event(store(), slack.Live(fake, st), ev("D1", msg(ts(1), "U2", "yo")), now=NOW)
    assert fake.count("conversations.info") == 0


def test_with_live_events_a_pass_reads_back_less(fake: Fake, tmp_path: Path) -> None:
    st = slack.State(tmp_path / "s.json")
    fake.history["C1"] = [msg(ts(60), "U1", "first")]
    slack.sync_pass(store(), fake, 14, state=st, now=NOW)  # checked at NOW
    m = msg(ts(1), "U2", "since then")
    slack.handle_event(store(), slack.Live(fake, st), ev("C1", m), now=NOW)
    fake.history["C1"].append(m)

    def oldest() -> float:
        return float(next(p["oldest"] for k, p in reversed(fake.calls) if k == "conversations.history"
                          and p["channel"] == "C1"))  # fmt: skip

    slack.sync_pass(store(), fake, 14, state=st, now=NOW + 60, live_since=NOW - 3600)
    assert oldest() == pytest.approx(float(m["ts"]) - slack.LIVE_LOOKBACK_S)  # from the event's resume point
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + 120, live_since=NOW + 90)  # broke since
    assert oldest() == pytest.approx(float(m["ts"]) - slack.LOOKBACK_S)
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + 180, live_since=None)
    assert oldest() == pytest.approx(float(m["ts"]) - slack.LOOKBACK_S)


def test_with_live_events_a_pass_reads_only_what_had_activity(fake: Fake, tmp_path: Path) -> None:
    st = slack.State(tmp_path / "s.json")
    slack.sync_pass(store(), fake, 14, state=st, now=NOW)  # every conversation read, checked at NOW
    assert st.data["last_pass"]["sweep"] is True
    m = msg(ts(1), "U2", "since then")
    slack.handle_event(store(), slack.Live(fake, st), ev("C1", m), now=NOW + 30)
    fake.history["C1"].append(m)
    fake.calls.clear()

    def read() -> set[str]:
        return {p["channel"] for k, p in fake.calls if k == "conversations.history"}

    # Events unbroken since before every conversation's check: only C1 heard anything.
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + 60, live_since=NOW - 3600)
    assert read() == {"C1"}
    assert st.data["last_pass"] == {"read": 1, "unchanged": 4, "sweep": False}
    fake.calls.clear()
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + 120, live_since=NOW - 3600)
    assert read() == set()  # nothing since
    # The daily sweep reads them all again, events or not.
    fake.calls.clear()
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + slack.SWEEP_EVERY_S + 1, live_since=NOW - 3600)
    assert read() == {"C1", "D1", "D3", "G1", "C2"} and st.data["last_pass"]["sweep"] is True
    # Events broke: every conversation checked before the break is read.
    fake.calls.clear()
    slack.sync_pass(store(), fake, 14, state=st, now=NOW + slack.SWEEP_EVERY_S + 60,
                    live_since=NOW + slack.SWEEP_EVERY_S + 30)  # fmt: skip
    assert read() == {"C1", "D1", "D3", "G1", "C2"}
    # A message whose event wasn't heard but moved the resume point (state from before
    # `heard`) still counts as activity.
    later = NOW + slack.SWEEP_EVERY_S + 60
    st.advance("D1", f"{later + 5:.6f}")
    fake.calls.clear()
    slack.sync_pass(store(), fake, 14, state=st, now=later + 60, live_since=NOW)
    assert read() == {"D1"}


# --- which DMs make an alert ----------------------------------------------------------------


def test_alerts_for_announced_people_only(fake: Fake, tmp_path: Path) -> None:
    s = store()
    lv = live(fake, tmp_path / "s.json", announce=["Luca", "sam", "U1"])
    dm = msg(ts(5), "U3", "got a minute?")
    out = slack.handle_event(s, lv, ev("D3", dm, channel_type="im"), now=NOW)
    assert out == [
        {"id": f"slack:D3:{dm['ts']}", "who": "Luca", "channel": "D3", "ts": dm["ts"], "group": False}
    ]
    assert slack.handle_event(s, lv, ev("D3", dm), now=NOW) == []  # the same message again: once
    group = msg(ts(4), "U1", "group hello")
    assert [a["who"] for a in slack.handle_event(s, lv, ev("G1", group), now=NOW)] == ["Pat"]  # by id
    assert slack.handle_event(s, lv, ev("C1", msg(ts(4), "U2", "in a channel")), now=NOW) == []
    assert slack.handle_event(s, lv, ev("D1", msg(ts(3), "UME", "my own")), now=NOW) == []
    bot = msg(ts(3), "U2", "beep", bot_id="B1", subtype="bot_message")
    assert slack.handle_event(s, lv, ev("D1", bot), now=NOW) == []
    edit = {"type": "message", "subtype": "message_changed", "channel": "D1",
            "message": {**msg(ts(2), "U2", "edited"), "edited": {"ts": ts(1)}}}  # fmt: skip
    assert slack.handle_event(s, lv, edit, now=NOW) == []
    reply = msg(ts(1), "U2", "in a thread", thread_ts=ts(2))
    fake.replies[("D1", ts(2))] = [msg(ts(2), "U2", "root", reply_count=1, thread_ts=ts(2)), reply]
    assert slack.handle_event(s, lv, ev("D1", reply), now=NOW) == [
        {"id": f"slack:D1:{ts(2)}", "who": "sam", "channel": "D1", "ts": reply["ts"], "group": False}
    ]  # a reply in a DM thread: its item is the thread's
    assert (
        slack.handle_event(s, live(fake, tmp_path / "t.json"), ev("D3", msg(ts(0), "U3", "x")), now=NOW) == []
    )


def test_announce_names_match_like_people_do() -> None:
    luca = {"display": "", "real": "Luca Pradovera", "name": "lpradovera"}
    assert slack.announced_as(["Luca"], "U3", luca) == "Luca"
    assert slack.announced_as(["luca pradovera"], "U3", luca) == "luca pradovera"
    assert slack.announced_as(["@lpradovera"], "U3", luca) == "lpradovera"
    assert slack.announced_as(["u3"], "U3", luca) == "Luca"
    assert slack.announced_as(["Luc", "Luca Rossi", ""], "U3", luca) is None


# --- the Socket Mode client against a local server playing Slack ------------------------------


Script = Callable[[web.WebSocketResponse, int, "SlackSide"], Awaitable[None]]


class SlackSide:
    """apps.connections.open and the WebSocket, on 127.0.0.1 and an ephemeral port. Each
    connection runs `script(ws, n, self)`; what the client sends is kept in `acks`."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.opens = 0
        self.open_error = ""
        self.acks: list[str] = []
        self.live: set[int] = set()  # connections open now
        self.most_live = 0
        self.closed: list[int] = []
        self.port = 0
        self.tokens: list[str] = []

    async def _open(self, request: web.Request) -> web.Response:
        self.tokens.append(request.headers.get("Authorization", ""))
        if self.open_error:
            return web.json_response({"ok": False, "error": self.open_error})
        self.opens += 1
        return web.json_response({"ok": True, "url": f"ws://127.0.0.1:{self.port}/ws?n={self.opens}"})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        n = int(request.query["n"])
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.live.add(n)
        self.most_live = max(self.most_live, len(self.live))
        reader = asyncio.create_task(self._read(ws))
        try:
            await self.script(ws, n, self)
            await reader
        finally:
            reader.cancel()
            self.live.discard(n)
            self.closed.append(n)
        return ws

    async def _read(self, ws: web.WebSocketResponse) -> None:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                self.acks.append(json.loads(m.data)["envelope_id"])

    @contextlib.asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        app = web.Application()
        app.router.add_post("/api/apps.connections.open", self._open)
        app.router.add_get("/ws", self._ws)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.5)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            yield f"http://127.0.0.1:{self.port}/api"
        finally:
            await runner.cleanup()


def envelope(eid: str, event_id: str, text: str, retry: int = 0) -> dict[str, Any]:
    return {"envelope_id": eid, "type": "events_api", "accepts_response_payload": False,
            "retry_attempt": retry, "payload": {"event_id": event_id, "type": "event_callback",
            "event": {"type": "message", "channel": "D1", "user": "U2", "text": text, "ts": "1.0"}}}  # fmt: skip


async def hello(ws: web.WebSocketResponse, seconds: float | None = None) -> None:
    info = {"approximate_connection_time": seconds} if seconds else {}
    await ws.send_json({"type": "hello", "num_connections": 1, "debug_info": info})


async def until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


Reports = list[tuple[str, str | None]]


def client(api: str, got: list[dict[str, Any]], reports: Reports, **kw: Any) -> slack_events.Socket:
    async def on_event(payload: dict[str, Any]) -> None:
        got.append(payload)

    async def report(conn: str, error: BaseException | None, handled: int) -> None:
        reports.append((conn, None if error is None else str(error)))

    return slack_events.Socket("xapp-test", on_event, api=api, report=report, min_backoff_s=0.01,
                               max_backoff_s=0.05, **kw)  # fmt: skip


def test_envelopes_are_acked_events_handled_once_and_stop_closes() -> None:
    async def script(ws: web.WebSocketResponse, n: int, side: SlackSide) -> None:
        await hello(ws)
        await ws.send_json(envelope("e1", "Ev1", "hi"))
        await ws.send_json(envelope("e2", "Ev1", "hi", retry=1))  # Slack's retry: acked, not redone
        await ws.send_json({"envelope_id": "e3", "type": "slash_commands", "payload": {}})
        await ws.send_json(envelope("e4", "Ev2", "again"))

    async def go() -> tuple[list[dict[str, Any]], Reports, SlackSide, slack_events.Socket]:
        side, got, reports = SlackSide(script), list[dict[str, Any]](), Reports()
        async with side.serve() as api:
            sock = client(api, got, reports)
            stop = asyncio.Event()
            task = asyncio.create_task(sock.run(stop))
            await until(lambda: len(side.acks) == 4 and len(got) == 2)
            assert sock.healthy_since is not None
            stop.set()
            await asyncio.wait_for(task, 5)
            await until(lambda: side.closed == [1])
        return got, reports, side, sock

    got, reports, side, sock = run(go())
    assert side.acks == ["e1", "e2", "e3", "e4"] and sock.acks == 4 and sock.ack_errors == 0
    assert [p["event"]["text"] for p in got] == ["hi", "again"]
    assert sock.envelopes == {"hello": 1, "events_api": 3, "slash_commands": 1}
    assert reports == [("connected", None)] and sock.healthy_since is None
    assert side.tokens == ["Bearer xapp-test"]


def test_a_disconnect_or_the_connections_end_opens_the_next_one_first() -> None:
    async def script(ws: web.WebSocketResponse, n: int, side: SlackSide) -> None:
        if n == 1:
            await hello(ws)
            await ws.send_json({"type": "disconnect", "reason": "refresh_requested"})
            await until(lambda: side.opens >= 2)
            await ws.send_json(envelope("old", "EvOld", "still on the old one"))
        elif n == 2:
            await until(lambda: "old" in side.acks)  # the old one is still read meanwhile
            await hello(ws, seconds=0.2)  # this one ends soon: refreshed before it does
            await ws.send_json(envelope("new", "EvNew", "on the new one"))
        else:
            await hello(ws)

    async def go() -> tuple[SlackSide, list[dict[str, Any]], float | None, float | None]:
        side, got, reports = SlackSide(script), list[dict[str, Any]](), Reports()
        async with side.serve() as api:
            sock = client(api, got, reports)
            task = asyncio.create_task(sock.run())
            await until(lambda: side.opens >= 1 and sock.healthy_since is not None)
            first = sock.healthy_since
            await until(lambda: side.opens == 3 and side.closed[:2] == [1, 2])
            await until(lambda: len(got) == 2)
            after = sock.healthy_since
            task.cancel()  # shutdown: the task cancelled
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await until(lambda: side.closed == [1, 2, 3])
        return side, got, first, after

    side, got, first, after = run(go())
    assert side.most_live == 2  # the next connection was up before the old one closed
    assert {p["event"]["text"] for p in got} == {"still on the old one", "on the new one"}
    assert first is not None and after == first  # planned refreshes: events never stopped


def test_a_dropped_socket_reconnects_with_backoff() -> None:
    async def script(ws: web.WebSocketResponse, n: int, side: SlackSide) -> None:
        await hello(ws)
        if n == 1:
            await ws.close()  # no disconnect message: the network went

    async def go() -> tuple[SlackSide, Reports, bool]:
        side, got, reports = SlackSide(script), list[dict[str, Any]](), Reports()
        async with side.serve() as api:
            sock = client(api, got, reports)
            stop = asyncio.Event()
            task = asyncio.create_task(sock.run(stop))
            await until(lambda: side.opens == 2 and ("connected", None) in reports[1:])
            healthy = sock.healthy_since is not None
            stop.set()
            await asyncio.wait_for(task, 5)
        return side, reports, healthy

    side, reports, healthy = run(go())
    assert reports[:3] == [("connected", None), ("reconnecting", None), ("connected", None)] and healthy
    assert side.closed[:1] == [1]


def test_a_bad_token_or_socket_mode_off_is_failing(tmp_path: Path) -> None:
    async def script(ws: web.WebSocketResponse, n: int, side: SlackSide) -> None:
        await hello(ws)
        await ws.send_json({"type": "disconnect", "reason": "link_disabled"})

    async def go(error: str) -> Reports:
        side, got, reports = SlackSide(script), list[dict[str, Any]](), Reports()
        side.open_error = error
        async with side.serve() as api:
            sock = client(api, got, reports)
            stop = asyncio.Event()
            task = asyncio.create_task(sock.run(stop))
            await until(lambda: len([r for r in reports if r[1]]) >= 2)
            stop.set()
            await asyncio.wait_for(task, 5)
        return reports

    bad = run(go("invalid_auth"))
    assert bad[:2] == [("reconnecting", "apps.connections.open: invalid_auth")] * 2
    off = run(go(""))
    assert off[0] == ("connected", None)
    assert ("reconnecting", "Socket Mode is off for this Slack app (link_disabled)") in off


# --- in scout-memory: events stored, alerts queued, status reported ---------------------------


def test_the_events_loop_stores_alerts_and_reports(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dm = msg(ts(1), "U3", "the deck is ready")

    async def script(ws: web.WebSocketResponse, n: int, side: SlackSide) -> None:
        await hello(ws)
        await ws.send_json({"envelope_id": "e1", "type": "events_api", "payload": {
            "event_id": "Ev1", "event": {**dm, "channel": "D3", "channel_type": "im"}}})  # fmt: skip

    monkeypatch.setattr(slack, "fetcher", lambda token, **kw: fake)
    cfg = config.SlackConfig(token="xoxp-x", app_token="xapp-x", announce=["Luca"])  # noqa: S106 (made up)

    async def go() -> list[dict[str, Any]]:
        side = SlackSide(script)
        alerts = slack_events.Alerts()
        async with side.serve() as api:
            monkeypatch.setattr(slack_events, "API", api)
            socks: list[Any] = []
            task = asyncio.create_task(
                memory_service.slack_events_loop(
                    store(), cfg, slack.State(tmp_path / "s.json"), alerts, socks
                )
            )
            await until(lambda: bool(socks) and socks[0].handled == 1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return alerts.take()

    out = run(go())
    assert [(a["id"], a["who"]) for a in out] == [(f"slack:D3:{dm['ts']}", "Luca")]
    assert "text" not in out[0] and "deck" not in json.dumps(out)  # who, never what
    assert item(store(), f"slack:D3:{dm['ts']}")["body"] == "Luca Pradovera: the deck is ready"
    c = config.Config()
    c.slack = cfg
    st = {s.source: s for s in syncstate.statuses(cfg=c)}["slack_events"]
    assert st.state == "connected" and st.label == "Slack live events"


def test_events_status_reads_connected_reconnecting_failing() -> None:
    c = config.Config()
    c.slack.token, c.slack.app_token = "xoxp-x", "xapp-x"
    t = time.time()

    def state() -> str:
        return {s.source: s for s in syncstate.statuses(now=t, cfg=c)}["slack_events"].state

    syncstate.record("slack_events", 300, conn="connected", now=t - 10)
    assert state() == "connected"
    syncstate.record("slack_events", 300, conn="reconnecting", now=t - 5)
    assert state() == "reconnecting"
    syncstate.record(
        "slack_events", 300, conn="reconnecting", error=slack.SlackError("x: invalid_auth"), now=t
    )
    assert state() == "failing"
    syncstate.record("slack_events", 300, conn="connected", now=t)
    assert state() == "connected" and "Everything's syncing." in syncstate.speak(
        [s for s in syncstate.statuses(now=t, cfg=c) if s.source == "slack_events"], dt.datetime.now()
    )
    line = next(
        x
        for x in syncstate.lines(syncstate.statuses(now=t, cfg=c), dt.datetime.now())
        if x.startswith("slack_events")
    )
    assert "connected" in line
    c.slack.app_token = ""
    assert state() == "off"


def test_alerts_are_taken_once_and_go_stale() -> None:
    a = slack_events.Alerts()
    a.add({"id": "slack:D1:1.0", "who": "Sam"}, now=100.0)
    a.add({"id": "slack:D1:2.0", "who": "Sam"}, now=100.0 + a.KEEP_S + 5)
    assert [x["id"] for x in a.take(now=100.0 + a.KEEP_S + 10)] == ["slack:D1:2.0"]
    assert a.take() == []


def test_the_poll_waits_longer_while_events_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    passes: list[float | None] = []

    def fake_pass(*a: Any, live_since: float | None = None, **kw: Any) -> tuple[int, int]:
        passes.append(live_since)
        return 0, 0

    clock = [1000.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s
        if len(passes) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(slack, "fetcher", lambda token, **kw: None)
    monkeypatch.setattr(slack, "sync_pass", fake_pass)
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)
    cfg = config.SlackConfig(token="xoxp-x", every_min=15, live_every_min=60)  # noqa: S106 (made up)
    healthy: list[float | None] = [500.0]  # up since before the first pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(memory_service.slack_loop(store(), cfg, tmp_path / "s.json", live=lambda: healthy[0]))
    # Healthy all along: the next pass came an hour later, not 15 minutes.
    assert passes[:2] == [500.0, 500.0]
    assert sum(slept) >= 60 * 60  # (the third pass ends the run)
    healthy[0] = None
    passes.clear()
    slept.clear()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(memory_service.slack_loop(store(), cfg, tmp_path / "s.json", live=lambda: healthy[0]))
    assert passes[0] is None and 15 * 60 <= sum(slept) < 2 * 15 * 60 + 60


# --- the app says who messaged ----------------------------------------------------------------


def a(who: str, n: int, channel: str = "D1") -> dict[str, Any]:
    return {"id": f"slack:{channel}:{n}.0", "who": who, "channel": channel, "ts": f"{n}.0"}


def test_the_sentence_says_who_and_how_many() -> None:
    assert slack_alerts.sentence([a("Luca", 1)]) == "Luca messaged you on Slack."
    three = slack_alerts.sentence([a("Luca", 1), a("Luca", 2), a("Luca", 3)])
    assert three == "Luca sent you 3 messages on Slack."
    assert three.refs == ("slack:D1:1.0", "slack:D1:2.0", "slack:D1:3.0")
    assert slack_alerts.sentence([a("Luca", 1), a("Sam", 2, "D2")]) == "Luca and Sam messaged you on Slack."
    assert slack_alerts.sentence([a("Luca", 1), a("Sam", 2), a("Luca", 3)]) == (
        "Luca sent you 2 messages and Sam messaged you on Slack."
    )


def test_a_burst_is_said_once_after_it_settles() -> None:
    queue: list[list[dict[str, Any]]] = [[a("Luca", 1)], [a("Luca", 2)], [], [a("Luca", 2)], [], []]
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    noon = dt.datetime(2026, 10, 15, 12, 0).astimezone()
    ann = slack_alerts.Announcer(
        say, "22:00-08:00", take=lambda: queue.pop(0) if queue else [], now=lambda: noon
    )

    async def go() -> list[str | None]:
        return [await ann.step(t) for t in (0.0, 3.0, 6.0, 12.0, 30.0, 40.0)]

    out = run(go())
    assert out == [None, None, None, "Luca sent you 2 messages on Slack.", None, None]
    assert said == ["Luca sent you 2 messages on Slack."]  # message 2 came twice: said once
    assert refs.refs_of(said[0]) == ("slack:D1:1.0", "slack:D1:2.0")


def test_quiet_hours_and_a_down_service() -> None:
    said: list[str] = []

    async def say(text: str) -> None:
        said.append(text)

    night = dt.datetime(2026, 10, 15, 23, 30).astimezone()
    batches = [[a("Luca", 1)], []]
    ann = slack_alerts.Announcer(say, "22:00-08:00", take=lambda: batches.pop(0), now=lambda: night)

    def down() -> list[dict[str, Any]]:
        raise ToolError("memory service down")

    async def go() -> list[str | None]:
        out = [await ann.step(0.0), await ann.step(20.0)]
        ann.take = down
        out.append(await ann.step(40.0))
        return out

    assert run(go()) == [None, "Luca messaged you on Slack.", None] and said == []  # only logged


def test_the_app_announces_through_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assistant.announce: the floor, a chime, the sentence, its refs for "a link to that"."""

    async def go() -> tuple[list[str], tuple[str, ...]]:
        r = make()
        text = slack_alerts.sentence([a("Luca", 1)])
        await r.a.announce(text, "slack")
        last = r.a.said.last()
        assert last is not None and last.who == "slack"
        return list(r.spk.said), last.refs

    spoken, got = run(go())
    assert spoken == ["Luca messaged you on Slack."] and got == ("slack:D1:1.0",)


# --- sending, only with a yes -------------------------------------------------------------------


@pytest.fixture
def sender(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.setattr(slack_send, "_fetch", lambda post=False: fake)
    monkeypatch.setattr(slack_send, "_directory", {})
    monkeypatch.setattr(slack_send, "_drafts", {})
    return fake


def draft_id(out: str) -> str:
    return out.split()[1].rstrip(":")


def test_resolving_who_or_where(sender: Fake) -> None:
    people, convs = slack_send.directory(sender)
    assert slack_send.resolve(people, convs, "#launch") == [slack_send.Target("C1", "#launch")]
    assert slack_send.resolve(people, convs, "Sam") == [slack_send.Target("D1", "Sam Roe")]  # the DM
    assert slack_send.resolve(people, convs, "luca pradovera") == [slack_send.Target("D3", "Luca Pradovera")]
    assert [t.label for t in slack_send.resolve(people, convs, "Luca")] == ["Luca Pradovera", "Luca Rossi"]
    assert slack_send.resolve(people, convs, "Luca Rossi") == [
        slack_send.Target("U4", "Luca Rossi")
    ]  # no DM yet
    assert slack_send.resolve(people, convs, "#nowhere") == [] and slack_send.resolve(people, convs, "") == []
    calls = len(sender.calls)
    slack_send.directory(sender)
    assert len(sender.calls) == calls  # read once an hour


def test_draft_says_what_and_send_needs_the_draft(sender: Fake) -> None:
    out = run(slack_send.draft({"to": "Sam", "text": "Running 5 <min> late & sorry"}))
    assert out.startswith("Draft s") and "to Sam Roe on Slack: Running 5 <min> late & sorry" in out
    assert sender.posted == []  # a draft sends nothing
    many = run(slack_send.draft({"to": "Luca", "text": "hi"}))
    assert "Luca Pradovera, Luca Rossi" in many and "Ask the user which one" in many
    with pytest.raises(ToolError, match="Nobody on Slack"):
        run(slack_send.draft({"to": "Zed", "text": "hi"}))
    with pytest.raises(ToolError, match="text is required"):
        run(slack_send.draft({"to": "Sam", "text": " "}))
    d = draft_id(out)
    spoken, detail = brain.describe_tool("mcp__voice_app__slack_send", {"draft": d})
    assert spoken == "Send the Slack message to Sam Roe?\nIt says: Running 5 <min> late & sorry."
    assert detail.startswith("send Slack message to Sam Roe (D1)")
    sent = run(slack_send.send({"draft": d}))
    assert sent == "Sent to Sam Roe on Slack (ref slack:D1:1800000000.000100)."
    assert sender.posted == [{"channel": "D1", "text": "Running 5 &lt;min&gt; late &amp; sorry"}]
    with pytest.raises(ToolError, match="no such draft"):
        run(slack_send.send({"draft": d}))  # once
    assert len(sender.posted) == 1


def test_a_refused_send_can_be_tried_again(sender: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(slack_send._directory, "at", time.time())
    monkeypatch.setitem(slack_send._directory, "people", [])
    monkeypatch.setitem(slack_send._directory, "convs", [{"id": "CFAIL", "name": "locked"}])
    d = draft_id(run(slack_send.draft({"to": "#locked", "text": "hi"})))
    with pytest.raises(ToolError, match="not_in_channel"):
        run(slack_send.send({"draft": d}))
    assert slack_send._draft({"draft": d}) is not None  # nothing went out: still a draft


def test_send_is_confirmed_by_voice_and_never_promoted(sender: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    assert shared_tools.BY_NAME["slack_send"].asks and not shared_tools.BY_NAME["slack_draft"].asks
    assert "mcp__voice_app__slack_send" in brain.ALWAYS_ASK_TOOLS
    assert (
        trust.default().promote("slack_send")
        == "Sending Slack messages always asks; that one can't be turned off."
    )
    assert trust.action_for("sending slack messages") == "slack_send"

    async def go(answer: bool | None) -> tuple[dict[str, Any], list[str]]:
        r = make()
        asked: list[str] = []

        async def confirm(spoken: str, *rest: Any, **kw: Any) -> bool | None:
            asked.append(spoken)
            return answer

        monkeypatch.setattr(r.a, "_confirm", confirm)
        d = draft_id(
            (await r.a.run_shared_tool("desk#1", "slack_draft", {"to": "Sam", "text": "on my way"}))["text"]
        )
        return await r.a.run_shared_tool("desk#1", "slack_send", {"draft": d}), asked

    no, asked = run(go(False))
    assert no["status"] == "declined" and sender.posted == []
    assert asked == ["From desk: Send the Slack message to Sam Roe?\nIt says: on my way."]
    yes, _ = run(go(True))
    assert yes["status"] == "ok" and sender.posted == [{"channel": "D1", "text": "on my way"}]


def test_from_the_phone_in_two_steps(sender: Fake) -> None:
    assert "slack_send" in {t.name for t in phone_service.PHONE_ACTIONS}
    assert "slack_draft" in {t.name for t in phone_service.PHONE_TOOLS}

    async def go() -> list[dict[str, Any]]:
        r = make()
        d = draft_id(
            (await r.a.run_shared_tool(PHONE, "slack_draft", {"to": "#launch", "text": "shipped"}))["text"]
        )
        refused = await r.a.run_shared_tool(PHONE, "slack_send", {"draft": d})  # no yes yet
        assert sender.posted == []
        return [
            refused,
            await r.a.run_shared_tool(PHONE, "slack_send", {"draft": d}, preview=True),
            await r.a.run_shared_tool(PHONE, "slack_send", {"draft": d}, confirmed=True),
        ]

    refused, preview, done = run(go())
    assert refused["status"] == "error" and "phone" in refused["error"]
    assert preview == {"status": "ok", "text": "Send to #launch on Slack: shipped?"}
    assert done["status"] == "ok" and sender.posted == [{"channel": "C1", "text": "shipped"}]


def test_the_config_keys() -> None:
    p = Path(__file__).resolve().parents[1] / ".tmp" / f"slack-cfg-{time.time_ns()}.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(
            '[slack]\ntoken = "xoxp-1"\napp_token = "xapp-1"\nannounce = ["Luca"]\nlive_every_min = 30\n'
        )
        c = config.load(p)
        assert (c.slack.app_token, c.slack.announce, c.slack.live_every_min) == ("xapp-1", ["Luca"], 30.0)
        assert config.SlackConfig().app_token == "" and config.SlackConfig().announce == []
        p.write_text('[slack]\napp_tokens = "x"\n')
        with pytest.raises(ValueError, match=r"unknown config keys: slack\.app_tokens"):
            config.load(p)
    finally:
        p.unlink(missing_ok=True)


def test_dataclass_fields_documented() -> None:
    """Every [slack] key is in config.example.toml."""
    example = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text()
    section = example.split("[slack]", 1)[1].split("\n[", 1)[0]
    for f in dataclasses.fields(config.SlackConfig):
        assert f"# {f.name} =" in section, f.name


def test_sigterm_closes_the_socket_then_ends_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import signal

    done: list[str] = []
    real = signal.signal

    def handler(sig: int, how: Any) -> Any:
        if sig != signal.SIGTERM:
            return real(sig, how)  # asyncio.run's own (SIGINT)
        done.append(f"handler {how is signal.SIG_DFL}")
        return None

    monkeypatch.setattr(signal, "signal", handler)
    monkeypatch.setattr(os, "kill", lambda pid, sig: done.append(f"kill {pid == os.getpid()}"))

    async def close() -> None:
        done.append("closed")

    async def stuck() -> None:
        await asyncio.sleep(60)

    run(memory_service._terminate([close, stuck], wait_s=0.1))
    assert done == ["closed", "handler True", "kill True"]  # a stuck closer doesn't hold it up


def test_announce_every_dm_and_mentions(fake: Fake, tmp_path: Path) -> None:
    s = store()
    lv = live(fake, tmp_path / "s.json", announce_dms="all", announce_mentions=True)
    dm = msg(ts(5), "U2", "got a minute?")
    assert [a["who"] for a in slack.handle_event(s, lv, ev("D1", dm, channel_type="im"), now=NOW)] == ["Sam"]
    group = msg(ts(4), "U1", "group hello")
    assert [a["who"] for a in slack.handle_event(s, lv, ev("G1", group), now=NOW)] == ["Pat"]
    plain = msg(ts(4), "U2", "in a channel, not to me")
    assert slack.handle_event(s, lv, ev("C1", plain), now=NOW) == []
    to_me = msg(ts(3), "U3", "<@UME> can you look at the PR?")
    assert slack.handle_event(s, lv, ev("C1", to_me), now=NOW) == [
        {"id": f"slack:C1:{to_me['ts']}", "who": "Luca", "channel": "C1", "ts": to_me["ts"],
         "group": False, "mention": "#launch"}
    ]  # fmt: skip
    assert slack.handle_event(s, lv, ev("C1", msg(ts(2), "UME", "<@UME> note to self")), now=NOW) == []


def test_dms_off_still_says_mentions(fake: Fake, tmp_path: Path) -> None:
    s = store()
    lv = live(fake, tmp_path / "s.json", announce=["Luca"], announce_dms="off", announce_mentions=True)
    assert slack.handle_event(s, lv, ev("D3", msg(ts(5), "U3", "hi"), channel_type="im"), now=NOW) == []
    said = slack.handle_event(s, lv, ev("C1", msg(ts(4), "U3", "ping <@UME>")), now=NOW)
    assert [a.get("mention") for a in said] == ["#launch"]


def test_mention_sentences() -> None:
    from scout import slack_alerts

    one = [{"id": "slack:C1:1", "who": "Luca", "mention": "#launch"}]
    assert slack_alerts.sentence(one) == "Luca mentioned you in #launch on Slack."
    mixed = [*one, {"id": "slack:D1:2", "who": "Sam"}]
    assert slack_alerts.sentence(mixed) == "Luca mentioned you in #launch and Sam messaged you on Slack."


def test_announcing_needs_something_to_say() -> None:
    sc = config.SlackConfig
    assert not slack.announcing(sc(token="xoxp-x", app_token="xapp-x"))  # noqa: S106 (made up)
    assert slack.announcing(sc(token="xoxp-x", app_token="xapp-x", announce=["Luca"]))  # noqa: S106
    assert slack.announcing(sc(token="xoxp-x", app_token="xapp-x", announce_dms="all"))  # noqa: S106
    assert slack.announcing(sc(token="xoxp-x", app_token="xapp-x", announce_mentions=True))  # noqa: S106
    assert not slack.announcing(sc(token="xoxp-x", announce_dms="all"))  # noqa: S106 (no events)
    c = config.Config(slack=sc(announce_dms="everyone"))
    config._check(c)
    assert c.slack.announce_dms == "listed"  # an unknown value: warned, and the default used
