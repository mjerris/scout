"""The floor ("conch"): one speaker at a time on the shared mic and speakers.

Holders are the room assistant ("room"), timers ("timer") and Claude sessions
talking through the MCP server (named by their session). A holder takes the
floor for one exchange; it can keep it briefly between exchanges (`hold`) so a
back-and-forth isn't interrupted, and others queue in FIFO order.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(eq=False)
class _Waiter:
    """One queued acquire() call. Identity, not name, so two waiters with the
    same agent name can't be confused with each other."""

    agent: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class Floor:
    def __init__(self, hold_ttl: float = 10.0) -> None:
        self.hold_ttl = hold_ttl
        self.holder: str | None = None
        self.active = False  # mid-exchange, as opposed to holding between exchanges
        self.held_until = 0.0
        self._queue: list[_Waiter] = []

    def owner(self) -> str | None:
        """Who has the floor right now, if anyone (lapsed holds don't count)."""
        if self.holder and not self.active and time.monotonic() > self.held_until:
            self.holder = None
        return self.holder

    def _free_for(self, agent: str, me: _Waiter | None) -> bool:
        owner = self.owner()
        if owner == agent:
            return not self.active  # re-take your own hold, but not a second exchange at once
        if owner is not None:
            return False
        # Free: honour the queue order.
        return not self._queue or self._queue[0] is me

    def _take(self, agent: str, me: _Waiter | None) -> bool:
        if not self._free_for(agent, me):
            return False
        if me is not None and me in self._queue:
            self._queue.remove(me)
        self.holder, self.active = agent, True
        self._wake_all()
        return True

    def try_acquire(self, agent: str) -> bool:
        return self._take(agent, None)

    async def acquire(self, agent: str, wait: float = 0.0, cancel: asyncio.Event | None = None) -> bool:
        """Take the floor, waiting up to `wait` seconds in the queue. Setting
        `cancel` gives up at once (e.g. the user said "stop")."""
        if self._take(agent, None):
            return True
        if wait <= 0 or (cancel is not None and cancel.is_set()):
            return False
        me = _Waiter(agent)
        self._queue.append(me)
        deadline = time.monotonic() + wait
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    return False
                if self._take(agent, me):
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                # Wake on any release, or poll so lapsing holds are noticed.
                me.wake.clear()
                waits: list[asyncio.Task[Any]] = [asyncio.ensure_future(me.wake.wait())]
                if cancel is not None:
                    waits.append(asyncio.ensure_future(cancel.wait()))
                _, pending = await asyncio.wait(waits, timeout=min(0.5, left))
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
        finally:
            if me in self._queue:
                self._queue.remove(me)
                self._wake_all()  # the next waiter may now be first

    def _wake_all(self) -> None:
        for w in self._queue:
            w.wake.set()

    def release(self, agent: str, hold: bool = False, ttl: float | None = None) -> None:
        if self.holder != agent:
            return
        if hold:
            self.active = False
            self.held_until = time.monotonic() + (ttl if ttl is not None else self.hold_ttl)
        else:
            self.holder, self.active, self.held_until = None, False, 0.0
        self._wake_all()

    def status(self) -> dict[str, Any]:
        owner = self.owner()
        return {
            "holder": owner,
            "speaking": bool(owner and self.active),
            "held_for_s": round(max(0.0, self.held_until - time.monotonic()), 1)
            if owner and not self.active
            else 0,
            "queue": [w.agent for w in self._queue],
        }
