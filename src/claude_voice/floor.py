"""The floor ("conch"): one speaker at a time on the shared mic and speakers.

Holders are the room assistant ("room") and Claude sessions talking through
the MCP server (named by their session). A holder takes the floor for one
exchange; it can keep it briefly between exchanges (`hold`) so a back-and-
forth isn't interrupted, and others queue in FIFO order.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class Floor:
    def __init__(self, hold_ttl: float = 10.0):
        self.hold_ttl = hold_ttl
        self.holder: str | None = None
        self.active = False  # mid-exchange, as opposed to holding between exchanges
        self.held_until = 0.0
        self._waiters: deque[tuple[str, asyncio.Future]] = deque()

    def owner(self) -> str | None:
        """Who has the floor right now, if anyone (lapsed holds don't count)."""
        if self.holder and not self.active and time.monotonic() > self.held_until:
            self.holder = None
        return self.holder

    def _free_for(self, agent: str) -> bool:
        owner = self.owner()
        if owner == agent:
            return not self.active  # re-take your own hold, but not a second exchange at once
        if owner is not None:
            return False
        # Free: honour the queue order.
        return not self._waiters or self._waiters[0][0] == agent

    def try_acquire(self, agent: str) -> bool:
        if not self._free_for(agent):
            return False
        if self._waiters and self._waiters[0][0] == agent:
            self._waiters.popleft()
        self.holder, self.active = agent, True
        return True

    async def acquire(self, agent: str, wait: float = 0.0) -> bool:
        """Take the floor, waiting up to `wait` seconds in the queue."""
        if self.try_acquire(agent):
            return True
        if wait <= 0:
            return False
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((agent, fut))
        deadline = time.monotonic() + wait
        try:
            while time.monotonic() < deadline:
                if self.try_acquire(agent):
                    return True
                # Wake on release, or poll so lapsing holds are noticed.
                try:
                    await asyncio.wait_for(asyncio.shield(fut), min(0.5, deadline - time.monotonic()))
                except TimeoutError:
                    pass
                if fut.done():
                    fut = asyncio.get_running_loop().create_future()
                    self._replace_waiter(agent, fut)
            return self.try_acquire(agent)
        finally:
            self._waiters = deque(w for w in self._waiters if w[1] is not fut)

    def _replace_waiter(self, agent: str, fut: asyncio.Future) -> None:
        for i, (a, _) in enumerate(self._waiters):
            if a == agent:
                self._waiters[i] = (agent, fut)
                return
        self._waiters.append((agent, fut))

    def release(self, agent: str, hold: bool = False, ttl: float | None = None) -> None:
        if self.holder != agent:
            return
        if hold:
            self.active = False
            self.held_until = time.monotonic() + (ttl if ttl is not None else self.hold_ttl)
        else:
            self.holder, self.active, self.held_until = None, False, 0.0
        for _, fut in self._waiters:
            if not fut.done():
                fut.set_result(None)

    def status(self) -> dict:
        owner = self.owner()
        return {
            "holder": owner,
            "speaking": bool(owner and self.active),
            "held_for_s": round(max(0.0, self.held_until - time.monotonic()), 1) if owner and not self.active else 0,
            "queue": [a for a, _ in self._waiters],
        }
