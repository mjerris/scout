"""LAN control page: live transcript, typed requests, approve/deny, stop, mute."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import socket
from importlib import resources

from aiohttp import WSMsgType, web

from .assistant import Assistant
from .config import ROOT, WebConfig

log = logging.getLogger(__name__)
_COOKIE = "cv_token"


def load_token() -> str:
    path = ROOT / "state" / "web_token"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(18)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def start(cfg: WebConfig, assistant: Assistant) -> web.AppRunner:
    token = load_token()
    page = resources.files(__package__).joinpath("web.html").read_text()

    def authed(req: web.Request) -> bool:
        given = req.query.get("token") or req.cookies.get(_COOKIE, "")
        return hmac.compare_digest(given, token)

    async def index(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            return web.Response(status=401, text="Missing or wrong token. Use the URL printed in the log.")
        resp = web.Response(text=page, content_type="text/html")
        resp.set_cookie(_COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Strict")
        return resp

    async def ws_handler(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)
        q = assistant.subscribe()
        await ws.send_json({"type": "hello", "history": list(assistant.history),
                            "rules": assistant.rules.listing(), **assistant.snapshot()})

        async def pump():
            while True:
                await ws.send_json(await q.get())

        pump_task = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                kind = m.get("type")
                if kind == "say" and str(m.get("text", "")).strip():
                    if not assistant.start_turn(m["text"].strip(), speak=bool(m.get("speak", True))):
                        await ws.send_json({"type": "error", "text": "Busy with another request; stop it first."})
                elif kind == "confirm":
                    assistant.answer_confirm("always" if m.get("always") else bool(m.get("approved")))
                elif kind == "remove_rule":
                    assistant.remove_rule(int(m.get("index", -1)))
                elif kind == "stop":
                    await assistant.stop()
                elif kind == "mute":
                    assistant.set_mic_muted(bool(m.get("value")))
                elif kind == "reset":
                    asyncio.create_task(assistant.reset())
        finally:
            pump_task.cancel()
            assistant.unsubscribe(q)
        return ws

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    host = _lan_ip() if cfg.host in ("0.0.0.0", "") else cfg.host
    log.info("web UI: http://%s:%d/?token=%s", host, cfg.port, token)
    return runner
