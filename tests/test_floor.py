"""The floor (one speaker at a time), "hang on", and pronunciation rules."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest

from scout.floor import Floor
from scout.pronounce import Pronouncer, parse
from scout.speech import split_wait

T = TypeVar("T")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_one_holder_at_a_time() -> None:
    f = Floor()
    assert f.try_acquire("room")
    assert not f.try_acquire("desk#1")
    f.release("room")
    assert f.try_acquire("desk#1")


def test_release_by_non_holder_is_ignored() -> None:
    f = Floor()
    f.try_acquire("room")
    f.release("desk#1")
    assert f.owner() == "room"


def test_hold_keeps_floor_then_lapses() -> None:
    async def go() -> tuple[bool, bool, bool]:
        f = Floor()
        f.try_acquire("desk#1")
        f.release("desk#1", hold=True, ttl=0.3)
        blocked = not f.try_acquire("room")
        regained = f.try_acquire("desk#1")  # the holder can come straight back
        f.release("desk#1", hold=True, ttl=0.3)
        await asyncio.sleep(0.4)
        return blocked, regained, f.try_acquire("room")

    assert run(go()) == (True, True, True)


def test_waiters_get_floor_in_order() -> None:
    async def go() -> list[str]:
        f = Floor()
        f.try_acquire("room")
        order: list[str] = []

        async def want(agent: str) -> None:
            if await f.acquire(agent, wait=2):
                order.append(agent)
                await asyncio.sleep(0.05)
                f.release(agent)

        tasks = [asyncio.create_task(want("a")), asyncio.create_task(want("b"))]
        await asyncio.sleep(0.05)
        assert f.status()["queue"] == ["a", "b"]
        f.release("room")
        await asyncio.gather(*tasks)
        return order

    assert run(go()) == ["a", "b"]


def test_wait_times_out() -> None:
    async def go() -> tuple[bool, list[str]]:
        f = Floor()
        f.try_acquire("room")
        return await f.acquire("desk#1", wait=0.3), f.status()["queue"]

    assert run(go()) == (False, [])


@pytest.mark.parametrize(
    "text, rest, waiting",
    [
        ("Open Netflix and hang on", "Open Netflix and", True),
        ("what about, give me a sec.", "what about", True),
        ("wait", "", True),
        ("Run the tests. Hold on!", "Run the tests", True),
        ("I want to wait for the train", "I want to wait for the train", False),
        ("hold on to that file", "hold on to that file", False),
    ],
)
def test_split_wait(text: str, rest: str, waiting: bool) -> None:
    assert split_wait(text) == (rest, waiting)


def test_pronounce_defaults() -> None:
    p = Pronouncer()
    assert p.tts("edit config.toml") == "edit config dot toml"
    assert p.stt("hey clawed, open signal wire") == "hey Claude, open SignalWire"


def test_pronounce_user_rules(tmp_path: Path) -> None:
    f = tmp_path / "pronounce.txt"
    f.write_text("TTS  '\\bTali\\b'  'Tar-lee'  # dog\nbogus line\nSTT '\\b3M\\b' 'three M'\n")
    p = Pronouncer(f)
    assert p.tts("Tali is a dog") == "Tar-lee is a dog"
    assert p.stt("3M stock") == "three M stock"
    assert len(parse("TTS (unclosed x")) == 0


def test_same_agent_cannot_hold_two_exchanges() -> None:
    f = Floor()
    assert f.try_acquire("desk#1")
    assert not f.try_acquire("desk#1")  # a parallel second call waits
    f.release("desk#1", hold=True, ttl=5)
    assert f.try_acquire("desk#1")  # but it can re-take its own hold


def test_web_hosts_never_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import web

    async def no_tailscale() -> None:
        return None

    monkeypatch.setattr(web, "_lan_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(web, "_tailscale_ip", no_tailscale)
    hosts = asyncio.run(web.resolve_hosts(["localhost", "lan", "tailscale", "10.0.0.5"]))
    assert hosts == ["127.0.0.1", "10.0.0.5"]
