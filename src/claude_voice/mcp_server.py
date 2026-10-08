"""MCP server: lets any Claude Code session talk out loud through the running
claude-voice app, sharing its mic, voice, utterance gate and floor.

Register once (user scope):
    claude mcp add --scope user voice -- uv run --project ~/src/claude-voice python -m claude_voice.mcp_server
"""

from __future__ import annotations

import json
import os

from pathlib import Path

import aiohttp
from mcp.server.mcpserver import MCPServer

from .config import ROOT, load

INSTRUCTIONS = """\
Voice I/O through the always-on claude-voice app on this Mac. Use `discuss`
to say something out loud and (by default) hear the user's spoken reply,
which comes back as the tool result. Speak in short, plain spoken sentences:
no markdown, code or URLs (they are read aloud). The reply has already been
filtered for noise, so an empty reply means the user said nothing. The app
has one speaker at a time (the floor); set hold_floor=true when your next
discuss call follows straight on, so nobody cuts in between turns.

The user may be watching this session's text as well as listening. When a
discuss result comes back with `Heard: "..."`, show it once, immediately, as a
single quote line (> 🎙 ...) before you act. Never repeat earlier heard lines
in later messages or summaries."""

mcp = MCPServer("voice", instructions=INSTRUCTIONS)


def _agent() -> str:
    name = os.environ.get("CLAUDE_VOICE_AGENT") or Path.cwd().name or "session"
    return f"{name}#{os.getpid() % 10000}"


def _base() -> tuple[str, dict]:
    cfg = load()
    token = (ROOT / "state" / "web_token").read_text().strip()
    return f"http://127.0.0.1:{cfg.web.port}", {"Authorization": f"Bearer {token}"}


async def _call(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    if os.environ.get("CLAUDE_VOICE_ROOM"):
        return {
            "status": "error",
            "error": "the room assistant already owns the voice; it cannot use this tool",
        }
    try:
        base, headers = _base()
    except OSError:
        return {"status": "error", "error": "claude-voice has never run here (no state/web_token)"}
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s,
            s.request(method, base + path, json=body, headers=headers) as r,
        ):
            if r.status == 401:
                return {
                    "status": "error",
                    "error": "the app's token changed; restart this session's voice MCP server (/mcp)",
                }
            if r.status != 200:
                return {"status": "error", "error": f"HTTP {r.status}: {(await r.text())[:200]}"}
            return await r.json()
    except aiohttp.ClientConnectorError:
        return {
            "status": "error",
            "error": "claude-voice is not running (start it with scripts/launchd.sh restart)",
        }
    except TimeoutError:
        return {"status": "error", "error": "timed out waiting for the voice app"}
    except (aiohttp.ClientError, ValueError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
async def discuss(
    message: str = "",
    wait_for_response: bool = True,
    listen_timeout: float = 30.0,
    hold_floor: bool = False,
    wait_for_floor: float = 15.0,
    voice: str = "",
) -> str:
    """Say `message` out loud on the Mac and, unless wait_for_response is false,
    return what the user says back (no wake word needed).

    listen_timeout: seconds to wait for the reply (max 300). hold_floor: keep
    the floor briefly after this call for your next one. wait_for_floor:
    seconds to queue if someone else is talking. voice: a Kokoro voice name
    (see voice_status), or empty for the default.
    """
    r = await _call(
        "POST",
        "/api/discuss",
        {
            "agent": _agent(),
            "message": message,
            "wait_for_response": wait_for_response,
            "listen_timeout": listen_timeout,
            "hold_floor": hold_floor,
            "wait_for_floor": wait_for_floor,
            "voice": voice or None,
        },
        timeout=listen_timeout + wait_for_floor + 180,
    )
    status = r.get("status")
    if status == "ok":
        if not wait_for_response:
            return "(spoken)"
        text = r.get("text") or ""
        return f'Heard: "{text}"' if text else "(the user said nothing)"
    if status == "no_reply":
        return "(no reply within the listen timeout)"
    if status == "stopped":
        return "(the user said stop)"
    if status == "floor_busy":
        return f"(not spoken: {r.get('holder')} has the floor; queue: {r.get('queue')})"
    return f"(error: {r.get('error', r)})"


@mcp.tool()
async def voice_status() -> str:
    """Who has the floor, whether the room assistant is busy, and the available voices."""
    return json.dumps(await _call("GET", "/api/status"), indent=1)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
