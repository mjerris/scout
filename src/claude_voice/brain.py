"""Persistent Claude Code agent session via the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast, get_args
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from claude_agent_sdk.types import McpSdkServerConfig, PermissionMode, SettingSource

from .config import ROOT, ClaudeConfig
from .rules import Rules
import contextlib

log = logging.getLogger(__name__)

VOICE_PROMPT = """\
# Voice mode
You are running as an always-on voice assistant on the user's Mac mini. The user
talks to you through local speech recognition (expect transcription errors; infer
the intended meaning) and hears your replies through text-to-speech.
- Reply in short, natural spoken sentences. Default to one to three sentences
  unless the user asks for detail.
- No markdown, bullet lists, tables, code blocks, URLs or emoji in replies; they
  are read aloud. Summarize instead.
- Before long-running work, say in one short sentence what you are about to do.
- When you produce code or documents, write them to files and briefly say where,
  rather than reading them aloud.
- This voice front-end (wake word, speech recognition, text-to-speech, spoken
  approval prompts, web page, config.toml) is the claude-voice app at {root}.
  Another Claude session owns that project. Never edit it, its config or its
  processes yourself. When the user asks to change how you listen, talk, ask
  for approval or behave, call the request_app_change tool with a clear
  description, then tell them briefly that you passed it to the owner.
- For searching Netflix/YouTube/Google, opening apps, Chrome tabs, full screen,
  play/pause, volume and timers, use your voice_app tools; they run without
  asking. Opening a URL, fetching a page and any shell command are confirmed by
  voice. Never read credentials or keys (~/.ssh, ~/.aws, tokens); that is blocked.
- Actions that need permission are confirmed by the user's spoken yes or no. If
  they said no, ask what they want instead of retrying; if they didn't answer,
  say so briefly and offer to try again.
"""

# (spoken, detail) -> True/False, "always", or None for no answer
Confirm = Callable[[str, str], Awaitable[bool | str | None]]
_DENIED = "The user declined this action by voice."
_NO_ANSWER = "The user did not answer the spoken confirmation in time, so this was not run."

_SHELL_META = re.compile(r"[;&|`$<>(){}\\\n]")


def is_safe_bash(command: str, prefixes: list[str]) -> bool:
    """A single simple command (no chaining, substitution or redirection) whose
    first word is one of the configured safe programs."""
    cmd = command.strip()
    if not cmd or _SHELL_META.search(cmd):
        return False
    return cmd.split()[0] in prefixes


_SPOKEN_PROGRAM = re.compile(r"[A-Za-z0-9_.+-]+")


_VALUE_OPTIONS = {"-C", "-c", "-R", "--repo", "--git-dir", "--work-tree", "-u", "--user"}


def _argv_words(cmd: str) -> list[str]:
    """The words of a shell command without env assignments, wrappers (env, sudo),
    options or their values: "env X=1 git -C ~/x push origin" -> git push origin."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    while parts and (
        ("=" in parts[0] and not parts[0].startswith("-")) or parts[0] in ("env", "sudo", "command")
    ):
        parts.pop(0)
    out: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part in _VALUE_OPTIONS:
            i += 2
            continue
        if not part.startswith("-"):
            out.append(Path(part).name if not out else part)
        i += 1
    return out


def _command_words(cmd: str) -> list[str]:
    """Program and subcommand, for speaking: "git -C x push origin" -> ["git", "push"]."""
    return [w for w in _argv_words(cmd) if _SPOKEN_PROGRAM.fullmatch(w)][:2]


def _where(path: str) -> str:
    p = Path(path)
    return f"{p.name} in {p.parent.name}" if p.parent.name else p.name


def describe_tool(name: str, args: dict[str, Any]) -> tuple[str, str]:
    """(short spoken question, full description for the web page). The page puts
    the actual command or path first, untruncated, then the agent's description."""
    if name == "Bash":
        cmd = args.get("command", "")
        words = _command_words(cmd)
        desc = args.get("description") or ""
        return (f"Run {' '.join(words)}?" if words else "Run command?"), (
            f"run: {cmd}" + (f"  ({desc})" if desc else "")
        )
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        path = args.get("file_path") or args.get("notebook_path") or "a file"
        return f"Edit {_where(path)}?", f"edit {path}"
    if name == "Write":
        path = args.get("file_path", "a file")
        return f"Write {_where(path)}?", f"write {path}"
    if name in ("WebFetch", "mcp__voice_app__open_url", "mcp__voice_app__new_tab"):
        url = args.get("url", "")
        host = urlparse(url).netloc or "the web"
        verb = "Fetch" if name == "WebFetch" else "Open"
        return f"{verb} {host}?", f"{verb.lower()} {url}"
    if name.startswith("mcp__"):
        parts = name.split("__")
        tool, server = parts[-1].replace("_", " "), parts[1].replace("_", " ")
        return f"Use {tool}?", f"use {tool} from {server}"
    return f"Use {name}?", f"use the {name} tool"


