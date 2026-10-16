"""Slack's live events over Socket Mode, in scout-memory (docs/slack.md, Events).

With an app-level token (xapp-..., `[slack] app_token`, scope connections:write) and the
app's event subscriptions on behalf of users (message.im, message.mpim,
message.channels, message.groups), Slack pushes each message as it's posted, edited or
deleted, over a WebSocket Scout opens itself: nothing listens for Slack on this Mac.

The connection's life:

    apps.connections.open (the xapp token)  -> a wss:// URL, good for one connection
    hello                                    -> connected; it says how long the
                                                connection lasts, and a new one is
                                                opened REFRESH_MARGIN_S before that
    every envelope                           -> acked by its envelope_id at once,
                                                before the event is handled
    disconnect (warning, refresh_requested)  -> a new connection, then the old closed
    disconnect (link_disabled)               -> Socket Mode is off for the app: failing,
                                                tried again every MAX_BACKOFF_S
    a dropped socket (no pong for half the   -> reconnect, backing off 1 s to
    heartbeat: the Mac slept, the link died)    MAX_BACKOFF_S while it keeps failing
    shutdown (stop, or the task cancelled)   -> the socket closed

Events are handled one at a time, off the socket (an ack never waits for Slack's Web
API), by slack.handle_event: stored exactly as the poller stores them, the resume
point moved up. A retried envelope (Slack resends what it didn't see acked) is handled
once, by its event_id. The 15-minute poll stays as the backstop; while this connection
has been up since a pass began, the next pass waits [slack] live_every_min instead
(memory_service.slack_loop) and reads back less (slack.LIVE_LOOKBACK_S).

Status (syncstate "slack_events"): connected, reconnecting, or failing (the URL can't be
had: a bad token, Socket Mode off, no network), with a heartbeat every HEARTBEAT_S.

Alerts: a DM or group DM from someone in `[slack] announce` is queued here (Alerts), and
the Scout app takes the queue every few seconds (POST /v1/slack/alerts) and says who
messaged (slack_alerts.py). Your own messages are queued as a mark that you replied
there (where and when). The queue keeps only ids and names, never message text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from .slack import API as API
from .slack import SlackError

log = logging.getLogger(__name__)

MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 300.0
HELLO_TIMEOUT_S = 30.0  # a new connection says hello within this
HEARTBEAT_S = 30.0  # WebSocket pings; no pong in half of it and the socket is dropped
REFRESH_MARGIN_S = 120.0  # open the next connection this long before Slack ends this one
STATUS_EVERY_S = 300.0  # the "connected" heartbeat in syncstate
SEEN_EVENTS = 1000  # event ids remembered, so a retried envelope is handled once
OPEN_TIMEOUT_S = 30.0

Report = Callable[[str, BaseException | None, int], Awaitable[None]]  # (conn, error, handled)
Handler = Callable[[dict[str, Any]], Awaitable[None]]  # an events_api payload


async def _no_report(conn: str, error: BaseException | None, handled: int) -> None:
    return None


async def open_url(http: aiohttp.ClientSession, app_token: str, api: str = API) -> str:
    """apps.connections.open: the WebSocket URL for one connection. Errors are Slack's
    code (invalid_auth, not_allowed_token_type...), never the token."""
    try:
        async with http.post(
            f"{api}/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}", "User-Agent": "Scout"},
            timeout=aiohttp.ClientTimeout(total=OPEN_TIMEOUT_S),
        ) as r:
            if r.status != 200:
                raise SlackError(f"apps.connections.open: HTTP {r.status}")
            out = await r.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise SlackError(f"apps.connections.open: {type(exc).__name__}") from None
    if not isinstance(out, dict) or not out.get("ok") or not out.get("url"):
        err = out.get("error") if isinstance(out, dict) else None
        raise SlackError(f"apps.connections.open: {err or 'no url'}")
    return str(out["url"])


class _Conn:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.hello = asyncio.Event()
        self.refresh = asyncio.Event()  # time for the next connection
        self.fatal = ""  # link_disabled: Socket Mode is off for the app
        self.task: asyncio.Task[None] | None = None
        self.timer: asyncio.TimerHandle | None = None

    @property
    def dropped(self) -> bool:
        return self.task is None or self.task.done()

    async def close(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        with contextlib.suppress(Exception):
            await self.ws.close()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


class Socket:
    """One live Socket Mode connection kept up (see the module's doc)."""

    def __init__(
        self,
        app_token: str,
        on_event: Handler,
        *,
        api: str = API,
        report: Report = _no_report,
        min_backoff_s: float = MIN_BACKOFF_S,
        max_backoff_s: float = MAX_BACKOFF_S,
        status_every_s: float = STATUS_EVERY_S,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self.app_token, self.on_event, self.api, self.report = app_token, on_event, api, report
        self.min_backoff_s, self.max_backoff_s = min_backoff_s, max_backoff_s
        self.status_every_s, self.heartbeat_s = status_every_s, heartbeat_s
        # Since when events have come in without a break (connected, and every event
        # handled); None while down. The poller reads it (memory_service.slack_loop).
        self.healthy_since: float | None = None
        self.envelopes: Counter[str] = Counter()  # by type
        self.acks = 0
        self.ack_errors = 0
        self.handled = 0
        self.opens = 0
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._seen: deque[str] = deque(maxlen=SEEN_EVENTS)
        self._reported = 0  # self.handled at the last report

    async def _report(self, conn: str, error: BaseException | None = None) -> None:
        n, self._reported = self.handled - self._reported, self.handled
        try:
            await self.report(conn, error, n)
        except Exception:
            log.exception("slack events: status not recorded")

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Keep a connection up until `stop` is set (or the task is cancelled)."""
        stop = stop or asyncio.Event()
        stopping = asyncio.create_task(stop.wait())
        worker = asyncio.create_task(self._work())
        conn: _Conn | None = None
        backoff = self.min_backoff_s
        try:
            async with aiohttp.ClientSession(trust_env=False) as http:  # never through a proxy
                while not stop.is_set():
                    if conn is None:
                        try:
                            conn = await self._connect(http)
                        except (SlackError, aiohttp.ClientError, TimeoutError, OSError) as exc:
                            log.warning("slack events: can't connect (%s); again in %.0f s", exc, backoff)
                            await self._report("reconnecting", exc)
                            await _sleep_or_stop(stop, backoff)
                            backoff = min(backoff * 2, self.max_backoff_s)
                            continue
                        backoff = self.min_backoff_s
                        self.healthy_since = time.time()
                        log.info("slack events: connected")
                        await self._report("connected")
                    waits: set[asyncio.Future[Any]] = {stopping, asyncio.ensure_future(conn.refresh.wait())}
                    if conn.task is not None:
                        waits.add(conn.task)
                    done, _ = await asyncio.wait(
                        waits, timeout=self.status_every_s, return_when=asyncio.FIRST_COMPLETED
                    )
                    for w in waits - {stopping, conn.task}:
                        w.cancel()
                    if stop.is_set():
                        break
                    if conn.fatal:
                        err = SlackError(f"Socket Mode is off for this Slack app ({conn.fatal})")
                        await conn.close()
                        conn, self.healthy_since = None, None
                        log.warning("slack events: %s; again in %.0f s", err, self.max_backoff_s)
                        await self._report("reconnecting", err)
                        await _sleep_or_stop(stop, self.max_backoff_s)
                        continue
                    if conn.refresh.is_set():  # planned: the next connection, then this one closed
                        try:
                            new = await self._connect(http)
                        except (SlackError, aiohttp.ClientError, TimeoutError, OSError) as exc:
                            log.info("slack events: refresh failed (%s)", exc)
                            if conn.dropped:
                                await conn.close()
                                conn, self.healthy_since = None, None
                                await self._report("reconnecting", exc)
                            else:
                                conn.refresh.clear()  # the old one still works: keep it for now
                            await _sleep_or_stop(stop, backoff)
                            backoff = min(backoff * 2, self.max_backoff_s)
                            continue
                        old, conn = conn, new
                        await old.close()
                        backoff = self.min_backoff_s
                        log.info("slack events: connection refreshed")
                        await self._report("connected")
                        continue
                    if conn.dropped:  # unplanned: the Mac slept, the network went
                        log.info("slack events: the connection dropped; reconnecting")
                        await conn.close()
                        conn, self.healthy_since = None, None
                        await self._report("reconnecting")
                        await _sleep_or_stop(stop, backoff)
                        backoff = min(backoff * 2, self.max_backoff_s)
                        continue
                    if not done:
                        await self._report("connected")  # the heartbeat
        finally:
            stopping.cancel()
            if conn is not None:
                await conn.close()
            self.healthy_since = None
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            log.info("slack events: stopped")

    async def _connect(self, http: aiohttp.ClientSession) -> _Conn:
        url = await open_url(http, self.app_token, self.api)
        self.opens += 1
        ws = await asyncio.wait_for(
            http.ws_connect(url, heartbeat=self.heartbeat_s, autoping=True), OPEN_TIMEOUT_S
        )
        conn = _Conn(ws)
        conn.task = asyncio.create_task(self._read(conn))
        hello = asyncio.ensure_future(conn.hello.wait())
        await asyncio.wait({hello, conn.task}, timeout=HELLO_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        hello.cancel()
        if not conn.hello.is_set():
            await conn.close()
            raise SlackError("Socket Mode: no hello")
        return conn

    async def _read(self, conn: _Conn) -> None:
        async for msg in conn.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                env = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(env, dict):
                continue
            kind = str(env.get("type") or "")
            self.envelopes[kind] += 1
            if eid := env.get("envelope_id"):  # acked before anything else is done with it
                try:
                    await conn.ws.send_json({"envelope_id": eid})
                    self.acks += 1
                except (ConnectionError, RuntimeError, aiohttp.ClientError):
                    self.ack_errors += 1
                    return  # the socket is going: Slack resends what wasn't acked
            if kind == "hello":
                conn.hello.set()
                secs = (env.get("debug_info") or {}).get("approximate_connection_time")
                if isinstance(secs, (int, float)) and secs > 0:
                    delay = max(secs - REFRESH_MARGIN_S, secs / 2)
                    conn.timer = asyncio.get_running_loop().call_later(delay, conn.refresh.set)
            elif kind == "disconnect":
                if env.get("reason") == "link_disabled":
                    conn.fatal = "link_disabled"
                conn.refresh.set()
            elif kind == "events_api":
                payload = env.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("event"), dict):
                    eid = str(payload.get("event_id") or "")
                    if eid and eid in self._seen:
                        continue
                    if eid:
                        self._seen.append(eid)
                    self._queue.put_nowait(payload)

    async def _work(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self.on_event(payload)
                self.handled += 1
            except Exception as exc:
                # The poll's next pass reads this conversation back fully: events are only
                # unbroken from now on.
                log.warning("slack events: an event wasn't stored (%s)", type(exc).__name__)
                if self.healthy_since is not None:
                    self.healthy_since = time.time()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class Alerts:
    """DMs to say out loud, waiting for the Scout app to take them (one taker). Each is
    {id, who, channel, ts, group}: the item's id (for "give me a link to that") and the
    name to say, never the text; or {channel, ts, replied}: you wrote there (the app
    doesn't announce a conversation you're in). Ones not taken within KEEP_S are
    dropped: stale news."""

    KEEP_S = 600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def add(self, alert: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:  # the stale ones go now too, so no taker means no growth
            self._items = [a for a in self._items if now - float(a["at"]) <= self.KEEP_S]
            self._items.append({**alert, "at": now})

    def take(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            out = [a for a in self._items if now - float(a["at"]) <= self.KEEP_S]
            self._items = []
        return out
