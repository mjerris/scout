"""MCP server: lets any Claude Code session talk out loud through the running
claude-voice app, sharing its mic, voice, utterance gate and floor.

Register once (user scope):
    claude mcp add --scope user voice -- uv run --project ~/src/claude-voice python -m claude_voice.mcp_server
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

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


TOKEN_PATH = ROOT / "state" / "web_token"
_RESTART = "scripts/launchd.sh restart"
SPEECH_CHARS_PER_SECOND = 10.0  # slower than real speech, so a long message never times out
LISTEN_EXTENSIONS_S = 120.0  # room for "hang on" extensions while listening


def _agent() -> str:
    """This session's floor name: the directory (shortened) plus the full pid, so two
    sessions in the same directory never share an identity."""
    name = (os.environ.get("CLAUDE_VOICE_AGENT") or Path.cwd().name or "session").strip() or "session"
    return f"{name[:30]}#{os.getpid()}"


class _Unavailable(Exception):
    pass


def _token_name() -> str:
    return str(TOKEN_PATH.relative_to(ROOT)) if TOKEN_PATH.is_relative_to(ROOT) else str(TOKEN_PATH)


def _base() -> tuple[str, dict[str, Any]]:
    cfg = load()
    if not cfg.web.enabled:
        raise _Unavailable("the voice app's web server is off (web.enabled = false in config.toml)")
    if "localhost" not in cfg.web.hosts and "127.0.0.1" not in cfg.web.hosts:
        raise _Unavailable('the voice app doesn\'t listen on localhost (add "localhost" to web.hosts)')
    try:
        token = TOKEN_PATH.read_text().strip()
    except FileNotFoundError:
        raise _Unavailable(
            f"no {_token_name()}: claude-voice hasn't started here yet, or the token "
            f"file was deleted; start or restart the app ({_RESTART}) to make one"
        ) from None
    except OSError as exc:
        raise _Unavailable(f"can't read {TOKEN_PATH}: {exc}") from None
    if not token:
        raise _Unavailable(f"{_token_name()} is empty; restart the app ({_RESTART}) to make a new token")
    return f"http://127.0.0.1:{cfg.web.port}", {"Authorization": f"Bearer {token}"}


async def _call(
    method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30
) -> dict[str, Any]:
    if os.environ.get("CLAUDE_VOICE_ROOM"):
        return {
            "status": "error",
            "error": "the room assistant already owns the voice; it cannot use this tool",
        }
    try:
        base, headers = _base()
    except _Unavailable as exc:
        return {"status": "error", "error": str(exc)}
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=5)) as s,
            s.request(method, base + path, json=body, headers=headers) as r,
        ):
            if r.status == 401:
                return {
                    "status": "error",
                    "error": f"the app rejected the token in {_token_name()}: the file "
                    f"changed after the app started; restart the app ({_RESTART}) so both use the same one",
                }
            if r.status != 200:
                text = await r.text()
                try:
                    detail = json.loads(text).get("error") or text
                except (ValueError, AttributeError):
                    detail = text
                return {"status": "error", "error": f"HTTP {r.status}: {str(detail)[:200]}"}
            data = await r.json()
            if not isinstance(data, dict):
                return {"status": "error", "error": f"unexpected reply from the app: {str(data)[:200]}"}
            return data
    except aiohttp.ClientConnectorError:
        return {
            "status": "error",
            "error": f"claude-voice is not running on {base} (start it with {_RESTART})",
        }
    except TimeoutError:
        return {
            "status": "error",
            "error": f"no answer from the voice app within {timeout:.0f} s; the message may already "
            "have been spoken, so check voice_status before repeating it",
        }
    except (aiohttp.ClientError, ValueError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def call_timeout(message: str, listen_timeout: float, wait_for_floor: float, listen: bool) -> float:
    """How long a discuss call may take: queueing for the floor, speaking the whole
    message, then listening (with "hang on" extensions), plus a margin."""
    speak = len(message) / SPEECH_CHARS_PER_SECOND
    hear = (min(max(listen_timeout, 0.0), 300.0) + LISTEN_EXTENSIONS_S) if listen else 0.0
    return min(max(wait_for_floor, 0.0), 300.0) + speak + hear + 60.0


def describe(r: dict[str, Any], wait_for_response: bool) -> str:
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
    if status == "muted":
        return (
            "(not spoken: the mic is muted in the voice app, so the user can't answer by voice; "
            "they can unmute it on its web page)"
        )
    if status == "floor_busy":
        holder = r.get("holder")
        queue = [a for a in r.get("queue") or [] if a]
        if holder:
            waiting = f"; waiting: {', '.join(queue)}" if queue else ""
            return f"(not spoken: {holder} has the floor{waiting})"
        if queue:
            return f"(not spoken: others are queued for the floor ahead of you: {', '.join(queue)})"
        return "(not spoken: the floor was busy; try again)"
    return f"(error: {r.get('error', r)})"


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
        timeout=call_timeout(message, listen_timeout, wait_for_floor, wait_for_response),
    )
    return describe(r, wait_for_response)


@mcp.tool()
async def voice_status() -> str:
    """Who has the floor, whether the room assistant is busy, and the available voices."""
    return json.dumps(await _call("GET", "/api/status"), indent=1)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