def _permission_mode(mode: str) -> PermissionMode | None:
    if not mode:
        return None
    if mode not in get_args(PermissionMode):
        raise ValueError(f"claude.permission_mode must be one of {get_args(PermissionMode)}, not {mode!r}")
    return cast("PermissionMode", mode)


def _setting_sources(sources: list[str]) -> list[SettingSource]:
    allowed = get_args(SettingSource)
    bad = [s for s in sources if s not in allowed]
    if bad:
        raise ValueError(f"claude.setting_sources entries must be in {allowed}, not {bad}")
    return cast("list[SettingSource]", sources)


# Never readable by the voice agent, whatever the user answers.
SECRET_PATHS = [
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.netrc",
    "~/.config/gh",
    "~/.docker/config.json",
    "~/.kube",
    "~/Library/Keychains",
    "~/.claude/.credentials.json",
    str(ROOT / "state"),
]
_FILE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
_READ_TOOLS = ("Read", "Glob", "Grep", "NotebookRead", "LS")
# URL-opening voice tools ask (a URL can carry data off the machine); "always" is per site.
URL_TOOLS = ("mcp__voice_app__open_url", "mcp__voice_app__new_tab")


def _canon(path: str, cwd: Path) -> str:
    """Absolute, resolved, case-folded (macOS paths are case-insensitive)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = cwd / p
    with contextlib.suppress(OSError):
        p = p.resolve()
    return str(p).casefold()


def _under(path: str, roots: list[str], cwd: Path) -> bool:
    c = _canon(path, cwd)
    return any(c == r or c.startswith(r.rstrip("/") + "/") for r in (_canon(x, cwd) for x in roots))


_ASK_PATTERN = re.compile(r"^Bash\((.+?)(?::\*)?\)$")


def matches_always_ask(command: str, patterns: list[str]) -> bool:
    """Does a Bash command match an always-ask rule like "Bash(git push:*)", in any
    of its usual spellings (with -C/-R options, env, sudo)? Every simple command in
    a chain is checked ("cd x && git push")."""
    for piece in re.split(r"&&|\|\||;|\|", command):
        plain = " ".join(piece.split())
        words = " ".join(_argv_words(piece))
        for pat in patterns:
            m = _ASK_PATTERN.match(pat)
            if m and (plain.startswith(m.group(1).strip()) or words.startswith(m.group(1).strip())):
                return True
    return False


def _preview(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    """Tool arguments for the log, without file contents or long strings."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k in ("content", "new_string", "old_string", "edits", "new_source"):
            out[k] = f"<{len(str(v))} chars>"
        elif isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + "…"
        else:
            out[k] = v
    return out


