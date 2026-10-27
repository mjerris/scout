"""Saying a Slack DM out loud, in the Scout app (docs/slack.md, Alerts).

scout-memory hears Slack's live events (slack_events.py) and queues a DM or group DM
([slack] announce_dms: from the people in `announce`, or all) and @-mentions of you
(announce_mentions), and a mark for each message of your own; this takes that queue
every POLL_S (memory_client.slack_alerts) and says who messaged, never how often:

    a DM            "Luca messaged you on Slack." (one message or a burst)
    two people      "Luca and Sam messaged you on Slack."
    a mention       "Luca mentioned you in #launch on Slack."
    three or more   "Slack messages from Luca, Sam, and 2 others."
    a group DM      "Nina and Omar are talking in your group chat on Slack."
                    "Nina wrote in your group chat with Omar on Slack."
    ...that names   "Omar asked you something in the group chat with Nina on Slack."
       you          ("mentioned you" without a question), said first

Only who, never what they wrote ("what are they saying?" reads it on request:
slack_catchup.py). How often ([slack] announce_*):

    per conversation  a DM, a group DM, or a channel's mentions are gathered until
                      quiet_s pass with nothing new there (at most max_wait_s after the
                      first), then said once
    one at a time     at most one Slack announcement every min_gap; what settles in
                      between waits and is merged into the next
    you're in it      a conversation you wrote in within skip_if_replied isn't said, and
                      what was waiting from it is dropped (you've seen it)
    a group DM        said once while it goes on; again only after GROUP_QUIET_S with
                      nothing in it. A message there that names or @-mentions you is said
                      after PULLED_IN_S, whatever the gap or that
    held              while a phone call with Scout is on or the room is mid-turn,
                      nothing is said; it's kept and said (merged) once that's over

It's said like the pulse: through Assistant.announce, which waits for the floor, and
only logged in the pulse's quiet hours. Each message is said at most once (scout-memory
hands each out once; the app also remembers what it took). The sentence carries the
messages' item ids (refs.Spoken), so "give me a link to that" opens it in Slack.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import memory_client, refs
from .freebusy import join
from .mac import ToolError
from .starlink import in_quiet_hours

log = logging.getLogger(__name__)

POLL_S = 5.0
QUIET_S = 45.0
MAX_WAIT_S = 180.0
MIN_GAP_S = 300.0
SKIP_IF_REPLIED_S = 300.0
GROUP_QUIET_S = 1800.0  # a group DM said once stays unsaid until it's been quiet this long
PULLED_IN_S = 10.0  # a group DM message that names you is said after this much quiet
REMEMBER = 500  # messages taken, kept to never say one twice

Take = Callable[[], list[dict[str, Any]]]


def _people(alerts: Sequence[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(a.get("who") or "Someone") for a in alerts))


def _group(alerts: Sequence[dict[str, Any]]) -> str:
    """One group DM, said as a conversation, never what was written: "Nina and Omar
    are talking in your group chat"; or, when it pulls you in, "Omar asked you something
    in the group chat with Nina"."""
    posters = _people(alerts)
    members = list(dict.fromkeys(w for a in alerts for w in a.get("with") or []))
    pulled = [a for a in alerts if a.get("you")]
    if pulled:
        who = _people(pulled)
        rest = [p for p in dict.fromkeys([*posters, *members]) if p not in who]
        did = "asked you something" if any(a.get("you") == "asked" for a in pulled) else "mentioned you"
        where = f"the group chat with {join(rest)}" if rest else "a group chat"
        return f"{join(who)} {did} in {where}"
    others = [m for m in members if m not in posters]
    if len(posters) > 1:
        return f"{join(posters)} are talking in your group chat" + (f" with {join(others)}" if others else "")
    return f"{posters[0]} wrote in " + (f"your group chat with {join(others)}" if others else "a group chat")


def sentence(alerts: Sequence[dict[str, Any]]) -> refs.Spoken:
    """Who messaged or mentioned you, never how often or what they wrote, the parts that
    pull you in first: "Omar asked you something in the group chat with Nina and Luca
    messaged you on Slack."; three people or more in DMs, only names: "Slack messages from
    Luca, Sam, and 2 others." A group DM is a conversation, not messages to you."""
    ids = [str(a["id"]) for a in alerts if a.get("id")]
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    mentions: OrderedDict[tuple[str, str], None] = OrderedDict()
    dms: list[dict[str, Any]] = []
    for a in alerts:
        if a.get("group") and not a.get("mention"):
            groups.setdefault(str(a.get("channel") or ""), []).append(a)
        elif a.get("mention"):
            mentions[(str(a.get("who") or "Someone"), str(a["mention"]))] = None
        else:
            dms.append(a)
    dm_people = _people(dms)
    if len(dm_people) >= 3 and not groups and not mentions:
        named = dm_people if len(dm_people) == 3 else [*dm_people[:2], f"{len(dm_people) - 2} others"]
        return refs.Spoken(f"Slack messages from {join(named)}.", ids)
    gs = sorted(groups.values(), key=lambda g: not any(a.get("you") for a in g))
    parts = [_group(g) for g in gs if any(a.get("you") for a in g)]
    parts += [f"{who} mentioned you in {where}" for who, where in mentions]
    if dm_people:
        named = dm_people if len(dm_people) <= 3 else [*dm_people[:2], f"{len(dm_people) - 2} others"]
        parts.append(f"{join(named)} messaged you")
    parts += [_group(g) for g in gs if not any(a.get("you") for a in g)]
    return refs.Spoken(f"{join(parts)} on Slack.", ids)


