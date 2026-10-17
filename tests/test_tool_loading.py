"""Which of Scout's own tools are in the Claude session's prompt from the start
(claude.always_load_tools): marked with Claude Code's _meta "anthropic/alwaysLoad", so
"remember X" doesn't wait on a ToolSearch round trip. The rest stay deferred."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from test_routing import make

from scout import assistant as assistant_mod
from scout.config import ClaudeConfig
from scout.memory import Memory
from scout.tools import ALWAYS_LOAD, SERVER, Timers, build_server

DEFAULT = ["remember", "forget", "tasks_list", "reminder_add"]


def listed(server: Any) -> dict[str, Any]:
    """The tools as Claude Code gets them (tools/list), by name."""
    entry = server["instance"].get_request_handler("tools/list")
    result = asyncio.run(entry.handler(None, entry.params_type()))
    return {t.name: t for t in result.tools}


def always_loaded(server: Any) -> set[str]:
    return {name for name, t in listed(server).items() if (t.meta or {}).get(ALWAYS_LOAD) is True}


def test_the_default_list_is_small_and_explicit() -> None:
    assert ClaudeConfig().always_load_tools == DEFAULT


def test_only_the_listed_tools_are_always_loaded(tmp_path: Path) -> None:
    server, names = build_server(
        Timers(lambda label: None), memory=Memory(tmp_path / "m.json"), always_load=DEFAULT
    )
    assert always_loaded(server) == set(DEFAULT)
    assert len(names) > 40  # everything else is still there, loaded on demand
    plain, _ = build_server(Timers(lambda label: None), memory=Memory(tmp_path / "m.json"))
    assert always_loaded(plain) == set()


def test_the_room_and_the_phone_sessions_get_them(monkeypatch: pytest.MonkeyPatch) -> None:
    r = make()
    assert always_loaded(r.a.brain.tool_server) == set(DEFAULT)
    assert {f"mcp__{SERVER}__{n}" for n in DEFAULT} <= set(r.a.brain.tool_names)

    seen: list[Any] = []

    class Phone:
        def __init__(self, cfg: Any, server: Any, *args: Any) -> None:
            seen.append(server)

        async def ask(self, text: str) -> AsyncIterator[tuple[str, Any]]:
            yield "text", "Done."

    monkeypatch.setattr(assistant_mod, "PhoneBrain", Phone)
    asyncio.run(r.a._answer_text("phone", "explain how a heat pump works in winter"))
    (server,) = seen
    assert always_loaded(server) == set(DEFAULT)


def test_what_they_add_to_the_prompt(tmp_path: Path) -> None:
    """Roughly 4 characters a token: the four tools' definitions stay well under 1,500."""
    server, _ = build_server(
        Timers(lambda label: None), memory=Memory(tmp_path / "m.json"), always_load=DEFAULT
    )
    tools = listed(server)
    size = sum(
        len(
            json.dumps(
                {"name": n, "description": tools[n].description, "input_schema": tools[n].input_schema}
            )
        )
        for n in DEFAULT
    )
    assert size / 4 < 1500, size
    print(f"always-loaded tools: {size} characters, about {size // 4} tokens")
