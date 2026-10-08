"""Persistent Claude Code agent session via the Claude Agent SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
- For opening sites and apps, searching Netflix/YouTube/Google, Chrome tabs,
  full screen, play/pause, volume and timers, use your voice_app tools. They
  run without asking; raw osascript or open commands need the user's approval.
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


def describe_tool(name: str, args: dict[str, Any]) -> tuple[str, str]:
    """(short spoken question, fuller description for the web page)."""
    if name == "Bash":
        cmd = args.get("command", "")
        what = args.get("description") or cmd
        return "Run command?", f"run a command: {what[:200]}" + (f" ({cmd[:200]})" if cmd != what else "")
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        path = args.get("file_path") or args.get("notebook_path") or "a file"
        return f"Edit {Path(path).name}?", f"edit {path}"
    if name == "Write":
        path = args.get("file_path", "a file")
        return f"Write {Path(path).name}?", f"write {path}"
    if name == "WebFetch":
        host = urlparse(args.get("url", "")).netloc or "the web"
        return f"Fetch {host}?", f"fetch {args.get('url', '')}"
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
                # The app itself is maintained by its owner session, not by voice.
                "allow": [*self.tool_names, "WebSearch", "WebFetch"],
                # "//" = absolute path in Claude Code permission rules ("/x" is relative to the settings file).
                "deny": [
                    f"Edit(/{project}/**)",
                    f"Write(/{project}/**)",
                    f"MultiEdit(/{project}/**)",
                    f"NotebookEdit(/{project}/**)",
                    "mcp__voice",
                ],  # the desk-session voice tool; the room agent already owns the voice
                "ask": [] if strict else list(self.cfg.always_ask),
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
        if (
            name in self.cfg.auto_allow_tools
            or name in self.tool_names
            or (name == "Bash" and is_safe_bash(args.get("command", ""), self.cfg.auto_allow_commands))
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

    def _forbidden(self, name: str, args: dict[str, Any]) -> str | None:
        """Calls the voice agent may never make, whatever the user answers."""
        if name.startswith("mcp__voice__"):
            return "The voice tool is for other sessions; you already own the voice."
        if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            path = args.get("file_path") or args.get("notebook_path") or ""
            try:
                inside = Path(path).expanduser().resolve().is_relative_to(ROOT)
            except (OSError, ValueError):
                inside = False
            if inside:
                return (
                    "This voice app is maintained by its owner session; use "
                    "request_app_change instead of editing it."
                )
        return None

    async def _ask(self, name: str, args: dict[str, Any]) -> bool | None:
        if self.rules.matches(name, args):
            log.info("allowed by saved rule: %s %s", name, args)
            return True
        log.info("permission request: %s %s", name, args)
        answer = await self.confirm(*describe_tool(name, args))
        if answer == "always":
            if self.rules.add(name, args) is not None:
                self.notify("rules", "Okay, I won't ask about that again.")
            else:
                self.notify("rules", "Okay, just this once. File edits always ask.")
            return True
        return None if answer is None else bool(answer)

    async def _can_use_tool(
        self, name: str, args: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if reason := self._forbidden(name, args):
            return PermissionResultDeny(message=reason)
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