def on_a_call() -> bool:
    """A phone call with Scout is going on (scout-phone's state file)."""
    try:
        from .phone_service import active_calls

        return bool(active_calls())
    except Exception:  # no phone set up, an unreadable file: not on a call
        return False


@dataclass
class _Burst:
    first: float
    last: float
    alerts: list[dict[str, Any]] = field(default_factory=list)


class Announcer:
    def __init__(
        self,
        say: Callable[[str], Awaitable[None]],
        quiet_hours: str,
        take: Take = memory_client.slack_alerts,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
        held: Callable[[], bool] = lambda: False,
        quiet_s: float = QUIET_S,
        max_wait_s: float = MAX_WAIT_S,
        min_gap_s: float = MIN_GAP_S,
        skip_if_replied_s: float = SKIP_IF_REPLIED_S,
        group_quiet_s: float = GROUP_QUIET_S,
    ) -> None:
        self.say, self.quiet_hours, self.take, self.now, self.held = say, quiet_hours, take, now, held
        self.quiet_s, self.max_wait_s = quiet_s, max_wait_s
        self.min_gap_s, self.skip_if_replied_s = min_gap_s, skip_if_replied_s
        self.group_quiet_s = group_quiet_s
        self.group_said: set[str] = set()  # group DMs said, until they go quiet
        self.group_last: dict[str, float] = {}  # group DM -> its newest message
        self.bursts: OrderedDict[str, _Burst] = OrderedDict()  # by conversation, still coming
        self.ready: list[dict[str, Any]] = []  # settled, waiting for the gap (or a call to end)
        self.replied: dict[str, float] = {}  # conversation -> when you last wrote there
        self.said_at: float | None = None
        self.said: OrderedDict[str, None] = OrderedDict()

    @classmethod
    def from_config(
        cls,
        say: Callable[[str], Awaitable[None]],
        sc: Any,
        quiet_hours: str,
        held: Callable[[], bool] = lambda: False,
    ) -> Announcer:
        """[slack] announce_quiet_s, announce_max_wait_s, announce_min_gap_min and
        announce_skip_if_replied_min."""
        return cls(
            say,
            quiet_hours,
            held=held,
            quiet_s=float(sc.announce_quiet_s),
            max_wait_s=float(sc.announce_max_wait_s),
            min_gap_s=float(sc.announce_min_gap_min) * 60,
            skip_if_replied_s=float(sc.announce_skip_if_replied_min) * 60,
        )

    def _you_replied(self, channel: str, mono: float) -> None:
        self.replied[channel] = mono
        for c in [c for c, at in self.replied.items() if mono - at > self.skip_if_replied_s]:
            del self.replied[c]
        dropped = len(self.bursts.pop(channel, _Burst(0, 0)).alerts)
        kept = [a for a in self.ready if str(a.get("channel")) != channel]
        dropped += len(self.ready) - len(kept)
        self.ready = kept
        if dropped:
            log.info("slack alerts: you replied there; %d not said", dropped)

    def _taken(self, new: list[dict[str, Any]], mono: float) -> None:
        for a in new:
            channel = str(a.get("channel") or "")
            if a.get("replied"):
                self._you_replied(channel, mono)
                continue
            key = f"{channel}:{a.get('ts')}"
            if key in self.said:
                continue
            self.said[key] = None
            while len(self.said) > REMEMBER:
                self.said.popitem(last=False)
            at = self.replied.get(channel)
            if at is not None and mono - at < self.skip_if_replied_s:
                log.debug("slack alerts: you're talking there; not said")
                continue
            if a.get("group") and not a.get("mention"):
                last = self.group_last.get(channel)
                self.group_last[channel] = mono
                if last is not None and mono - last >= self.group_quiet_s:
                    self.group_said.discard(channel)  # quiet a while: a new conversation
                if channel in self.group_said and not a.get("you"):
                    log.debug("slack alerts: that group chat is still going; not said again")
                    continue
            burst = self.bursts.get(channel)
            if burst is None:
                burst = self.bursts[channel] = _Burst(mono, mono)
            burst.alerts.append(a)
            burst.last = mono

    async def step(self, mono: float | None = None) -> str | None:
        """Take what's new; say what has settled once the gap allows. Returns what was
        said (or would have been, in quiet hours)."""
        mono = time.monotonic() if mono is None else mono
        try:
            new = await asyncio.to_thread(self.take)
        except ToolError as exc:  # scout-memory is restarting: next time
            log.debug("slack alerts: none taken (%s)", exc)
            new = []
        self._taken(new, mono)
        for channel, b in list(self.bursts.items()):
            settle = min(self.quiet_s, PULLED_IN_S) if any(a.get("you") for a in b.alerts) else self.quiet_s
            if mono - b.last >= settle or mono - b.first >= self.max_wait_s:
                self.ready += self.bursts.pop(channel).alerts
        pulled_in = any(a.get("you") for a in self.ready)  # said now, whatever the gap
        if not self.ready or (
            not pulled_in and self.said_at is not None and mono - self.said_at < self.min_gap_s
        ):
            return None
        if self.held():  # a call, or the room mid-turn: kept for after
            return None
        self.group_said |= {
            str(a.get("channel")) for a in self.ready if a.get("group") and not a.get("mention")
        }
        text, self.ready = sentence(self.ready), []
        self.said_at = mono
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
