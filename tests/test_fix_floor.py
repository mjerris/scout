"""A release wakes a waiting acquire at once, even when it also waits on a cancel event."""

from __future__ import annotations

import asyncio
import time

from scout.floor import Floor


def test_release_wakes_a_waiter_that_has_a_cancel_event() -> None:
    asyncio.run(_wakes())


async def _wakes() -> None:
    floor = Floor()
    assert await floor.acquire("a")
    cancel = asyncio.Event()
    waiter = asyncio.create_task(floor.acquire("b", wait=5, cancel=cancel))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    floor.release("a")
    assert await waiter
    assert time.monotonic() - t0 < 0.2
