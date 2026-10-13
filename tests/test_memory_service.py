"""scout-memory over real HTTP: the service on an ephemeral 127.0.0.1 port in a thread,
the real client talking to it. Store, search, memories, auth, errors, and the one-time
move from the app's old store."""

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from scout import index, memory_client, memory_service
from scout.mac import ToolError
from scout.memory import Memory

from test_index import FakeEmbedder


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[memory_client.Client, memory_service.Service]]:
    ix = index.Index(tmp_path / "memory.db", FakeEmbedder(), min_similarity=0.3)
    tok = tmp_path / "memory.token"
    svc = memory_service.Service(ix, Memory(tmp_path / "memory.json"), memory_service.token(tok))
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    port: list[int] = []
    runner = web.AppRunner(svc.app(), access_log=None)

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        loop.run_until_complete(site.start())
        port.append(site._server.sockets[0].getsockname()[1])  # type: ignore[union-attr]
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    yield memory_client.Client(f"http://127.0.0.1:{port[0]}", tok), svc
    asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    ix.close()


def test_store_and_search_over_http(served: tuple[memory_client.Client, Any]) -> None:
    store = memory_client.Store(served[0])
    assert store.put("n1", "note", "Oil change", "Oil changed in Cadillac", ts=100) is True
    assert store.put("n1", "note", "Oil change", "Oil changed in Cadillac", ts=100) is False
    store.put("m1", "memory", "Pat is my manager", "Pat is my manager", ts=200)
    assert [h.id for h in store.search("oil", semantic=False)] == ["n1"]
    assert store.search("who is my boss")[0].id == "m1"  # by meaning
    assert store.count() == 2 and store.count("note") == 1
    assert [h.id for h in store.recent(["note", "memory"], 5)] == ["m1", "n1"]
    assert store.sync("note", []) == (0, 1) and store.get("n1") is None
    assert store.prune("memory", 300) == 1 and store.count() == 0
    assert store.health()["ok"] is True


def test_memories_over_http(served: tuple[memory_client.Client, Any]) -> None:
    mem = memory_client.RemoteMemory(served[0])
    assert mem.add("Pat is my manager") == "Pat is my manager"
    assert mem.matching("pat") == ["Pat is my manager"]
    assert mem.relevant("who is my boss") == ["Pat is my manager"]  # close in meaning, no shared words
    assert mem.newest(5) == ["Pat is my manager"] and len(mem.facts) == 1
    with pytest.raises(ToolError, match="nothing to remember"):
        mem.add("")
    assert mem.forget("pat") == ["Pat is my manager"] and mem.facts == []
    assert memory_client.Store(served[0]).count("memory") == 0  # the index follows the facts


def test_a_wrong_token_is_refused(served: tuple[memory_client.Client, Any], tmp_path: Path) -> None:
    bad = tmp_path / "other.token"
    bad.write_text("not-the-token")
    with pytest.raises(ToolError, match="unauthorized"):
        memory_client.Store(memory_client.Client(served[0].url, bad)).count()


def test_a_stopped_service_says_so(tmp_path: Path) -> None:
    tok = tmp_path / "t"
    tok.write_text("x")
    with pytest.raises(ToolError, match="memory service isn't running"):
        memory_client.Store(memory_client.Client("http://127.0.0.1:9", tok)).count()


def test_token_is_private_and_stable(tmp_path: Path) -> None:
    p = tmp_path / "memory.token"
    t = memory_service.token(p)
    assert memory_service.token(p) == t and (p.stat().st_mode & 0o777) == 0o600


def test_the_old_app_store_moves_over_once(tmp_path: Path) -> None:
    old = tmp_path / "scout.db"
    legacy = index.Index(old, None)
    legacy.put("turn:1", "turn", "is starlink down", "is starlink down\nScout: online", ts=5)
    legacy.close()
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT)")  # app tables stay
    conn.commit()
    conn.close()
    new = index.Index(tmp_path / "memory.db", None)
    assert memory_service.migrate(new, old) == 1 and memory_service.migrate(new, old) == 0
    assert new.get("turn:1") is not None
    tables = {r[0] for r in sqlite3.connect(old).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "docs" not in tables and "notes" in tables
