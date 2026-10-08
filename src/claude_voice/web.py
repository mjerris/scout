"""LAN control page: live transcript, typed requests, approve/deny, stop, mute."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import secrets
import socket
import uuid
from importlib import resources
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .assistant import Assistant
from .audio import decode_to_pcm
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


def _lan_ip() -> str | None:
    """The address this Mac uses on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith(("127.", "100.")) else ip


async def _tailscale_ip() -> str | None:
    from .mac import ToolError, _run

    for exe in ("/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        if Path(exe).exists():
            try:
                ip = (await _run(exe, "ip", "-4", timeout=5)).splitlines()
            except ToolError:
                continue
            if ip:
                return ip[0].strip()
    return None


async def resolve_hosts(names: list[str]) -> list[str]:
    """Turn the configured names into concrete addresses, skipping any that aren't up."""
    out: list[str] = []
    for name in names:
        ip: str | None
        if name == "localhost":
            ip = "127.0.0.1"
        elif name == "lan":
            ip = _lan_ip()
        elif name == "tailscale":
            ip = await _tailscale_ip()
        else:
            ip = name
        if ip is None:
            log.warning("web: no %s address right now; not listening there", name)
        elif ip not in out:
            out.append(ip)
    return out


async def start(cfg: WebConfig, assistant: Assistant) -> web.AppRunner:
    token = load_token()
    page = resources.files(__package__).joinpath("web.html").read_text()

    def authed(req: web.Request) -> bool:
        bearer = req.headers.get("Authorization", "")
        given = (
            (bearer[7:] if bearer.startswith("Bearer ") else "")
            or req.query.get("token")
            or req.cookies.get(_COOKIE, "")
        )
        return hmac.compare_digest(given.encode(), token.encode())  # bytes: non-ASCII input can't raise

    # --- API for the MCP server (and anything else holding the token) -------------

    async def api_discuss(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            raise web.HTTPUnauthorized()
        try:
            b = await req.json()
            result = await assistant.discuss(
                agent=str(b.get("agent") or "session"),
                message=b.get("message"),
                listen=bool(b.get("wait_for_response", True)),
                timeout=min(float(b.get("listen_timeout", 30)), 300.0),
                hold=bool(b.get("hold_floor", False)),
                voice=b.get("voice") or None,
                wait_for_floor=min(float(b.get("wait_for_floor", 0)), 300.0),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"status": "error", "error": str(exc)}, status=400)
        return web.json_response(result)

    async def api_status(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            raise web.HTTPUnauthorized()
        return web.json_response(assistant.voice_status())

    async def index(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            return web.Response(status=401, text="Missing or wrong token. Use the URL printed in the log.")
        resp = web.Response(text=page, content_type="text/html")
        resp.set_cookie(_COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="Strict")
        return resp

    async def ws_handler(req: web.Request) -> web.StreamResponse:
        if not authed(req):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=30 * 1024 * 1024)
        await ws.prepare(req)
        q = assistant.subscribe()
        client_id = uuid.uuid4().hex
        bg: set[asyncio.Task[Any]] = set()  # keep fire-and-forget tasks referenced
        await ws.send_json(
            {
                "type": "hello",
                "history": list(assistant.history),
                "rules": assistant.rules.listing(),
                **assistant.snapshot(),
            }
        )

        async def send_audio(text: str) -> None:
            try:
                wav = await assistant.speaker.synth_wav(text)
                await ws.send_json({"type": "audio_reply", "data": base64.b64encode(wav).decode()})
            except Exception:
                log.exception("could not send reply audio")

        async def pump() -> None:
            while True:
                ev = await q.get()
                if ev["type"] != "say":
                    await ws.send_json(ev)
                if ev["type"] == "say":
                    if ev.get("to") == client_id and ev.get("text"):
                        await send_audio(ev["text"])  # in order, one sentence block at a time
                    continue

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
                output = m.get("output", "device")  # where replies play: device | mini | both
                to_mini = output in ("mini", "both")
                to_me = client_id if output in ("device", "both") else None
                if kind == "say" and str(m.get("text", "")).strip():
                    ignored = await assistant.submit_text(m["text"].strip(), speak=to_mini, client=to_me)
                    if ignored and ignored not in ("gate",):
                        await ws.send_json({"type": "error", "text": f"Not sent: {ignored}."})
                elif kind == "confirm":
                    assistant.answer_confirm("always" if m.get("always") else bool(m.get("approved")))
                elif kind == "remove_rule":
                    assistant.remove_rule(int(m.get("index", -1)))
                elif kind == "stop":
                    await assistant.stop()
                elif kind == "mute":
                    assistant.set_mic_muted(bool(m.get("value")))
                elif kind == "reset":
                    task = asyncio.create_task(assistant.reset())
                    bg.add(task)
                    task.add_done_callback(bg.discard)
                elif kind == "audio":
                    try:
                        clip = base64.b64decode(m.get("data", ""))
                        if len(clip) > 20_000_000:
                            raise ValueError("clip too long")
                        pcm = await decode_to_pcm(clip)
                        result = await assistant.submit_audio(pcm, speak=to_mini, client=to_me)
                        await ws.send_json({"type": "ptt_result", **result})
                    except Exception as exc:
                        log.exception("push-to-talk failed")
                        await ws.send_json({"type": "error", "text": f"Couldn't use that recording: {exc}"})
        finally:
            pump_task.cancel()
            assistant.unsubscribe(q)
        return ws

    app = web.Application(client_max_size=30 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/", index),
            web.get("/ws", ws_handler),
            web.post("/api/discuss", api_discuss),
            web.get("/api/status", api_status),
        ]
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    hosts = await resolve_hosts(cfg.hosts)
    for host in hosts:
        await web.TCPSite(runner, host, cfg.port).start()
    log.info("web: listening on %s", ", ".join(f"{h}:{cfg.port}" for h in hosts))
    shown = next((h for h in hosts if not h.startswith("127.")), hosts[0] if hosts else "127.0.0.1")
    log.info("web UI: http://%s:%d/?token=%s", shown, cfg.port, token)
    return runner