class Brain:
    def __init__(
        self,
        cfg: ClaudeConfig,
        confirm: Confirm,
        tool_server: McpSdkServerConfig,
        tool_names: list[str],
        rules: Rules,
        notify: Callable[[str, str], None],
    ) -> None:
        self.cfg = cfg
        self.confirm = confirm
        self.tool_server = tool_server
        self.tool_names = tool_names  # pre-approved voice_app tools
        self.rules = rules
        self.notify = notify  # (event, text): tells the user about saved rules
        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None
        self._lock = asyncio.Lock()
        self._options()  # validates permission_mode, setting_sources and approval_policy now

    def _options(self) -> ClaudeAgentOptions:
        append = VOICE_PROMPT.format(root=ROOT) + (
            "\n" + self.cfg.extra_system_prompt if self.cfg.extra_system_prompt else ""
        )
        strict = self.cfg.approval_policy == "strict"
        if self.cfg.approval_policy not in ("strict", "settings", "settings_no_hooks"):
            raise ValueError(f"unknown approval_policy {self.cfg.approval_policy!r}")
        project = str(ROOT)
        overrides: dict[str, Any] = {
            "permissions": {
                "allow": [*(t for t in self.tool_names if t not in URL_TOOLS), "WebSearch"],
                # "//" = absolute path in Claude Code permission rules ("/x" is relative to the
                # settings file). The app is maintained by its owner session, not by voice;
                # secrets are never readable; mcp__voice is the desk-session voice tool.
                "deny": [
                    *(f"{tool}(/{project}/**)" for tool in _FILE_TOOLS),
                    *(f"{tool}(/{_canon(p, ROOT)}/**)" for tool in _FILE_TOOLS for p in [project]),
                    *(
                        f"Read(/{Path(p).expanduser()}{'/**' if not Path(p).suffix else ''})"
                        for p in SECRET_PATHS
                    ),
                    "mcp__voice",
                ],
                # Ask beats allow, so these always reach the voice prompt even when
                # ~/.claude settings allow them (verified against the CLI).
                "ask": ["Bash", "WebFetch", *URL_TOOLS, *self.cfg.always_ask],
            }
        }
        if self.cfg.approval_policy == "settings_no_hooks":
            overrides["disableAllHooks"] = True
        return ClaudeAgentOptions(
            cwd=str(Path(self.cfg.cwd).expanduser()),
            settings=json.dumps(overrides),
            env={"CLAUDE_VOICE_ROOM": "1"},  # our own MCP voice tool refuses to run inside the room agent
            mcp_servers={"voice_app": self.tool_server},
            extra_args={"remote-control": self.cfg.remote_control} if self.cfg.remote_control else {},
            model=self.cfg.model or None,
            permission_mode=_permission_mode(self.cfg.permission_mode),  # "" = settings' defaultMode
            setting_sources=_setting_sources(self.cfg.setting_sources),
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            can_use_tool=self._can_use_tool,
            # Strict policy: the hook gates every tool call, ahead of any allow
            # rules inherited from ~/.claude settings.
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use], timeout=900)]} if strict else None,
        )

    async def _pre_tool_use(self, hook_input: Any, tool_use_id: str | None, context: Any) -> Any:
        name, args = hook_input["tool_name"], hook_input["tool_input"]
        if reason := self._forbidden(name, args):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        if self._auto_allowed(name, args) or (
            name not in ("Bash", "WebFetch", *URL_TOOLS)
            and (name in self.cfg.auto_allow_tools or name in self.tool_names)
        ):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "on the voice auto-allow list",
                }
            }
        answer = await self._ask(name, args)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if answer else "deny",
                "permissionDecisionReason": "approved by voice"
                if answer
                else _NO_ANSWER
                if answer is None
                else _DENIED,
            }
        }

    @property
    def _cwd(self) -> Path:
        return Path(self.cfg.cwd).expanduser()

    def _auto_allowed(self, name: str, args: dict[str, Any]) -> bool:
        """Runs without asking: the short list of harmless single commands."""
        return name == "Bash" and is_safe_bash(args.get("command", ""), self.cfg.auto_allow_commands)

    def _forbidden(self, name: str, args: dict[str, Any]) -> str | None:
        """Calls the voice agent may never make, whatever the user answers."""
        own_app = "This voice app is maintained by its owner session; use request_app_change instead of changing it."
        if name.startswith("mcp__voice__"):
            return "The voice tool is for other sessions; you already own the voice."
        path = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")
        if name in _FILE_TOOLS and path and _under(path, [str(ROOT)], self._cwd):
            return own_app
        if name in _READ_TOOLS and path and _under(path, SECRET_PATHS, self._cwd):
            return "Reading credentials and keys is blocked for the voice agent."
        if name == "Bash":
            cmd = str(args.get("command", ""))
            low = cmd.casefold()
            home = str(Path.home()).casefold()
            app = str(ROOT).casefold()
            if app in low or app.replace(home, "~", 1) in low:
                return own_app
            for secret in SECRET_PATHS:
                s = str(Path(secret).expanduser()).casefold()
                if s in low or s.replace(home, "~", 1) in low:
                    return "Reading credentials and keys is blocked for the voice agent."
        return None

    async def _ask(self, name: str, args: dict[str, Any]) -> bool | None:
        # "Always ask" commands (git push, PR merge...) can't be skipped by a saved rule.
        always_ask = name == "Bash" and matches_always_ask(str(args.get("command", "")), self.cfg.always_ask)
        if not always_ask and self.rules.matches(name, args):
            log.info("allowed by saved rule: %s %s", name, _preview(args))
            return True
        log.info("permission request: %s %s", name, _preview(args))
        answer = await self.confirm(*describe_tool(name, args))
        if answer == "always":
            if not always_ask and self.rules.add(name, args) is not None:
                self.notify("rules", "Okay, I won't ask about that again.")
            else:
                self.notify("rules", "Okay, just this once. That kind of action always asks.")
            return True
        return None if answer is None else bool(answer)

    async def _can_use_tool(
        self, name: str, args: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if reason := self._forbidden(name, args):
            return PermissionResultDeny(message=reason)
        if self._auto_allowed(name, args):
            return PermissionResultAllow()
        answer = await self._ask(name, args)
        if answer:
            return PermissionResultAllow()
        return PermissionResultDeny(message=_NO_ANSWER if answer is None else _DENIED)

    async def _ensure(self) -> ClaudeSDKClient:
        if self.client is None:
            self.client = ClaudeSDKClient(self._options())
            await self.client.connect()
            log.info("Claude session connected")
        return self.client

    async def ask(self, text: str) -> AsyncIterator[tuple[str, Any]]:
        """Yields ("text", str), ("tool", (name, input)) and finally ("result", ResultMessage)."""
        async with self._lock:
            client = await self._ensure()
            await client.query(text)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if msg.parent_tool_use_id:  # subagent chatter
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            yield "text", block.text
                        elif isinstance(block, ToolUseBlock):
                            yield "tool", (block.name, block.input)
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id
                    yield "result", msg

    async def interrupt(self) -> None:
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception:
                log.exception("interrupt failed")

    async def reset(self) -> None:
        async with self._lock:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    log.exception("disconnect failed")
            self.client = None
            self.session_id = None
            log.info("Claude session reset")

    async def close(self) -> None:
        """Disconnect immediately, without waiting for an in-flight turn."""
        client, self.client = self.client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                log.exception("disconnect failed")
