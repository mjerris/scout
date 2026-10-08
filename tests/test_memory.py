"""Scout's memory: storing, recalling and forgetting facts in plain code, the room
agent's memory tools, and what of it reaches Claude's request context."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from scout import memory, privacy
from scout.mac import ToolError
from scout.memory import Memory
from scout.tools import memory_tools


def call(fn: Callable[[dict[str, Any]], Awaitable[str]], args: dict[str, Any]) -> str:
    async def go() -> str:
        return await fn(args)

    return asyncio.run(go())


@pytest.fixture
def mem(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "state" / "memory.json")


def test_facts_are_kept_on_disk(mem: Memory, tmp_path: Path) -> None:
    mem.add("my dentist is Dr. Lee.")
    mem.add("Pat is my manager")
    mem.add("Pat is my manager")  # said again: kept once
    again = Memory(mem.path)
    assert again.newest() == ["Pat is my manager", "my dentist is Dr. Lee"]
    assert json.loads(mem.path.read_text())["facts"][0]["text"] == "my dentist is Dr. Lee"


def test_unreadable_file_starts_empty(tmp_path: Path) -> None:
    (tmp_path / "memory.json").write_text("{not json")
    assert Memory(tmp_path / "memory.json").facts == []


@pytest.mark.parametrize(("fact", "error"), [("", "nothing"), ("x" * 301, "too long"), ("a\x00b", "control")])
def test_bad_facts_are_refused(mem: Memory, fact: str, error: str) -> None:
    with pytest.raises(ToolError, match=error):
        mem.add(fact)


def test_matching_and_relevance(mem: Memory) -> None:
    mem.add("my dentist is Dr. Lee")
    mem.add("Pat is my manager")
    mem.add("Pat's birthday is in May")
    assert mem.matching("Pat") == ["Pat's birthday is in May", "Pat is my manager"]
    assert mem.matching("my dentist") == ["my dentist is Dr. Lee"]
    assert mem.matching("the") == []  # no content words
    assert mem.relevant("email Pat about the managers meeting")[0] == "Pat is my manager"
    assert mem.relevant("what time is it") == []


@pytest.mark.parametrize(
    ("said", "answer"),
    [
        ("remember that my dentist is Dr. Lee", "Okay, I'll remember that your dentist is Dr. Lee."),
        (
            "Scout, please remember I am allergic to cats.",
            "Okay, I'll remember that you are allergic to cats.",
        ),
        ("note that Pat is my manager", "Okay, I'll remember that Pat is your manager."),
    ],
)
def test_remembering_by_voice(mem: Memory, said: str, answer: str) -> None:
    assert memory.answer(said, mem) == answer
    assert len(mem.facts) == 1


def test_recalling_and_forgetting_by_voice(mem: Memory) -> None:
    assert memory.answer("what do you remember", mem) == "You haven't asked me to remember anything yet."
    mem.add("my dentist is Dr. Lee")
    mem.add("Pat is my manager")
    assert memory.answer("who's my dentist?", mem) == "You told me your dentist is Dr. Lee."
    assert memory.answer("who is Pat", mem) == "You told me Pat is your manager."
    assert memory.answer("what do you know about Pat", mem) == "You told me Pat is your manager."
    assert memory.answer("what do you know about Sam", mem) == "You haven't told me anything about Sam."
    assert memory.answer("what do you remember", mem) == (
        "You told me Pat is your manager; your dentist is Dr. Lee."
    )
    assert memory.answer("forget about my dentist", mem) == "Okay, I forgot that your dentist is Dr. Lee."
    assert memory.answer("forget Sam", mem) == "I don't have anything remembered about Sam."
    assert mem.newest() == ["Pat is my manager"]


@pytest.mark.parametrize(
    "said",
    [
        "remember to call Sam tomorrow",  # a reminder: Claude
        "forget it",
        "who is the president of France",  # nothing remembered about it: Claude
        "what is the weather",
        "what's on my calendar today",
        "tell me a joke",
    ],
)
def test_other_requests_are_not_memory(mem: Memory, said: str) -> None:
    mem.add("my dentist is Dr. Lee")
    assert memory.answer(said, mem) is None
    assert len(mem.facts) == 1


def test_the_room_agents_memory_tools(mem: Memory) -> None:
    tools = {name: fn for name, _, _, fn in memory_tools(mem)}
    assert call(tools["remember"], {"fact": "Pat is my manager"}) == "Remembered: Pat is my manager"
    assert "Pat is my manager" in call(tools["recall"], {"about": "Pat"})
    try:
        privacy.configure("strict")
        assert "Pat is my manager" in call(tools["recall"], {"about": "Pat"})  # need-to-know
        with pytest.raises(ToolError, match="Strict"):
            call(tools["recall"], {"about": ""})  # but never the whole list
        assert call(tools["forget"], {"about": "Pat"}) == "Forgot 1 remembered fact."
    finally:
        privacy.configure("balanced")
    call(tools["remember"], {"fact": "Pat is my manager"})
    assert call(tools["forget"], {"about": "Pat"}) == "Forgot: Pat is my manager"
    assert call(tools["forget"], {"about": "Pat"}) == "Nothing remembered matches that."


def test_memory_tools_are_preapproved_for_the_room(mem: Memory) -> None:
    from scout.brain import ASKING_TOOLS
    from scout.tools import Timers, build_server

    _, names = build_server(Timers(lambda label: None), memory=mem)
    for name in ("remember", "forget", "recall"):
        assert f"mcp__voice_app__{name}" in names and f"mcp__voice_app__{name}" not in ASKING_TOOLS
