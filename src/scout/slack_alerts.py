"""Saying a Slack DM out loud, in the Scout app (docs/slack.md, Alerts).

scout-memory hears Slack's live events (slack_events.py) and queues a DM or group DM
([slack] announce_dms: from the people in `announce`, or all) and @-mentions of you
(announce_mentions); this takes that queue every POLL_S
(memory_client.slack_alerts) and says who messaged:

    one message     "Luca messaged you on Slack."
    a burst         "Luca sent you 3 messages on Slack."
    two people      "Luca sent you 2 messages and Sam messaged you on Slack."
    a mention       "Luca mentioned you in #launch on Slack."

Only who, never what they wrote. A burst is said once: messages are gathered until
SETTLE_S pass with nothing new (at most HOLD_S after the first). It's said like the
pulse: through Assistant.announce, which waits for the floor, and only logged in the
pulse's quiet hours. Each message is said at most once (scout-memory hands each out
once; the app also remembers what it said). The sentence carries the messages' item ids
(refs.Spoken), so "give me a link to that" opens it in Slack.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from . import memory_client, refs
from .freebusy import join
from .mac import ToolError
from .starlink import in_quiet_hours

log = logging.getLogger(__name__)

POLL_S = 5.0
SETTLE_S = 8.0
HOLD_S = 60.0
REMEMBER = 500  # messages said, kept to never say one twice

Take = Callable[[], list[dict[str, Any]]]


def sentence(alerts: Sequence[dict[str, Any]]) -> refs.Spoken:
    """Who messaged or mentioned you, each person once with their count, in the order
    they wrote: "Luca mentioned you in #launch and Sam messaged you on Slack." """
    counts: OrderedDict[str, int] = OrderedDict()
    mentions: OrderedDict[tuple[str, str], int] = OrderedDict()
    for a in alerts:
        who = str(a.get("who") or "Someone")
        if a.get("mention"):
            k = (who, str(a["mention"]))
            mentions[k] = mentions.get(k, 0) + 1
        else:
            counts[who] = counts.get(who, 0) + 1
    if not mentions and all(n == 1 for n in counts.values()) and len(counts) > 1:
        text = f"{join(list(counts))} messaged you on Slack."
    else:
        parts = [f"{who} mentioned you in {where}" if n == 1 else f"{who} mentioned you {n} times in {where}"
                 for (who, where), n in mentions.items()]  # fmt: skip
        parts += [
            f"{who} messaged you" if n == 1 else f"{who} sent you {n} messages" for who, n in counts.items()
        ]
        text = f"{join(parts)} on Slack."
    return refs.Spoken(text, [str(a["id"]) for a in alerts if a.get("id")])


class Announcer:
    def __init__(
        self,
        say: Callable[[str], Awaitable[None]],
        quiet_hours: str,
        take: Take = memory_client.slack_alerts,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
    ) -> None:
        self.say, self.quiet_hours, self.take, self.now = say, quiet_hours, take, now
        self.pending: list[dict[str, Any]] = []
        self.first = self.last = 0.0  # when the first and the newest pending one came
        self.said: OrderedDict[str, None] = OrderedDict()

    async def step(self, mono: float | None = None) -> str | None:
        """Take what's new; say the pending ones once the burst has settled. Returns what
        was said (or would have been, in quiet hours)."""
        mono = time.monotonic() if mono is None else mono
        try:
            new = await asyncio.to_thread(self.take)
        except ToolError as exc:  # scout-memory is restarting: next time
            log.debug("slack alerts: none taken (%s)", exc)
            new = []
        for a in new:
            key = f"{a.get('channel')}:{a.get('ts')}"
            if key in self.said:
                continue
            self.said[key] = None
            while len(self.said) > REMEMBER:
                self.said.popitem(last=False)
            if not self.pending:
                self.first = mono
            self.pending.append(a)
            self.last = mono
        if not self.pending or (mono - self.last < SETTLE_S and mono - self.first < HOLD_S):
            return None
        text, self.pending = sentence(self.pending), []
        if in_quiet_hours(self.quiet_hours, self.now()):
            log.info("slack alerts: quiet hours, a DM alert not said")
            return text
        await self.say(text)
        return text

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception:
                log.exception("slack alerts: a check failed")
            await asyncio.sleep(POLL_S)
