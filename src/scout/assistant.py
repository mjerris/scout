"""The conversation loop: utterances → gate → floor → room assistant or a
Claude session talking through the MCP server → speech."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import logging.handlers
import re
import time
import wave
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    addressed,
    behavior,
    briefing,
    corrections,
    extmcp,
    gate,
    index,
    kiwix,
    ledger,
    local_intents,
    mail_mac,
    mail_phone,
    mailindex,
    memory_client,
    memory,
    onboarding,
    phone_pairing,
    privacy,
    pulse,
    reference,
    refs,
    sessions,
    shared_tools,
    speech,
    starlink,
    taskfarm,
    tasks,
    trust,
    tier1,
)
from .asr import Transcriber
from .audio import Utterance, analyze
from .brain import Brain, PhoneBrain, describe_tool, split_question
from .config import DATA, Config
from .config import load as load_config
from .floor import Floor
from .mac import ToolError
from .progress import STILL_WORKING, progress_phrase
from .pronounce import Pronouncer
from .rules import Rules
from .tools import SERVER, Timers, build_server
from .tts import Speaker

log = logging.getLogger(__name__)

BARGE_WAIT_S = 12.0  # a ducked reply waits this long for the words before resuming
BARGE_MATCH_S = 1.0  # an utterance starting this close to the onset is the one that barged
# The rest of a request a pause split off starts this soon after the first part ended;
# later speech is not joined to it (heard live: three utterances of someone else's
# conversation glued onto one request, each starting up to 6 s after the last).
SPLIT_GAP_S = 2.5

# Requests whose answer needs the calendar or the inbox: prefetched into the context.
_CALENDARISH = re.compile(
    r"\b(calendar|schedule|agenda|meetings?|appointments?|events?|busy|free at|free on|free this)\b"
)
_MAILISH = re.compile(r"\b(e-?mails?|inbox|mail|messages? from)\b")
ROOM = "room"
PHONE = "phone"  # the agent name scout-phone uses for its calls
ASK_DEADLINE_S = (
    7.0  # an ask from the phone answers within this, or says it's still working (SignalWire waits ~10 s)
)
ASK_KEEP_S = 600.0  # a late answer waits this long for the phone to ask again
ASK_STILL_WORKING = "I'm still working on that; ask me again in a moment."
# Scout's own tools a phone request may use through Claude (ask_scout): the shared tools
# that never ask, but not mail (the phone agent sends and drafts it itself, by
# mail_phone's rules), recall and remember, and reading the timers.
PHONE_TOOLS = {
    f"mcp__{SERVER}__{n}"
    for n in (
        *(t.name for t in shared_tools.SHARED if not t.asks and t.name not in ("mail_send", "mail_draft")),
        "recall",
        "remember",
        "list_timers",
    )
}
# Never from the phone: starting a call on the Mac (nobody is there to take it; call_link
# puts the number on the phone). Email by phone has its own rules (mail_phone.py).
NOT_BY_PHONE = ("call_start",)
PHONE_AGENT = "phone"  # phone_service.AGENT: its calls are journaled whole when they end
# Siri on a paired phone (web.py, /api/phone/ask; docs/siri-design.md). What Siri hears and
# says passes through Apple, so in strict privacy mode an answer from private data is held back.
SIRI_AGENT = "siri"
SIRI_WITHHELD = "That answer comes from your private records, so I won't say it through Siri. Ask me at home."
# An answer built from these is private: mail and messages, and what's indexed from them.
_PRIVATE_TOOL = re.compile(
    r"^(?:mail|messages?|contacts?|call)_|^(?:local_search|task_why|tasks_list|github_items|session_ask|session_replies)$"
)
PREFETCH_DEADLINE_S = 4.0  # the context prefetch gives up after this; the fetch finishes in the background
# A Claude turn says its progress line ("One moment.") no sooner than this after the turn
# began, unless a tool call has started: a quick answer would otherwise wait behind it.
CLAUDE_PROGRESS_S = 3.0
PAIR_APPROVE_S = 120.0  # a phone asking to pair waits this long for a yes
SHARED_FLOOR_WAIT_S = 60.0  # a session's tool question waits this long for the floor


def _retrieve(task: asyncio.Task[Any]) -> None:
    """Read a background prefetch's error, so asyncio doesn't warn about it."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.debug("context prefetch failed: %s", exc)


def _transcript_log() -> logging.Logger:
    """logs/transcript.jsonl, one event per line, rotated at midnight (kept 30 days)."""
    tx = logging.getLogger("scout.transcript")
    if not tx.handlers:
        (DATA / "logs").mkdir(parents=True, exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(
            DATA / "logs" / "transcript.jsonl", when="midnight", backupCount=30, encoding="utf-8"
        )
        h.setFormatter(logging.Formatter("%(message)s"))
        tx.addHandler(h)
        tx.setLevel(logging.INFO)
        tx.propagate = False  # not in the main log
    return tx


@dataclass
class _Heard:
    """How the utterance that started a turn was heard: for the per-turn timing line."""

    end_reason: str  # why the segmenter ended it ("turn", "pause", "max")
    turn_prob: float | None  # the end-of-turn model's last answer
    speech_end: float  # time.monotonic() when the clip ended
    asr_s: float  # how long the transcription itself took
    asr_done: float  # time.monotonic() when it was done


@dataclass
class _Context:
    """The request context, fetched while tier 1 decides. Mail waits for `go`: the
    tier 1 answer may read the inbox itself, and two listings at once slow both."""

    task: asyncio.Task[str]
    go: asyncio.Event


@dataclass
class _Listen:
    """A Claude session (via MCP) waiting for the user's spoken reply."""

    agent: str
    fut: asyncio.Future[Any]
    since: float  # only speech that starts after this counts
    parts: list[str]
    extend: bool = False  # "hang on" asked for more time


class Assistant:
    def __init__(
        self,
        cfg: Config,
        asr: Transcriber,
        speaker: Speaker,
        pronounce: Pronouncer | None = None,
        echo_cancelled: bool = False,
    ) -> None:
        self.cfg = cfg
        # With an echo-cancelling voice layer the mic no longer hears our own voice,
        # so talk-over is real speech (barge-in) and text-based echo stripping is off.
        self.echo_cancelled = echo_cancelled
        self.echo_stats: Callable[[], dict[str, Any]] = dict  # the voice layer's stats(); set by the app
        self.last_onset = 0.0  # when speech last began in the room (monotonic)
        # Scout calls you: alerts that ring the phone when the room is quiet ([phone] ring_for).
        from . import phone_ring

        self.ringer = phone_ring.Ringer.from_config(cfg.phone)
        self._message_barged: float | None = None
        self._confirm_barged: float | None = None  # onset of an answer said over the question
        self._barge_pending: float | None = None  # onset of speech over a reply, until its words are heard
        self._barge_strict = False  # that onset came before the canceller adapted: only a stop counts
        self._turn_text, self._turn_started, self._turn_spoke = "", 0.0, False
        # Per-turn timing: the utterance that started it, when its answer was first said,
        # which tier answered.
        self._heard: _Heard | None = None  # set while _handle routes an utterance
        self._turn_heard: _Heard | None = None
        self._turn_answer_at, self._turn_tier = 0.0, ""
        self._turn_claude = False  # the turn went to Claude (the progress line waits longer)
        self._tool_started = asyncio.Event()  # the current room turn called a tool
        self._context: _Context | None = None  # the context fetched alongside tier 1
        # The stopped turn's queued request still runs: a rejoined split request, or the
        # follow-up said over the turn's answer.
        self._requeued = False
        self.asr = asr
        self.speaker = speaker
        self.pronounce = pronounce or Pronouncer(DATA / "pronounce.txt")
        self.floor = Floor(cfg.floor.hold_seconds)
        self.rules = Rules(DATA / "state" / "voice_allow.json")
        self.timers = Timers(self._timer_done)
        sessions.configure(self._session_replied)  # a late reply to the room's ask is said here
        # What was said lately with the records it was about (refs.py): "give me a link to that".
        self.said = refs.use(refs.Log())
        self._local = local_intents.Context(
            self.timers, last_spoken=lambda: self.last_spoken, said=lambda: self.said, act=self._room_tool
        )
        # (time, request, answer, from private data): private ones reach Claude only if
        # the privacy mode allows.
        self.recent_local: deque[tuple[float, str, str, bool]] = deque(maxlen=10)
        self.privacy = privacy.configure(cfg.privacy.mode)
        # Memories and the local store/search live in the scout-memory service.
        self.memory = memory_client.memory()
        self.index = index.default()
        self.tier1: tier1.LocalModel | None = None  # loaded by start_tier1()
        # What Claude sessions said out loud (time, session, text, waited for a reply),
        # so the room knows who the user might be answering; and replies the room passed
        # on, delivered with that session's next discuss call.
        self.session_said: deque[tuple[float, str, str, bool]] = deque(maxlen=20)
        self.session_inbox: dict[str, list[tuple[float, str]]] = {}
        self.last_spoken: tuple[str, str] | None = None  # (who, text) for "say that again"
        self.last_private = False  # the last local answer came from private data (a repeat is too)
        self._turn_tool = ""  # the tool the current room turn is running, for the progress line
        self._phone_refs: list[str] = []  # ...and the phone's request's (asks run one at a time)
        self._turn_refs: list[str] = []  # records the room's Claude turn read about (its tools' results)
        server, names = build_server(
            self.timers, self.relay, self.memory, self._turn_refs.extend, cfg.claude.always_load_tools
        )
        # The user's own MCP servers (tools.toml): the room's session gets them by its rules.
        self.external = extmcp.External(cfg.claude.cwd, lambda: self.privacy.mode)
        self.kiwix: kiwix.Server | None = None  # the offline reference's server (run)
        self.brain = Brain(cfg.claude, self._confirm, server, names, self.rules, self._notify, self.external)
        self.utterances: asyncio.Queue[Utterance] = asyncio.Queue()
        self.state = "idle"  # idle | listening | thinking | speaking | confirming | agent
        self.mic_muted = False
        # Speech without the wake word counts until then. The window is "follow-up" after
        # the room answered a request (the speech must look addressed to Scout, by
        # [wake] follow_up) or "request" after a bare "Hey Scout" or a "hang on".
        self.follow_up_until = 0.0
        self._window = ""
        self._window_answer = ""  # what the room answered, for the follow-up check
        self._barged_turn: float | None = None  # onset of talk over the room's answer
        # The current turn was a follow-up without the wake word (it opens no window of
        # its own: no chains), or a split request asked whole (it joins nothing more).
        self._turn_followup = self._turn_joined = False
        self._next_followup = self._next_joined = False  # ...for the turn start_turn starts next
        self._queued_followup = self._queued_joined = False  # ...for the queued request
        self._turn: asyncio.Task[Any] | None = None
        self._silence_turn = False  # set by stop(): drain the turn without speaking
        self._queued: str | None = None  # a request to run when the current turn ends
        self._queued_out: dict[str, Any] = {"mini": True, "client": None}  # ...and where to answer it
        self._held_words: list[str] = []  # words before a "hang on"
        self._held_until = 0.0  # ...kept only while the wait window is open
        self._held_source: str | None = None  # who said them: a web client id, or None for the mic
        self._ref_db: float | None = None  # your voice level on accepted wake requests
        self._confirm_fut: asyncio.Future[Any] | None = None
        self._confirm_lock = asyncio.Lock()  # parallel tool calls ask one at a time
        self._confirm_gen = 0  # bumped by stop(): queued questions are dropped, not asked
        self._confirm_started = 0.0
        self._confirm_id = 0  # id of the pending question (web answers must name it)
        self._confirm_audience: dict[str, Any] = {"mini": True, "client": None}  # who was asked
        self._confirm_waiting = 0  # questions queued behind the one being asked
        self._confirm_later = False  # the pending question takes "later" as an answer (setup)
        self._stop_event = asyncio.Event()  # set (and replaced) by every stop()
        self._listen: _Listen | None = None
        self._discuss_stop: asyncio.Event | None = None  # set by stop() during a discuss call
        # Where the current room turn's speech goes: the mini's speakers and/or
        # the web page (by client id) that asked.
        self._out: dict[str, Any] = {"mini": True, "client": None}
        self.history: deque[dict[str, Any]] = deque(maxlen=300)
        # When the user talked over an answer (wall clock): the nightly pass reads the turn
        # after each as a correction (corrections.py). Kept only while the app runs.
        self.barge_ins: deque[float] = deque(maxlen=100)
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._saved: deque[Path] = deque()
        self._bg: set[asyncio.Task[Any]] = set()  # keeps fire-and-forget tasks alive until done
        # Requests from the phone (ask): one at a time, on their own Claude session.
        self._phone_brain: Brain | None = None
        self._server_brains: dict[
            str, PhoneBrain
        ] = {}  # by server: the phone's asks to the user's MCP servers
        self._ask_lock = asyncio.Lock()
        self._ask_jobs: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._ask_ready: dict[str, tuple[float, str, dict[str, Any]]] = {}  # late answers, by request

    # --- events: web page, transcript file ------------------------------------------

    def emit(self, kind: str, **data: Any) -> None:
        ev = {"type": kind, "ts": time.time(), **data}
        if kind not in ("state", "say"):
            self.history.append(ev)
            self._write_transcript(ev)
        for q in list(self._listeners):
            q.put_nowait(ev)

    def _write_transcript(self, ev: dict[str, Any]) -> None:
        _transcript_log().info(json.dumps(ev, default=str))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._listeners.discard(q)

    def _timer_done(self, label: str) -> None:
        self.emit("timer", text=f"{label} done")
        self._spawn(self._announce_timer(label))

    def _session_replied(self, ask: sessions.Ask) -> None:
        """A Claude session answered the room after session_ask stopped waiting: say it.
        A phone's or a desk session's ask is theirs to collect (session_replies)."""
        if ask.origin != ROOM:
            return
        text = speech.to_speech(f"{ask.to} replied: {ask.reply or ''}"[:600])
        self.emit("session_reply", text=text)
        self._spawn(self.announce(text, who="session"))

    async def _announce_timer(self, label: str) -> None:
        # Take the floor like any speaker, so it never talks over a session or a
        # room turn; after 30 minutes of waiting, announce anyway.
        took = await self.floor.acquire("timer", wait=1800)
        try:
            self.speaker.chime("wake")
            self.speaker.chime("wake")
            self.speaker.speak("Your timer is done." if label == "timer" else f"Your {label} timer is done.")
            await self.speaker.wait_idle()
        finally:
            if took:
                self.floor.release("timer")

    async def _announce_briefing(self) -> None:
        """The daily briefing (briefing.at), spoken like a timer: it waits for the floor."""
        c = self._local

        async def todo() -> str:
            rows = tasks.ordered(
                await asyncio.to_thread(tasks.all_tasks),
                c.now().date(),
                await asyncio.to_thread(tasks._people),
            )
            return briefing.tasks_part(rows, c.now().date())

        text = await briefing.build(c.now(), c.events, c.reminders, c.mail, c.hours(), todo)
        took = await self.floor.acquire("briefing", wait=1800)
        try:
            self.speaker.chime("wake")
            self.speaker.speak(text)
            self._spoke("briefing", text, refs.refs_of(text), refs.private_of(text))
            await self.speaker.wait_idle()
            room = {"mini": True, "client": None}
            await self._offer_rules(room)  # rules suggested overnight, one at a time
            if await asyncio.to_thread(self._review_due):
                said = await self._review_rules(room, monthly=True)
                if said:
                    self.speaker.speak(said)
                    await self.speaker.wait_idle()
        finally:
            if took:
                self.floor.release("briefing")

    async def _record_turn(
        self,
        request: str,
        reply: str,
        who: str,
        private: bool,
        tools: Sequence[tuple[str, dict[str, Any]]] = (),
        about: Sequence[str] = (),
    ) -> None:
        """Keep the exchange in the journal (memory/journal/, indexed), so "what did I ask
        about X" can find it. The tools line says what was used, not what it returned; the
        refs line (`about`) the records the answer was about."""
        private = private or any(self._private_tool(n) for n, _ in tools)
        try:
            # The store may be the memory service: off the event loop.
            await asyncio.to_thread(
                self._journal_add, time.time(), who, request, reply, tools, private, about
            )
        except (ToolError, OSError) as exc:  # the memory service is down: only this record is lost
            log.warning("recording the conversation failed: %s", exc)
        except Exception:
            log.exception("recording the conversation failed")

    def _journal_add(
        self,
        ts: float,
        who: str,
        q: str,
        a: str,
        tools: Sequence[tuple[str, dict[str, Any]]],
        private: bool,
        about: Sequence[str],
    ) -> None:
        ids = list(refs.clean(about))
        if ids and "refs" in inspect.signature(self.index.journal_add).parameters:
            self.index.journal_add(ts, who, q, a, tools, private, refs=ids)
        else:  # nothing to link, or a store from before refs
            self.index.journal_add(ts, who, q, a, tools, private)

    def _spoke(
        self,
        who: str,
        text: str,
        about: Sequence[str] = (),
        private: bool = False,
        request: str = "",
        channel: str = "room",
        session: bool = False,
    ) -> None:
        """Something was said (`who`: "Scout", "pulse", "briefing", or a Claude session
        when `session`): for "say that again" when the room heard it, and with the records
        it was about for "give me a link to that" (refs.Log)."""
        if channel == "room":
            self.last_spoken = (who if session else "Scout", text)
        self.said.add(refs.Said(time.time(), who, text, refs.clean(about), private, channel, request))

    async def announce(self, text: str, who: str = "announce") -> None:
        """Say something unprompted (Starlink went down): it waits for the floor, like a timer.
        When nobody has spoken in the room for a while, an alert [phone] ring_for names also
        rings the phone (phone_ring.py)."""
        if self.ringer is not None:
            from .slack_alerts import on_a_call

            quiet = time.monotonic() - self.last_onset
            self._spawn(self.ringer.consider(who, text, quiet, await asyncio.to_thread(on_a_call)))
        took = await self.floor.acquire(who, wait=300)
        try:
            self.speaker.chime("wake")
            self.speaker.speak(text)
            # The pulse's sentences carry the records they're about (refs.Spoken).
            self._spoke(who, text, refs.refs_of(text), refs.private_of(text))
            await self.speaker.wait_idle()
        finally:
            if took:
                self.floor.release(who)

    def say(self, text: str) -> None:
        """Speak where the current turn's replies go."""
        self._turn_spoke = True
        if not self._turn_answer_at:
            self._turn_answer_at = time.monotonic()
        if self._out["mini"]:
            self.speaker.speak(text)
        if self._out["client"]:
            self.emit("say", text=text, to=self._out["client"])

    def chime(self, kind: str) -> None:
        if self._out["mini"]:
            self.speaker.chime(kind)

    def _reply(self, text: str, speak: bool, client: str | None) -> None:
        """Answer the person who just spoke (mic: the mini; web: their page)."""
        if speak:
            self.speaker.speak(text)
        if client:
            self.emit("say", text=text, to=client)

    def _notify(self, kind: str, text: str) -> None:
        self.say(text)
        self.emit("rules", rules=self.rules.listing(), text=text)

    def remove_rule(self, rule_id: str) -> None:
        self.rules.remove(rule_id)
        self.emit("rules", rules=self.rules.listing())

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "mic_muted": self.mic_muted,
            "confirming": self._confirm_fut is not None,
            "confirm_id": self._confirm_id if self._confirm_fut is not None else None,
            "floor": self.floor.status(),
        }

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("state", **self.snapshot())

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    # --- main loop ---------------------------------------------------------------

    async def run(self) -> None:
        lib = reference.library()  # its archives, served by kiwix-serve (docs/reference.md)
        self.kiwix = kiwix.Server(lib.files, self.cfg.reference.port, on_change=lib.changed)
        self._spawn(self.kiwix.run())
        if self.external.catalog.servers or self.external.catalog.problems:
            self._spawn(self.external.watch())  # health, for scout status; reconnects failing ones
        if self.cfg.briefing.at:
            self._spawn(briefing.daily(self.cfg.briefing.at, self._announce_briefing))
        if self.cfg.behavior.nightly_at:  # rules from the day's corrections (corrections.py)
            self._spawn(briefing.daily(self.cfg.behavior.nightly_at, self._nightly_rules))

        def idle() -> bool:
            return self.state == "idle" and not self.busy

        def active() -> bool:  # speaking or listening this instant: no background pass starts
            return self.room_active()

        if self.cfg.pulse.enabled:
            p = self.cfg.pulse
            self._spawn(
                pulse.run(
                    lambda text: self.announce(text, "pulse"),
                    pulse.Pulse(p.event_lead_min, p.every_min, grace_min=self.cfg.sync.grace_min),
                    p.quiet_hours,
                )
            )
        if self.cfg.index.mail:
            self._spawn(mailindex.loop(idle, active=active))
        if self.cfg.tasks.farm:
            self._spawn(taskfarm.loop(idle, lambda: self.tier1, active=active))
        self._spawn(self._setup_watch(idle))  # a new install's walkthrough, new MCP servers (onboarding.py)
        if self.cfg.contacts.enabled:  # one person across mail, meetings and Slack (people.py)
            from . import people

            self._spawn(people.loop())
        if self.cfg.starlink.enabled:
            self._spawn(starlink.watch(lambda text: self.announce(text, "starlink")))
        sc = self.cfg.slack
        from . import slack

        if slack.announcing(sc):  # DMs and mentions said out loud (slack_alerts.py)
            from . import slack_alerts

            def held() -> bool:  # on the phone with Scout, or the room mid-turn
                return not idle() or slack_alerts.on_a_call()

            self._spawn(
                slack_alerts.Announcer.from_config(
                    lambda text: self.announce(text, "slack"), sc, self.cfg.pulse.quiet_hours, held
                ).run()
            )
        while True:
            utt = await self.utterances.get()
            if self.mic_muted:
                continue
            if waiting := self.utterances.qsize():  # clips are transcribed one at a time
                log.info("asr backlog: %d more clip%s waiting", waiting, "s" * (waiting != 1))
            try:
                await self._handle(utt)
            except Exception:
                log.exception("error handling utterance")

    def _reject(self, kind: str, text: str, reason: str | None) -> bool:
        """Log and surface a gate rejection. Returns True if rejected."""
        if reason is None:
            return False
        log.info("ignored %s (%s): %s", kind, reason, text)
        self.emit("ignored", text=text, reason=reason)
        return True

    def _save_utterance(self, utt: Utterance, t: gate.Transcript) -> None:
        keep = self.cfg.audio.save_utterances
        if keep <= 0:
            return
        folder = DATA / "state" / "utterances"
        folder.mkdir(parents=True, exist_ok=True)
        if not self._saved:  # first save this run: pick up what earlier runs left
            self._saved.extend(sorted(p.with_suffix("") for p in folder.glob("*.wav")))
        stem = folder / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with wave.open(str(stem.with_suffix(".wav")), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(utt.pcm)
        stem.with_suffix(".json").write_text(
            json.dumps(
                {"text": t.text, "echo": utt.echo, "transcript": t.__dict__, "audio": utt.stats.__dict__},
                indent=1,
            )
        )
        self._saved.append(stem)
        while len(self._saved) > keep:
            old = self._saved.popleft()
            for suffix in (".wav", ".json"):
                old.with_suffix(suffix).unlink(missing_ok=True)

    async def _handle(self, utt: Utterance) -> None:
        t0 = time.monotonic()
        t = await self.asr.transcribe(utt.pcm)
        done = time.monotonic()
        self._heard = _Heard(utt.end_reason, utt.turn_prob, utt.ended or t0, done - t0, done)
        try:
            await self._hear(utt, t)
        finally:
            self._heard = None

    async def _hear(self, utt: Utterance, t: gate.Transcript) -> None:
        t.text = self.pronounce.stt(t.text)
        self._save_utterance(utt, t)
        text = t.text
        if self._barge_pending is not None and utt.started >= self._barge_pending - BARGE_MATCH_S:
            words = await self._barge_check(utt, text)
            if words is None:
                return
            text = words
        if not text:
            return
        a = utt.stats
        log.info(
            "heard%s: %s  [conf %.2f, no-speech %.2f, %.1f words/s, %.0f dB over noise]",
            " (echo)" if utt.echo else "",
            text,
            t.avg_logprob,
            t.no_speech_prob,
            len(gate.norm(text).split()) / max(a.voiced_ms / 1000, 0.3),
            a.level_db - a.floor_db,
        )
        if gate.junk(text):
            return  # not speech at all; nothing to show
        if self._reject("clip", text, gate.coverage(text, a, self.cfg.gate)):
            return
        cmd = speech.strip_wake(
            text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
        )
        confirming = self._confirm_fut is not None and not self._confirm_fut.done()
        listening = self._listen is not None and not self._listen.fut.done()
        started = utt.started

        # Echo only if our speech actually overlapped the clip; a reply that merely
        # starts in the echo tail is the user's alone (and may reuse our words).
        # (utt.echo only says playback was on when the speech began.)
        over_us = utt.echo and self.speaker.overlap(utt.started, utt.ended or time.monotonic()) >= 0.3
        echo = not self.echo_cancelled and over_us
        if over_us and not echo and self._is_stop(text):
            # "Stop" said over our own speech needs no wake word, as in the echo case below
            # (heard live: a bare stop over a reply was dropped as "busy with another request").
            self.emit("heard", text=text)
            await self.stop()
            return
        if confirming and over_us and not echo and not self._is_stop(text):
            # Said over the read-back of a question (or a barge-in over it). The canceller
            # may not have taken all of our voice out, and the message may hold a "yes":
            # only a clear yes or no that isn't our own words is an answer.
            spoken = self.speaker.recent_speech(since=utt.started)
            residual = speech.strip_own_speech(text, spoken, self.pronounce.tts(spoken))
            if not residual or speech.parse_answer(residual) is None:
                self._reject("answer", text, "not a clear yes or no over the read-back")
                return
            text = residual
            cmd = speech.strip_wake(
                text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
            )
        if echo:
            # The mic heard the assistant too: "<wake> stop" always counts;
            # otherwise keep only the words that weren't ours.
            if self._named(text) and self._is_stop(text):
                self.emit("heard", text=text)
                await self.stop()
                return
            spoken = self.speaker.recent_speech(since=utt.started)
            residual = speech.strip_own_speech(text, spoken, self.pronounce.tts(spoken))
            if not residual or gate.junk(residual):
                return
            if self._is_stop(residual):  # "stop, stop" said over the reply
                self.emit("heard", text=residual)
                await self.stop()
                return
            # Leftovers are often misheard bits of our own speech: during an approval
            # only a clear yes/no counts; otherwise apply the follow-up rules.
            if confirming:
                if speech.parse_answer(residual) is None:
                    return
            elif not listening and self._reject(
                "residual", residual, gate.check("residual", residual, t, a, self._ref_db, self.cfg.gate)
            ):
                return
            log.info("heard after own speech: %s", residual)
            text = residual
            started = self.speaker.last_speech_end(utt.started) or utt.started
            cmd = speech.strip_wake(
                text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
            )
            if (
                not listening
                and self.busy
                and not confirming
                and not (cmd is not None and speech.is_stop(cmd))
            ):
                # Spoke over the end of the reply: with the wake word, run it once this
                # turn finishes. Without it we can't tell the user from someone else
                # in the room (the clip's stats are mixed with our own voice), so drop it.
                if cmd is None:
                    self._reject("request", text, "talked over the reply without the wake word")
                    return
                self.emit("heard", text=text)
                self._queued, self._queued_out = cmd, {"mini": True, "client": None}  # heard in the room
                return

        checked: dict[str, str | None] = {}

        def check(kind: str) -> str | None:
            # Clip stats describe a mixed clip when it held our voice; skip them then.
            if kind not in checked:
                checked[kind] = None if echo else gate.check(kind, text, t, a, self._ref_db, self.cfg.gate)
            return checked[kind]

        if not echo and cmd is not None and not confirming and not listening and check("wake") is None:
            self._ref_db = a.level_db if self._ref_db is None else 0.7 * self._ref_db + 0.3 * a.level_db
        await self._route(text, cmd, started, check, direct=False, ended=utt.ended or None)

    async def _route(
        self,
        text: str,
        cmd: str | None,
        started: float,
        check: Callable[[str], str | None],
        direct: bool,
        speak: bool = True,
        client: str | None = None,
        ended: float | None = None,
    ) -> str | None:
        """Where a heard utterance goes. Shared by the mic, web push-to-talk and
        typed text. `check(kind)` returns a gate rejection reason (None = passes);
        `direct` means the user addressed us explicitly (web), no wake word needed.
        Returns a reason when the utterance was ignored."""

        def reply(msg: str) -> None:
            self._reply(msg, speak and not self._floor_taken_by_session(), client)

        def ignore(reason: str) -> str:
            self._reject("request", text, reason)
            return reason

        confirming = self._confirm_fut is not None and not self._confirm_fut.done()
        listening = self._listen is not None and not self._listen.fut.done()
        in_follow_up = not self.busy and started <= self.follow_up_until
        via = {"via": "web"} if direct else {}

        # "Stop" ends whatever is going on: speech, a turn, a pending question, a
        # session's listen or floor hold, a "hang on".
        expecting = direct or confirming or listening or in_follow_up
        if (self._named(text) or expecting) and self._is_stop(text):
            self.emit("heard", text=text, **via)
            await self.stop()
            return None

        # "Claude, new conversation" is for the room even while a session listens.
        if cmd is not None and speech.is_reset(cmd) and listening:
            self.emit("heard", text=text, **via)
            await self.stop()
            self._spawn(self.reset(speak, client))
            return None

        # A Claude session is waiting for the user's reply: it gets the speech.
        if self._listen is not None and not self._listen.fut.done():
            if started < self._listen.since - 0.5:
                return ignore("said before the question")
            if self._reject("reply", text, check("reply")):
                return "gate"
            self.emit("heard", text=text, to=self._listen.agent, **via)
            self._listen_got(text, ended)
            return None

        # Another session has the floor (it's mid-sentence or between turns).
        owner = self.floor.owner()
        if owner not in (None, ROOM) and not confirming and cmd is None and not direct:
            return ignore(f"{owner} has the floor")
        # (A "Hey Scout" or web request goes ahead; its turn queues for the floor.)

        if confirming:
            aud = self._confirm_audience
            if not direct and not aud["mini"]:
                # Asked only on a web page: the room didn't hear the question.
                return ignore("the question was asked on another device")
            if self._reject("answer", text, check("confirm")):
                return "gate"
            # Web answers come from someone who can see the question already.
            if not direct and started < self._confirm_started - 0.5:
                return ignore("said before the question")
            self.emit("heard", text=text, **via)
            answer = speech.parse_answer(cmd if cmd else text)
            if self._confirm_later and speech.says_later(cmd if cmd else text):
                answer = onboarding.LATER  # setup's third answer: ask again another time
            if answer is None:
                # Nothing counts as an answer until the re-ask (which says "yes"
                # and "no" itself) has finished playing.
                self._confirm_started = float("inf")
                reply("Sorry, was that a yes or a no?")
                self._spawn(self._reopen_confirm())
            elif self._confirm_fut is not None:
                self._confirm_fut.set_result(answer)
                if not direct and getattr(self.speaker, "speaking", self.speaker.busy):
                    self.speaker.stop()  # answered over the read-back: the rest isn't needed
            return None

        if cmd is not None or direct:
            if self._reject("request", text, check("wake")):
                return "gate"
        elif in_follow_up:
            if self._reject("follow-up", text, check("followup")):
                return "gate"
            if self._window == "follow-up" and (why := self._unaddressed(text, self._window_answer)):
                return ignore(why)

        if self.busy:
            if cmd is not None or direct:
                # Run it next rather than dropping it.
                self.emit("heard", text=text, **via)
                self._queued = cmd if cmd is not None else text
                self._queued_out = {"mini": speak, "client": client}
                reply("Okay, I'll do that next.")
                return None
            in_room = speak == self._out.get("mini", True)
            barged = self._barged_turn
            if barged is not None and started >= barged - BARGE_MATCH_S and in_room:
                # Said over the answer to your request: the next request (the turn is
                # being stopped). No wake word, so it passes the follow-up checks.
                self._barged_turn = None
                if self._reject("follow-up", text, check("followup")):
                    return "gate"
                if why := self._unaddressed(text, self._window_answer):
                    return ignore(why)
                log.info("follow-up said over the answer: %s", text)
                self.emit("heard", text=text, **via)
                self._queued, self._queued_out = text, {"mini": speak, "client": client}
                self._requeued, self._queued_followup, self._queued_joined = True, True, False
                return None
            heard = self._turn_heard
            if (
                not self._turn_spoke
                and self._turn_text
                and heard is not None
                and not self._turn_joined
                and started - heard.speech_end <= SPLIT_GAP_S
                and in_room
            ):
                # The end of a request that a pause split off ("what time do those winds ..."
                # / "... forecast to start"): drop the half-answered turn, ask the whole thing.
                # No wake word, so it passes the same checks as a follow-up (not the TV, not
                # someone else's conversation), and a turn joins one split only.
                if self._reject("follow-up", text, check("followup")):
                    return "gate"
                whole = f"{self._turn_text.rstrip(' .…')} {text}".strip()
                if why := self._unaddressed(text, self._turn_text, rest_of=True):
                    return ignore(f"not the rest of the request: {why}")
                if self._turn_followup and (why := self._unaddressed(whole, self._window_answer)):
                    return ignore(why)
                log.info("rejoined a split request: %s", whole)
                self.emit("heard", text=text, **via)
                self._silence_turn, self._requeued = True, True
                self._queued_followup, self._queued_joined = self._turn_followup, True
                self._spawn(self.brain.interrupt())
                self._queued, self._queued_out = whole, {"mini": speak, "client": client}
                return None
            return ignore("busy with another request (say the wake word to queue it)")

        if cmd is None:
            if not direct and started > self.follow_up_until:
                return "no wake word"
            # A follow-up window or the web: no wake word needed. A follow-up's turn opens
            # no window of its own.
            self._next_followup = not direct and self._window == "follow-up"
            cmd = text
        self.emit("heard", text=text, **via)
        try:
            self._room_command(cmd, speak, client)
        finally:
            self._next_followup = False
        return None

    def _unaddressed(self, text: str, answer: str, rest_of: bool = False) -> str | None:
        """Why speech without the wake word isn't taken as a follow-up ([wake] follow_up
        "strict": it must look addressed to Scout), or None."""
        if self.cfg.wake.follow_up != "strict":
            return None
        w = self.cfg.wake
        return addressed.reason(text, answer, w.names, w.greeted_names, rest_of=rest_of)

    def _open_follow_up(self, start: float, answer: str) -> float:
        """After the room answered a request: listen for a follow-up without the wake
        word. Never after a follow-up's own answer (no chains), nor with follow_up "off".
        Returns how long the window is open."""
        seconds = self.cfg.wake.follow_up_seconds
        if self.cfg.wake.follow_up == "off" or self._turn_followup or seconds <= 0:
            return 0.0
        self.follow_up_until = start + seconds
        self._window, self._window_answer = "follow-up", answer
        return max(0.0, self.follow_up_until - time.monotonic())

    def _is_stop(self, text: str) -> bool:
        return speech.is_stop_utterance(
            text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
        )

    def _named(self, text: str) -> bool:
        """Does the utterance contain the wake name at all ("Stop, Claude")?"""
        wanted = {n.lower() for n in self.cfg.wake.names}
        return any(w in wanted for w in speech.words(speech.normalize(text)))

    def _floor_taken_by_session(self) -> bool:
        owner = self.floor.owner()
        return owner not in (None, ROOM, "timer")

    def _room_command(self, cmd: str, speak: bool = True, client: str | None = None) -> None:
        if speech.is_stop(cmd):  # before joining anything held by "hang on"
            self._held_words = []
            self._spawn(self.stop())
            return
        cmd, waiting = speech.split_wait(cmd)
        if self._held_words and (time.monotonic() > self._held_until or self._held_source != client):
            self._held_words = []  # expired, or someone else (another device) said them
        if waiting:
            # "…, hang on": keep what was said and keep listening.
            if cmd:
                self._held_words.append(cmd)
                self._held_source = client
            self._held_until = time.monotonic() + self.cfg.wake.wait_seconds + 5
            self._listen_for_more(self.cfg.wake.wait_seconds, speak)
            return
        if self._held_words:
            cmd = " ".join([*self._held_words, cmd]).strip()
            self._held_words = []
        if not cmd.strip():  # a bare "Hey Scout": chime and wait for the request
            self._listen_for_more(max(self.cfg.wake.follow_up_seconds, 6), speak)
            return
        if speech.is_reset(cmd):
            self._spawn(self.reset(speak, client))
            return
        self.start_turn(cmd, speak, client)

    def _listen_for_more(self, seconds: float, speak: bool) -> None:
        """Open a no-wake-word window, hold the floor for it, and show "listening"
        only while it's actually open."""
        if speak:
            self.speaker.chime("wake")
        self._set_state("listening")
        self.follow_up_until = time.monotonic() + seconds
        self._window = "request"  # the request itself: no follow-up check
        if self.floor.try_acquire(ROOM):  # so no session cuts in meanwhile
            self.floor.release(ROOM, hold=True, ttl=seconds)
        self._spawn(self._idle_after(self.follow_up_until))

    async def _idle_after(self, deadline: float) -> None:
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.05)
        if self.state == "listening" and time.monotonic() >= self.follow_up_until:
            self._set_state("idle")

    # --- push-to-talk from the web page -------------------------------------------------

    async def _reopen_confirm(self) -> None:
        await self.speaker.wait_idle()
        if self._confirm_fut is not None and not self._confirm_fut.done():
            self._confirm_started = time.monotonic()

    async def submit_text(self, text: str, speak: bool = True, client: str | None = None) -> str | None:
        """A request typed on the web page: same routing as speech (stop, reset,
        approvals, a listening session), no gate. Returns why it was ignored, if it was."""
        cmd = speech.strip_wake(
            text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
        )
        return await self._route(
            text, cmd, time.monotonic(), lambda kind: None, direct=True, speak=speak, client=client
        )

    async def submit_audio(self, pcm: bytes, speak: bool = True, client: str | None = None) -> dict[str, Any]:
        """A clip recorded on the web page: no wake word needed, same gate and routing."""
        t = await self.asr.transcribe(pcm)
        t.text = self.pronounce.stt(t.text)
        a = analyze(pcm, self.cfg.audio.vad_aggressiveness)
        if not t.text or gate.junk(t.text):
            return {"text": t.text, "ignored": "no speech heard"}
        reason = gate.check("direct", t.text, t, a, None, self.cfg.gate)
        if self._reject("web", t.text, reason):
            return {"text": t.text, "ignored": reason}
        cmd = speech.strip_wake(
            t.text, self.cfg.wake.names, self.cfg.wake.max_position, self.cfg.wake.greeted_names
        )
        ignored = await self._route(
            t.text, cmd, time.monotonic(), lambda kind: None, direct=True, speak=speak, client=client
        )
        return {"text": t.text, **({"ignored": ignored} if ignored else {})}

    # --- Claude sessions talking through the MCP server ------------------------------------

    def _listen_got(self, text: str, ended: float | None = None) -> None:
        lst = self._listen
        if lst is None:
            return
        body, waiting = speech.split_wait(text)
        if waiting:
            if body:
                lst.parts.append(body)
            self.speaker.chime("wake")
            # Speech after the "hang on" clip ended counts, even if it began while
            # that clip was still being transcribed.
            lst.since = ended if ended is not None else time.monotonic()
            lst.extend = True
            return
        if speech.is_stop(text):
            lst.parts = []
            text = "stop"
        lst.parts.append(text)
        if not lst.fut.done():
            lst.fut.set_result(" ".join(lst.parts).strip())

    async def discuss(
        self,
        agent: str,
        message: str | None,
        listen: bool = True,
        timeout: float = 30.0,
        hold: bool = False,
        voice: str | None = None,
        wait_for_floor: float = 0.0,
    ) -> dict[str, Any]:
        """Speak `message` for a Claude session and (optionally) return the
        user's spoken reply, filtered by the same gate as the room assistant."""
        agent = (agent or "session").strip()[:80] or "session"
        if agent in (ROOM, "timer"):
            return {"status": "error", "error": "reserved agent name"}
        if voice and voice not in self.speaker.voices():
            return {"status": "error", "error": f"unknown voice {voice!r}; see voice_status for the list"}
        if self.mic_muted and listen:
            return {"status": "muted"}
        stop_ev = self._stop_event  # "stop" while queued for the floor cancels this call
        if not await self.floor.acquire(agent, wait_for_floor, cancel=stop_ev):
            if stop_ev.is_set():
                return {"status": "stopped", "text": ""}
            return {"status": "floor_busy", **self.floor.status()}
        stopped = self._discuss_stop = asyncio.Event()
        cancelled = False
        try:
            self._set_state("agent")
            relayed = self.take_relayed(agent)

            def result(**d: Any) -> dict[str, Any]:
                return {**d, "relayed": relayed} if relayed else d

            if message:
                self.emit("agent_said", agent=agent, text=message)
                self.session_said.append((time.time(), agent, message, listen))
                self._spoke(agent, message, session=True)
                self.speaker.speak(speech.to_speech(message), voice=voice)
                await self.speaker.wait_idle()
            if stopped.is_set():
                return result(status="stopped", text="")
            if not listen:
                # No reply wanted, so no window without the wake word either: a reply
                # starts "Hey Scout, ..." and the room hears it with this message in context.
                return result(status="ok", spoke=bool(message))
            fut = asyncio.get_running_loop().create_future()
            barged, self._message_barged = self._message_barged, None
            # If the user talked over the message, what they're saying is the reply.
            lst = self._listen = _Listen(agent, fut, (barged - 0.2) if barged else time.monotonic(), [])
            if barged is None:
                self.speaker.chime("wake")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    text = await asyncio.wait_for(asyncio.shield(fut), max(0.1, deadline - time.monotonic()))
                    break
                except TimeoutError:
                    if lst.extend:  # "hang on" pushed the deadline out
                        lst.extend = False
                        deadline = time.monotonic() + self.cfg.wake.wait_seconds
                        continue
                    parts = lst.parts
                    text = " ".join(parts).strip() if parts else None
                    break
            if text is None:
                return result(status="no_reply", text="")
            if speech.is_stop(text):
                return result(status="stopped", text=text)
            return result(status="ok", text=text)
        except asyncio.CancelledError:
            # The session gave up (Esc, exit, HTTP timeout): stop its speech too.
            cancelled = True
            if self.speaker.busy:
                self.speaker.stop()
            raise
        finally:
            self._listen = None
            self._discuss_stop = None
            self.floor.release(agent, hold=hold and not stopped.is_set() and not cancelled)
            self._set_state("thinking" if self.busy else "idle")

    def on_speech_onset(self, while_speaking: bool) -> None:
        """Called by the mic loop when speech starts. With an echo canceller,
        speech while we're talking is the user talking over us: stop."""
        self.last_onset = time.monotonic()
        if not (while_speaking and self.echo_cancelled and self.cfg.audio.barge_in):
            return
        # Words, not chimes: barging into the "heard you" chime or a working tick
        # cancelled the request that had just started (heard live).
        if not getattr(self.speaker, "speaking", self.speaker.busy) or self.mic_muted:
            return
        # Not a stop yet: on the TV our own voice leaks past the canceller loudly enough to
        # start "speech" (heard live, twice). Duck, and decide when the words are heard.
        if self._barge_pending is not None:
            return
        onset = time.monotonic()
        self._barge_pending = onset
        # Before the canceller has adapted, only a stop interrupts (on the TV it often never
        # got past 12 dB: most onsets over two weeks were held, a stop included). It doesn't
        # duck then: our own voice leaking through set it off three times in two restarts,
        # dropping the reply to 20% for 1-3 s (heard live as stuttering).
        self._barge_strict = not self.echo_settled()
        if self._barge_strict:
            log.info(
                "barge-in?: the echo canceller is still adapting (%s), so only a stop counts",
                self._echo_note(),
            )
        else:
            duck = getattr(self.speaker, "duck", None)
            if duck is not None:
                duck(True)
            log.info("barge-in?: playback lowered until the words are heard")
        asyncio.get_running_loop().call_later(BARGE_WAIT_S, self._barge_expire, onset)

    def _barge_expire(self, onset: float) -> None:
        """No words came of it (a cough, a door): back to full volume."""
        if self._barge_pending == onset:
            self._barge_resume("no words heard")

    def _barge_resume(self, why: str) -> None:
        self._barge_pending = None
        duck = getattr(self.speaker, "duck", None)
        if duck is not None:
            duck(False)
        log.info("barge-in cancelled (%s): back to full volume", why)

    async def _barge_check(self, utt: Utterance, text: str) -> str | None:
        """For the utterance that began over our reply: the user's words, after taking out
        our own (barging in for them), or None when it was only our voice (resume)."""
        onset = self._barge_pending or utt.started
        spoken = self.speaker.recent_speech(since=utt.started)
        alt = self.pronounce.tts(spoken)
        if not text or gate.junk(text) or speech.mostly_ours(text, spoken, alt):
            self._barge_resume(f"heard our own voice: {text!r}" if text else "no words heard")
            return None
        words = speech.strip_own_speech(text, spoken, alt) or text
        if self._barge_strict and not self._is_stop(words):
            self._barge_resume(f"not a stop while the echo canceller adapts: {words!r}")
            return text  # heard as it would have been without the barge-in
        self._barge_pending = None
        self.barge_ins.append(time.time())  # a correction, maybe (corrections.py)
        if self.state == "agent" and self._listen is not None:
            # The session's message finished and we're listening for the reply: just hear it.
            log.info("barge-in: the message had finished; taking this as the reply")
        else:
            await self._barge_in(onset)
        duck = getattr(self.speaker, "duck", None)
        if duck is not None:
            duck(False)
        return words

    def echo_settled(self) -> bool:
        """Has the echo canceller adapted enough that speech during playback is the user?"""
        st = self.echo_stats()
        if "erle_db" not in st:
            return True  # this backend doesn't measure it
        a = self.cfg.audio
        erle, learned = st.get("erle_db"), st.get("echo_learned_s") or 0.0
        return erle is not None and erle >= a.barge_in_min_erle_db and learned >= a.barge_in_min_learned_s

    def _echo_note(self) -> str:
        st = self.echo_stats()
        return f"{st.get('erle_db')} dB removed after {st.get('echo_learned_s')} s of playback"

    async def _barge_in(self, onset: float | None = None) -> None:
        onset = onset or time.monotonic()
        log.info("barge-in: stopping playback")
        self.emit("barge")
        if self.state == "agent" and self._listen is None:
            # A session's message is playing: cut it short and listen for the reply.
            self._message_barged = onset
            self.speaker.stop()
            return
        if self.state == "confirming":
            # Talking over the question is the answer: stop the question, keep listening.
            # (A full stop answered "no" for the user: heard live, it denied a weather lookup.)
            self._confirm_barged = onset
            self.speaker.stop()
            return
        # Over the answer to a request heard in the room, what the user is saying now is a
        # follow-up: it needs no wake word. Over anything else (the pulse, a timer, the
        # briefing, a Slack alert) it is someone talking in the room: no window (heard
        # live: talk over the pulse right after a restart opened one, and the room's
        # conversation was answered).
        room_answer = self.busy and self._out["mini"] and not self._turn_followup
        # The answer may have finished while the words were heard: its window stays open.
        kept = (self.follow_up_until, self._window) if self.follow_up_until >= onset else None
        await self.stop()
        if room_answer and self._open_follow_up(onset, self._turn_said()) > 0:
            self._barged_turn = onset
        elif kept is not None:
            self.follow_up_until, self._window = kept

    def voice_status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "mic_muted": self.mic_muted,
            "floor": self.floor.status(),
            "room_busy": self.busy,
            "voices": self.speaker.voices(),
            "default_voice": self.speaker.voice,
            # The echo path measured for the speaker and mic in use (echo_cal.py).
            "echo_path": self.echo_stats().get("calibration"),
            # The offline reference's kiwix-serve: running, not installed, no archives...
            "reference": self.kiwix.status() if self.kiwix is not None else None,
        }

    # --- room turns ---------------------------------------------------------------------

    def start_turn(self, text: str, speak: bool = True, client: str | None = None) -> bool:
        if self.busy:
            return False
        self._turn_text, self._turn_started, self._turn_spoke = text, time.monotonic(), False
        self._turn_followup, self._turn_joined = self._next_followup, self._next_joined
        self._next_followup = self._next_joined = False
        self._barged_turn = None
        # Heard just now (not a queued request, typed text, or a "hang on" join).
        self._turn_heard = None if self._turn_joined else self._heard
        self._turn_answer_at, self._turn_tier = 0.0, ""
        self._turn_claude = False
        self._tool_started = asyncio.Event()
        self._turn = asyncio.create_task(self._run_turn(text, speak, client))
        return True

    async def _working_ticks(self) -> None:
        """A soft tick every few seconds while tools run and nothing is being said."""
        quiet_since = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            if self.speaker.busy or self.state != "thinking":
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > 3.0:
                self.speaker.chime("tick")
                quiet_since = time.monotonic()

    def _may_say_progress(self) -> bool:
        """Nothing said yet, not stopped, not asking a question, nothing playing."""
        return (
            not self._turn_spoke
            and not self._silence_turn
            and self.state == "thinking"
            and self._confirm_fut is None
            and not self.speaker.busy
        )

    async def _progress_speech(self, text: str) -> None:
        """A long lookup with nothing said yet: say what's happening, once (and once more
        if it drags on), instead of a long silence. Ticks carry on after that.

        A turn that went to Claude waits until CLAUDE_PROGRESS_S after it began unless a
        tool call started: a quick answer arriving just as the line was queued waited for it."""
        tts = self.cfg.tts
        await asyncio.sleep(tts.progress_delay_s)
        if self._turn_claude and not self._tool_started.is_set():
            left = self._turn_started + CLAUDE_PROGRESS_S - time.monotonic()
            if left > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._tool_started.wait(), left)
        if self._turn_spoke:
            return  # the answer is already being spoken
        if self._may_say_progress():
            said = progress_phrase(self._turn_tool or text)
            log.info("progress: %s", said)
            self._speak_progress(said)  # not the answer: leaves _turn_spoke and last_spoken alone
        if tts.progress_again_s <= 0:
            return
        await asyncio.sleep(tts.progress_again_s)
        if self._may_say_progress():
            log.info("progress: %s", STILL_WORKING)
            self._speak_progress(STILL_WORKING)

    def _speak_progress(self, text: str) -> None:
        """A progress line the speaker drops if the answer is queued before it plays."""
        getattr(self.speaker, "speak_progress", self.speaker.speak)(text)

    async def _answer_locally(self, text: str, fetch_context: bool = False) -> bool:
        """Tier 0: answer in plain code when the request clearly fits. True if handled.
        `fetch_context`: while tier 1 decides, start fetching Claude's request context
        (self._context), so a request tier 1 passes on doesn't wait for it afterwards."""
        setup = onboarding.is_request(text)  # "set me up": plain code, local_first or not
        if not self.cfg.claude.local_first and not setup:
            return False
        tier: int | None = 0  # set before the lookups: a ToolError below is still said
        private = False
        about: list[str] = []  # the records the answer is about (refs.py)
        try:
            if setup:
                said: str | None = await self._walkthrough(None, over=onboarding.from_the_start(text))
            else:
                said = await local_intents.answer(text, self._local)
                # Worked out from private data: a memory, an email's text, the title of a
                # task found in mail, a meeting or Slack (local_intents sets ctx.private).
                private, about = self._local.private, list(self._local.refs)
            if said is not None and not setup and local_intents.is_repeat(text):
                tier = None  # repeating isn't a new answer
                private = self.last_private  # ...and is as private as what it repeats
            if said is None and (said := await self._rule_request(text)) is not None:
                private, about = False, list(refs.refs_of(said))  # a rule's record (behavior:<id>)
            if said is None:
                # The memory may be the memory service: off the event loop, and while it's
                # down Claude takes the request.
                try:
                    said = await asyncio.to_thread(memory.answer, text, self.memory)
                except (ToolError, OSError) as exc:
                    log.warning("memory unavailable for a local answer: %s", exc)
                    said = None
                private = said is not None
                about = list(refs.refs_of(said))  # the facts it said (memory:<id>)
            if said is None and self.tier1 is not None:
                now = datetime.now().astimezone()
                # Claude's context is fetched meanwhile, in case tier 1 passes (~1 s).
                ctx = self._fetch_context(text) if fetch_context else None
                decision = await self.tier1.decide(text, now)
                # A recall may still pass after reading the records: keep fetching until it answers.
                maybe = decision is not None and decision["tool"] == "recall"
                if ctx is not None:
                    if decision is None or maybe:
                        ctx.go.set()
                    else:  # tier 1 answers: no context needed
                        ctx.task.cancel()
                        self._context = None
                if decision is not None:
                    said = await tier1.run(decision, now, self.privacy.summaries)
                    if said is not None:  # None: the lookup couldn't answer it after all (Claude)
                        tier, private = 1, tier1.is_private(decision)
                        about = list(decision.get("refs") or [])
                        self.emit("tool", name=f"local:{decision['tool']}", input=_preview(decision["args"]))
                        if ctx is not None and maybe:
                            ctx.task.cancel()
                            self._context = None
                if said is None and await reference.wanted(text, self.cfg.reference):
                    # Offline (by default): the reference archives on this Mac (reference.py).
                    said = await reference.answer(text, self.tier1)
                    if said is not None:
                        tier, private, about = 1, False, list(refs.refs_of(said))
                        self.emit("tool", name="local:reference", input=_preview({"question": text}))
                        if ctx is not None:
                            ctx.task.cancel()
                            self._context = None
        except ToolError as exc:  # e.g. calendar access not allowed yet: say why
            said = f"Sorry, {exc}"
        except Exception:  # never let the local tiers lose a request: Claude takes it
            log.exception("local answer failed; asking Claude")
            return False
        if said is None:
            return False
        log.info("answered locally (tier %s): %s -> %s", tier, text, said or "(done)")
        self.emit("local", text=said or "(done)")
        self.recent_local.append((time.time(), text, said or "(done)", private))
        self._turn_tier = "repeat" if tier is None else str(tier)
        if tier is not None:  # a repeat isn't a request of its own
            ledger.record(str(tier), time.monotonic() - self._turn_started if self._turn_started else 0.0)
        if self._silence_turn:
            log.info("stopped before the local answer was ready; not saying it")
        else:
            if said:
                said = speech.speak_dates(said)  # "09/30/2026" -> "September 30th"
                self._set_state("speaking")
                self.say(said)
                if tier is not None:  # a repeat keeps what it repeats, refs and all
                    self._spoke("Scout", said, about, private, text)
                    self.last_private = private
            if hint := mail_mac.take_setup_hint():  # a slow mail search: offer the one-time fix
                self.say(hint)
            await self.speaker.wait_idle()
        await self._record_turn(
            text, said or "(done)", "room", private, about=about if tier is not None else ()
        )
        return True

    async def _room_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Tier 0's "text Pat ..." and "call Pat" (local_intents.Context.act): a shared tool
        run the way the room's own Claude runs it. One that asks is asked about with the
        same question and trust log (Brain._ask: the read-back, never skipped by a rule or
        "always") and runs only on a yes."""
        t = shared_tools.BY_NAME[name]
        if t.asks:
            ok = await self.brain._ask(f"mcp__{SERVER}__{name}", args)
            if ok is None:
                return {"status": "no_answer"}
            if not ok:
                return {"status": "declined"}
        try:
            return {"status": "ok", "text": await t.run(args)}
        except ToolError as exc:
            return {"status": "error", "error": str(exc)}

    # --- behavior rules (behavior.py, corrections.py) ------------------------------------

    async def _rule_request(self, text: str, out: dict[str, Any] | None = None) -> str | None:
        """Tier 0 for behavior rules: "from now on ..." is read back and kept on a spoken
        yes; "what are my rules" lists them; "review my rules" reviews them. None for
        anything else."""
        if (rule := behavior.told(text)) is not None:
            answer = await self._confirm(
                behavior.read_back(rule),
                f"Keep this rule for how Scout behaves (memory/behavior.md):\n{rule}",
                out,
            )
            if answer is None:
                return ""  # "No answer, so I skipped that." was said
            if not answer:
                return "Okay, not a rule."
            b = behavior.default()
            try:
                r, new = await asyncio.to_thread(b.add, rule, "told")
            except ToolError as exc:
                return f"Sorry, {exc}."
            said = "Okay, I'll keep that rule." if new else "That was a rule already; I'll keep it."
            return refs.Spoken(said + b.over_cap_note(), [r.id])
        if behavior.is_listing(text):
            return await asyncio.to_thread(behavior.spoken_listing)
        if behavior.is_review(text):
            return await self._review_rules(out)
        return None

    def _review_due(self) -> bool:
        live = any(not r.enforced for r in behavior.default().rules())
        return live and corrections.Candidates().review_due(briefing.local_now().date())

    async def _offer_rules(self, out: dict[str, Any] | None = None) -> int:
        """Offer the rules the nightly pass suggested, one question each; kept only on a
        yes, never offered again after a no (a question nobody answered waits for the
        next briefing). Returns how many were kept."""
        cands = corrections.Candidates()
        b = behavior.default()
        kept = 0
        for c in (await asyncio.to_thread(cands.pending))[: corrections.MAX_CANDIDATES]:
            answer = await self._confirm(
                corrections.offer_text(c),
                f"Keep this rule for how Scout behaves?\n{c.rule}\n(from {', '.join(c.turns)})",
                out,
            )
            if answer is None:
                break
            if answer:
                await asyncio.to_thread(b.add, c.rule, "from corrections", "", c.turns)
                kept += 1
            await asyncio.to_thread(cands.resolve, c.id, bool(answer))
        if kept and (out or self._out).get("mini"):
            self.speaker.speak("Okay, I'll keep that." if kept == 1 else f"Okay, I'll keep those {kept}.")
        return kept

    async def _review_rules(self, out: dict[str, Any] | None = None, monthly: bool = False) -> str:
        """The upkeep review: rules that say the same thing merged, rules that never come
        up dropped, each only on a yes (three questions at most), suggestions offered;
        then what there is now, with the ones the code keeps."""
        b = behavior.default()
        root = b.path.parent
        today = briefing.local_now().date()
        talk = await asyncio.to_thread(corrections.recent_talk, root, today)
        rv = behavior.review(await asyncio.to_thread(b.rules), talk, today)
        merged = dropped = asked = 0

        def said(r: behavior.Rule) -> str:
            return behavior.spoken(r.text)

        for keep, dup in rv.duplicates:
            if asked >= 3:
                break
            asked += 1
            answer = await self._confirm(
                f"These two rules say the same thing: {said(keep)}; and {said(dup)}. Merge them?",
                f"Merge the behavior rules:\n{keep.text}\n{dup.text}\n(keeps the first)",
                out,
            )
            if answer is None:
                break
            if answer:
                await asyncio.to_thread(b.merge, keep.id, dup.id)
                merged += 1
        for r in rv.unused:
            if asked >= 3:
                break
            asked += 1
            answer = await self._confirm(
                f"{said(r)} hasn't come up in a month. Drop it?", f"Drop the behavior rule:\n{r.text}", out
            )
            if answer is None:
                break
            if answer:
                await asyncio.to_thread(b.drop, r.id)
                dropped += 1
            else:
                await asyncio.to_thread(b.confirm, [r.id])
        await self._offer_rules(out)
        await asyncio.to_thread(corrections.Candidates().reviewed, today)
        n = len(await asyncio.to_thread(b.rules))
        if monthly and not asked:
            return ""  # nothing to change: the monthly review says nothing
        done = [f"merged {merged}"] * bool(merged) + [f"dropped {dropped}"] * bool(dropped)
        head = f"Okay, {' and '.join(done)}. " if done else ("" if asked else "Your rules look fine. ")
        tail = f"You have {n} rule{'s' * (n != 1)}"
        if rv.enforced:
            e = len(rv.enforced)
            tail += f"; {e} {'is' if e == 1 else 'are'} kept by my code"
        return head + tail + "."

    async def _nightly_rules(self) -> None:
        """The nightly pass over yesterday's corrections, on the local model only."""
        if self.tier1 is None:
            log.info("rules: no local model loaded; the nightly pass waits for it")
            return
        day = briefing.local_now().date() - timedelta(days=1)
        await corrections.nightly(self.tier1, day, behavior.default().path.parent, list(self.barge_ins))

    # --- setup by voice (onboarding.py) ----------------------------------------------------

    def _setup_room(self, out: dict[str, Any] | None) -> onboarding.Walkthrough:
        """The walkthrough's questions and words: in the current turn (`out` None: "stop"
        ends it like any turn), or for the room alone (`out`: at a quiet moment)."""

        async def ask_yes(spoken: str, detail: str) -> bool | str | None:
            gen = self._confirm_gen
            got = await self._confirm(spoken, detail, out, later=True)
            return None if gen != self._confirm_gen else got  # "stop": not a no, stop here

        async def say(text: str) -> None:
            where = out or self._out
            if where.get("mini"):
                self.speaker.speak(text)
            if where.get("client"):
                self.emit("say", text=text, to=where["client"])
            await self.speaker.wait_idle()

        async def access(what: str) -> str:
            from . import calendar_mac, contacts_mac

            try:
                if what == "contacts":
                    await contacts_mac.ensure_access()
                else:
                    await calendar_mac.ensure_access(reminders=what == "reminders")
            except ToolError as exc:
                return str(exc)
            return ""

        return onboarding.Walkthrough(
            load_config(onboarding.CONFIG), ask_yes, lambda q: self._ask_text(q, out), say, access,
            config_path=onboarding.CONFIG, tools_path=extmcp.PATH, state_path=onboarding.STATE,
            claude_json=extmcp.CLAUDE_JSON,
        )  # fmt: skip

    async def _walkthrough(self, out: dict[str, Any] | None, over: bool = False) -> str:
        w = self._setup_room(out)
        said = await w.run(over)
        log.info("setup: walkthrough %s; %d changes", "finished" if said.startswith("That's") else "paused",
                 len(w.changed))  # fmt: skip
        return said

    async def _ask_text(self, question: str, out: dict[str, Any] | None, timeout: float = 20.0) -> str | None:
        """Ask and hear a spoken answer in the user's words (setup's "what should I call
        you"): None for no answer or "stop"."""
        where = out or self._out
        if where.get("mini"):
            self.speaker.speak(question)
        if where.get("client"):
            self.emit("say", text=question, to=where["client"])
        await self.speaker.wait_idle()
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._listen = _Listen("setup", fut, time.monotonic(), [])
        if where.get("mini"):
            self.speaker.chime("wake")
        try:
            text = str(await asyncio.wait_for(fut, timeout))
        except TimeoutError:
            return None
        finally:
            self._listen = None
        return None if not text or speech.is_stop(text) else text

    def room_active(self) -> bool:
        """Scout is speaking or listening right now: talking, a request or a reply being
        listened for, or a yes-or-no question open. An overdue background pass waits for a
        gap in this (syncstate.gap); Claude thinking isn't one. Speech Scout isn't listening
        for (a TV, people talking among themselves) doesn't count, or a pass could wait forever."""
        return (
            self.speaker.busy
            or self.state in ("listening", "speaking", "confirming")
            or (self._listen is not None and not self._listen.fut.done())
        )

    def _quiet_moment(self, idle: Callable[[], bool]) -> bool:
        """Nobody talking, nothing going on, not quiet hours: a moment to ask something."""
        return (
            idle()
            and self.floor.owner() is None
            and not self.mic_muted
            and not self.speaker.busy
            and not starlink.in_quiet_hours(self.cfg.pulse.quiet_hours, datetime.now())
        )

    async def _setup_watch(
        self, idle: Callable[[], bool], first_s: float = 120.0, every_s: float = 600.0
    ) -> None:
        """A new install's walkthrough, at the first quiet moment; then, every `every_s`,
        a new MCP server in Claude Code asked about once (onboarding.new_servers)."""
        await asyncio.sleep(first_s)
        try:
            st = await asyncio.to_thread(
                onboarding.startup_state, load_config(onboarding.CONFIG), onboarding.CONFIG, onboarding.STATE,
                extmcp.CLAUDE_JSON,
            )  # fmt: skip
        except Exception:
            log.exception("setup: no state; the setup watch is off")
            return
        room = {"mini": True, "client": None}
        while True:
            try:
                if self._quiet_moment(idle) and await self.floor.acquire("setup", wait=0):
                    try:
                        st = onboarding.read_state(onboarding.STATE) or st
                        if st.get("auto"):
                            st["auto"] = False
                            onboarding.save_state(st, onboarding.STATE)
                            said = await self._walkthrough(room)
                            await self._setup_room(room).say(said)
                        else:
                            await self._offer_new_servers(room, st)
                    finally:
                        self.floor.release("setup")
            except Exception:
                log.exception("setup: the watch failed this time")
            await asyncio.sleep(every_s)

    async def _offer_new_servers(self, out: dict[str, Any], st: dict[str, Any]) -> None:
        cfg = load_config(onboarding.CONFIG)
        names = await asyncio.to_thread(
            onboarding.new_servers, cfg, extmcp.PATH, st, time.time(), extmcp.CLAUDE_JSON
        )
        if not names:
            return
        w = self._setup_room(out)
        for n in names[:1]:  # one question a quiet moment
            st.setdefault("mcp_asked", {})[n] = time.time()
            got = await w.offer_server(n)
            if got != onboarding.LATER:
                st.setdefault("mcp", {})[n] = got
            onboarding.save_state(st, onboarding.STATE)
            log.info("setup: a new MCP server in Claude Code: %s", got)

    def relay(self, session: str, text: str) -> str:
        """The room passes the user's words to a Claude session that spoke to them."""
        known = {a for _, a, _, _ in self.session_said}
        target = session if session in known else next((a for a in known if a.split("#")[0] == session), None)
        if target is None:
            return f"No session called {session!r} has spoken recently: " + (
                ", ".join(sorted(known)) or "none"
            )
        self.session_inbox.setdefault(target, []).append((time.time(), text))
        log.info("relayed to %s: %s", target, text)
        return f"Passed to {target.split('#')[0]}; it gets it with its next voice call."

    def take_relayed(self, agent: str) -> list[str]:
        """Messages the room passed on for this session (cleared once delivered)."""
        return [t for _, t in self.session_inbox.pop(agent, [])]

    async def start_tier1(self) -> None:
        """Load the local model in the background; requests use Claude until it's ready."""
        name = self.cfg.claude.local_model
        # Wanted for tier 1, and for the email summaries Claude gets instead of email.
        if not name or not (self.cfg.claude.local_first or self.privacy.mode == "balanced"):
            return
        model = tier1.LocalModel(name, extra=self.cfg.claude.local_recall)
        model.recall_floor = self.cfg.claude.recall_min_similarity
        model.rewrite, model.said = self.cfg.claude.local_rewrite, self.said  # follow-ups (followup.py)
        hold = getattr(self.speaker, "hold", None)
        if hold is not None:
            hold(True)  # loading holds the GIL for seconds at a time: speech would stutter
        try:
            await model.load()
        except Exception as exc:
            log.warning("tier 1 model %s unavailable (%s); run scripts/fetch-models.sh", name, exc)
            return
        finally:
            if hold is not None:
                hold(False)
        self.tier1 = model
        self.privacy.summaries.model = model
        log.info("tier 1 ready: %s", name)

    def _fetch_context(self, text: str) -> _Context | None:
        """Start _with_context in the background (self._context); mail waits for `go`."""
        if not self.cfg.claude.request_context:
            return None
        go = asyncio.Event()
        task = asyncio.create_task(self._with_context(text, go))
        task.add_done_callback(_retrieve)
        self._context = _Context(task, go)
        return self._context

    async def _with_context(self, text: str, go: asyncio.Event | None = None) -> str:
        """The request with what Claude would otherwise spend a tool call on. The
        memories, the calendar and the mail are fetched at once; the mail only once `go`
        is set, when given."""
        if not self.cfg.claude.request_context:
            return text
        now = datetime.now().astimezone()
        lines = [f"Now: {now.strftime('%A, %B %-d, %Y, %-I:%M %p %Z')}."]
        if timers := self.timers.listing():
            lines.append("Timers running: " + "; ".join(timers) + ".")
        recent = [
            f"{q!r} -> {a!r}"
            if self.privacy.shares_private_text or not private
            else "(a request answered from private data; withheld in strict privacy mode)"
            for ts, q, a, private in self.recent_local
            if time.time() - ts < 600
        ]
        if recent:
            lines.append("Just answered locally: " + " | ".join(recent[-3:]))
        try:  # what was said with the records it was about: "give me a link to that"
            lines += await refs.context_lines(self.said, self.privacy.mode)
        except Exception as exc:  # the store is down: the request goes without them
            log.info("refs left out of the context: %s", exc)
        said = [s for s in self.session_said if time.time() - s[0] < 600]
        if said:
            lines.append(
                "Other Claude sessions said this aloud recently; the user may be answering or asking about "
                "one of them. If they're replying to a session, pass it on with pass_to_session and say so "
                "briefly; if they want it repeated, repeat it:"
            )
            for ts, agent, msg, waited in said[-4:]:
                when = datetime.fromtimestamp(ts).strftime("%-I:%M %p")
                note = "it waited for a reply" if waited else "it did not wait for a reply"
                lines.append(f"  [{when}, session {agent}, {note}] {msg}")

        async def memories() -> list[str]:
            try:
                # The memory service: off the event loop, and its outage only costs the memories.
                return await asyncio.to_thread(self.privacy.memories, self.memory, text)
            except Exception as exc:
                log.warning("memories unavailable, asking without them: %s", exc)
                return []

        async def calendar() -> str | None:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            listing = await self._prefetch(
                "the calendar",
                self.privacy.calendar_events(start.isoformat(), (start + timedelta(days=2)).isoformat()),
            )
            if listing is None:
                return None
            return (
                "Calendar, today and tomorrow (fetched just now; use it rather than calling the tool):\n"
                + listing
            )

        async def mail() -> str | None:
            if go is not None:
                await go.wait()
            listing = await self._prefetch(
                "mail", self.privacy.mail_list(5, budget_s=privacy.PREFETCH_BUDGET_S)
            )
            return None if listing is None else "Newest email (fetched just now):\n" + listing

        async def nothing() -> None:
            return None

        async def fetched(c: Coroutine[Any, Any, str | None]) -> str | None:
            try:
                return await c
            except ToolError as exc:
                log.info("context prefetch skipped: %s", exc)
                return None

        async def unsure() -> list[dict[str, str]]:
            """Same-name GitHub people the request names, for Claude to ask about (people.py)."""
            if not self.cfg.contacts.enabled:
                return []
            try:
                return await asyncio.to_thread(index.default().people_suggestions, text)
            except Exception as exc:  # the store is down, or older: none this time
                log.info("people suggestions left out of the context: %s", type(exc).__name__)
                return []

        low = text.lower()
        facts, cal, inbox, maybe = await asyncio.gather(
            memories(),
            fetched(calendar()) if _CALENDARISH.search(low) else nothing(),
            fetched(mail()) if _MAILISH.search(low) else nothing(),
            unsure(),
        )
        if facts:
            lines.append("The user told Scout (memories that may bear on this request):")
            lines += [f"  - {f} (ref {memory.fact_id(f)})" for f in facts]  # open_link takes the ref
        if maybe:
            lines.append(
                "Scout isn't sure these are the same person (the same name, nothing else shared). If it "
                "bears on the request, ask the user once, briefly; on a yes remember \"<name>'s GitHub is "
                '<login>", on a no remember "<name>\'s GitHub is not <login>":'
            )
            lines += [f"  - {m['ask']}" for m in maybe[:3]]
        lines += [part for part in (cal, inbox) if part is not None]
        return "[Context]\n" + "\n".join(lines) + "\n[/Context]\n" + text

    async def _prefetch(self, what: str, fetch: Coroutine[Any, Any, str]) -> str | None:
        """A context fetch, or None if it takes longer than PREFETCH_DEADLINE_S (a big
        inbox can take 15 s to list); it then finishes in the background."""
        task = self._spawn(fetch)
        task.add_done_callback(_retrieve)
        try:
            return await asyncio.wait_for(asyncio.shield(task), PREFETCH_DEADLINE_S)
        except TimeoutError:
            log.info("context prefetch of %s is slow; asking without it", what)
            return None

    async def _run_turn(self, text: str, speak: bool, client: str | None = None) -> None:
        self._out = {"mini": speak, "client": client}
        self._silence_turn = False
        self.follow_up_until = 0
        stop_ev = self._stop_event
        try:
            got = await self.floor.acquire(ROOM, self.cfg.floor.room_wait_seconds, cancel=stop_ev)
        except asyncio.CancelledError:
            self._out = {"mini": True, "client": None}
            raise
        if not got or self._silence_turn or stop_ev.is_set():
            if got:
                self.floor.release(ROOM)
            elif not stop_ev.is_set():
                owner = self.floor.owner()
                self.emit("error", text=f"{owner} has the floor; request dropped: {text}")
                # Tell the asker, but never over the session that has the floor.
                self._reply(
                    "Another session is using the voice right now. Try again in a moment.",
                    speak and not self._floor_taken_by_session(),
                    client,
                )
            self._queued = None
            self._queued_out = {"mini": True, "client": None}
            self._silence_turn = False  # it's over: later confirmations aren't this turn's
            self._out = {"mini": True, "client": None}
            self._set_state("idle")
            return
        self.emit("you", text=text)
        self._set_state("thinking")
        self._turn_tool = ""
        ticks: asyncio.Task[None] | None = None
        # Started before the local tiers: a weather or route lookup there can take seconds too.
        progress = (
            asyncio.create_task(self._progress_speech(text))
            if speak and self.cfg.tts.progress_speech
            else None
        )
        replies: list[str] = []
        used: list[tuple[str, dict[str, Any]]] = []
        self._turn_refs.clear()  # build_server's on_refs fills it from the tools' results
        self._context = None
        try:
            if await self._answer_locally(text, fetch_context=True):
                return
            self._turn_claude, self._turn_tier = True, "claude"
            ticks = (
                asyncio.create_task(self._working_ticks()) if speak and self.cfg.tts.working_sound else None
            )
            self.chime("ack")
            ctx, self._context = self._context, None
            if ctx is not None:
                ctx.go.set()
                request = await ctx.task
            else:
                request = await self._with_context(text)
            async for kind, data in self.brain.ask(request):
                if kind in ("speak", "text"):
                    if kind == "text":
                        self.emit("claude", text=data)
                        self.last_private = False
                    if not self._silence_turn:
                        said = speech.to_speech(data)
                        if said:
                            self._set_state("speaking")
                            self.say(said)
                    if kind == "text":
                        self.last_spoken = ("Scout", data)
                        replies.append(data)
                elif kind == "spoken":  # its sentences were spoken as they arrived
                    self.emit("claude", text=data)
                    self.last_spoken, self.last_private = ("Scout", data), False
                    replies.append(data)
                elif kind == "tool":
                    name, args = data
                    used.append((name, args if isinstance(args, dict) else {}))
                    self._turn_tool = name
                    self._tool_started.set()
                    self.emit("tool", name=name, input=_preview(args))
                    if self.state == "speaking" and not self.speaker.busy:
                        self._set_state("thinking")
                elif kind == "result":
                    took = time.monotonic() - self._turn_started if self._turn_started else 0.0
                    ledger.record("claude", took, **ledger.from_result(data))
                    self.emit(
                        "result",
                        cost=data.total_cost_usd,
                        turns=data.num_turns,
                        error=data.is_error,
                        session=data.session_id,
                    )
            await self.speaker.wait_idle()
        except Exception as exc:
            log.exception("Claude turn failed")
            self.emit("error", text=str(exc))
            self.chime("error")
            self.say("Sorry, something went wrong talking to Claude.")
            await self.brain.reset()
        finally:
            if ticks:
                ticks.cancel()
            if progress:
                progress.cancel()
            if self._context is not None:  # answered locally after all, or failed: not needed
                self._context.task.cancel()
                self._context = None
            self._log_turn_timing()
            if replies:
                about = list(refs.clean(self._turn_refs))
                private = any(self._private_tool(n) for n, _ in used)
                # last_spoken is already the reply; this is what it was about.
                self.said.add(
                    refs.Said(time.time(), "Scout", " ".join(replies), tuple(about), private, "room", text)
                )
                self._spawn(self._record_turn(text, " ".join(replies), "room", False, used, about))
            self._set_state("idle")
            # The follow-up window is for a reply the room heard; a phone-only turn opens
            # none, and nor does a follow-up's (_open_follow_up). A stopped turn leaves
            # alone the window a barge-in opened.
            follow = 0.0
            if self._out["mini"] and not self._silence_turn:
                follow = self._open_follow_up(time.monotonic(), " ".join(replies) or self._turn_said())
            # Keep the floor through the follow-up window so other sessions don't cut in.
            self.floor.release(ROOM, hold=follow > 0, ttl=follow)
            self._out = {"mini": True, "client": None}
            self._barged_turn = None
            queued, self._queued = self._queued, None
            out, self._queued_out = self._queued_out, {"mini": True, "client": None}
            requeued, self._requeued = self._requeued, False
            origin = (self._queued_followup, self._queued_joined)
            self._queued_followup = self._queued_joined = False
            if queued and (not self._silence_turn or requeued) and queued.strip():
                asyncio.get_running_loop().call_soon(self._start_queued, queued, out, *origin)
            self._silence_turn = False  # it's over: later confirmations aren't this turn's

    def _log_turn_timing(self) -> None:
        """One line of numbers per room turn (no words): how it was heard, how long the
        answer took, which tier gave it, and how long until its audio started."""

        def ms(seconds: float | None) -> str:
            return "-" if seconds is None else f"{seconds * 1000:.0f}"

        h, answer_at = self._turn_heard, self._turn_answer_at
        first_played = getattr(self.speaker, "first_played_since", None)
        audio_at = first_played(answer_at) if answer_at and first_played is not None else None
        log.info(
            "turn timing: end=%s p=%s speech_end_to_asr_done_ms=%s asr_ms=%s turn_to_answer_ms=%s "
            "tier=%s answer_to_audio_ms=%s",
            (h.end_reason or "-") if h else "-",
            "-" if h is None or h.turn_prob is None else f"{h.turn_prob:.2f}",
            ms(h.asr_done - h.speech_end) if h else "-",
            ms(h.asr_s) if h else "-",
            ms(answer_at - self._turn_started) if answer_at else "-",
            self._turn_tier or "-",
            ms(audio_at - answer_at) if audio_at is not None else "-",
        )

    def _turn_said(self) -> str:
        """What the room said last (a local answer), for the follow-up check."""
        return self.last_spoken[1] if self.last_spoken and self.last_spoken[0] == "Scout" else ""

    def _start_queued(
        self, text: str, out: dict[str, Any], followup: bool = False, joined: bool = False
    ) -> None:
        if speech.is_reset(text):
            self._spawn(self.reset(out["mini"], out["client"]))
        elif not speech.is_stop(text):
            self._next_followup, self._next_joined = followup, joined
            try:
                self._room_command(text, out["mini"], out["client"])
            finally:
                self._next_followup = self._next_joined = False

    async def _confirm(
        self,
        spoken: str,
        description: str,
        out: dict[str, Any] | None = None,
        timeout: float | None = None,
        later: bool = False,
        show: bool = True,
    ) -> bool | str | None:
        """True/False for a spoken or clicked answer, None if nobody answered.
        `out`: where to ask (default: where the current turn's replies go); `timeout`:
        how long to wait for the answer (default [claude] confirm_timeout_s); `later`:
        "later" is an answer too (onboarding.LATER; setup's questions only); `show`:
        False asks by voice only (the web UI shows the question its own way)."""
        gen = self._confirm_gen
        if self._confirm_waiting >= 5:  # a runaway fan-out of tool calls: don't queue forever
            return False
        self._confirm_waiting += 1
        try:
            async with self._confirm_lock:
                if gen != self._confirm_gen:
                    return False  # the user said stop while this one was waiting its turn
                self._confirm_later = later
                try:
                    return await self._confirm_one(spoken, description, out, timeout, show)
                finally:
                    self._confirm_later = False
        finally:
            self._confirm_waiting -= 1

    async def _confirm_one(
        self,
        spoken: str,
        description: str,
        out: dict[str, Any] | None = None,
        timeout: float | None = None,
        show: bool = True,
    ) -> bool | str | None:
        if self._silence_turn and out is None:  # the room turn was stopped; don't ask about its tools
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._confirm_id += 1
        cid = self._confirm_id
        self._confirm_started = float("inf")  # no speech counts until the question has been asked
        out = dict(out or self._out)
        self._confirm_audience = out
        self._confirm_fut = fut
        ok: bool | str | None = False
        question, read_back = split_question(spoken)
        try:
            if show:
                self.emit("confirm", id=cid, text=description)
            self._set_state("confirming")
            if out["mini"]:
                self.speaker.speak(question)
            if out["client"]:
                self.emit("say", text=f"{question} {read_back}".strip(), to=out["client"])
            await self.speaker.wait_idle()
            if not fut.done():
                barged, self._confirm_barged = self._confirm_barged, None
                # An answer begun over the question counts from when it began.
                self._confirm_started = (barged - 0.2) if barged else time.monotonic()
                if read_back and out["mini"] and not barged:
                    # The action and who it's for have been said: an answer said over
                    # the read-back counts (_hear keeps only a clear yes or no that
                    # isn't our own words), and ends it.
                    self.speaker.speak(read_back)
                    await self.speaker.wait_idle()
                    self._confirm_barged = None
            try:
                ok = await asyncio.wait_for(fut, timeout or self.cfg.claude.confirm_timeout_s)
            except TimeoutError:
                ok = None
                if out["mini"]:
                    self.speaker.speak("No answer, so I skipped that.")
                if out["client"]:
                    self.emit("say", text="No answer, so I skipped that.", to=out["client"])
        finally:
            # Also on cancellation (the SDK withdrew the request): never leave a stale question.
            self._confirm_fut = None
            self._confirm_started = 0.0
            if show:
                self.emit("confirm_done", id=cid, approved=bool(ok), always=ok == "always")
            self._set_state("thinking" if self.busy else "idle")
        return ok

    async def run_shared_tool(
        self,
        agent: str,
        name: str,
        args: dict[str, Any],
        preview: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Run a mail or calendar tool for another Claude session (via the MCP server).
        Tools that send or add something are confirmed by voice in the room first,
        whatever that session's own permission settings say; "always" is just once.

        From the phone nobody in the room is asked: the phone agent confirms with the
        caller in two steps (phone_service.py). preview=True says what a tool would do
        without doing it; confirmed=True, only from the phone, runs one the caller said
        yes to. Email by phone has its own rules (mail_phone.py): drafts are saved with no
        window, and a send is to known people, short, and read back first. A call is never
        started on the Mac from the phone (call_link puts the number on the phone)."""
        if extmcp.split(name) and self.external.decision(name, "phone") is not None:
            return await self._run_external(agent, name, args, preview, confirmed)
        t = shared_tools.BY_NAME.get(name)
        if t is None:
            return {"status": "error", "error": f"unknown tool {name!r}"}
        if preview and agent == PHONE and name == "mail_send":  # the read-back, by its rules
            return await mail_phone.preview(args)
        if preview:  # the read-back: exactly what would be done
            spoken, _ = describe_tool(name, args, full=True)
            try:
                if name == "session_ask":  # the session it will reach, by its name and folder
                    spoken = await sessions.preview(args)
                elif name == "rule_drop":  # the rules it would drop
                    which = [
                        r for r in behavior.default().matching(str(args.get("about") or "")) if not r.enforced
                    ]
                    if not which:
                        return {"status": "error", "error": "no rule matches that"}
                    names = "; ".join(
                        behavior.spoken(r.text)[:1].lower() + behavior.spoken(r.text)[1:] for r in which[:3]
                    )
                    spoken = f"Drop the rule{'s' * (len(which) != 1)} {names}?"
                elif name == "forget":  # the facts it would drop, unless strict privacy hides them
                    gone = await asyncio.to_thread(self.memory.matching, str(args.get("about") or ""))
                    if not gone:
                        return {"status": "error", "error": "nothing remembered matches that"}
                    shown = "; ".join(gone[:5]) if self.privacy.shares_private_text else f"{len(gone)} facts"
                    spoken = f"Forget {shown}?"
            except ToolError as exc:
                return {"status": "error", "error": str(exc)}
            if name == "note_delete":
                try:
                    nid = int(args.get("id") or 0)
                except (TypeError, ValueError):
                    return {"status": "error", "error": "id must be a note number from notes_find"}
                h = await asyncio.to_thread(index.default().get, f"note:{nid}")
                if h is None:
                    return {"status": "error", "error": f"there's no note {nid}"}
                spoken = f"Delete note {nid}, {h.title}?"
            return {"status": "ok", "text": spoken}
        typed = False
        if "approved" in args:  # the user's approval typed in that session's chat
            if name not in shared_tools.TYPED_TOOLS:
                return {
                    "status": "error",
                    "error": f"approved is only for {', '.join(shared_tools.TYPED_TOOLS)}; leave it out",
                }
            if args["approved"] != "typed":
                return {"status": "error", "error": 'approved can only be "typed"; leave it out to ask aloud'}
            if agent == PHONE:
                return {"status": "error", "error": "typed approval isn't for the phone"}
            args = {k: v for k, v in args.items() if k != "approved"}
            typed = self.cfg.confirm.typed
            if not typed:
                log.info(
                    "typed approval from %s for %s, but [confirm] typed is off: asking aloud", agent, name
                )
        if agent == PHONE and (name in NOT_BY_PHONE or (t.asks and not confirmed)):
            # Nobody in the room should be asked about a call they can't hear.
            return {"status": "error", "error": f"{name} can't be used from the phone"}
        if confirmed and agent != PHONE:
            return {"status": "error", "error": "confirmed is only for the phone agent"}
        tr = trust.default()
        if t.asks and confirmed:  # the caller said yes on the phone (two steps, a gate)
            log.info("confirmed on the phone: %s", name)
            tr.record(name, "phone", True, describe_tool(name, args)[1], by=agent)
        elif t.asks and typed:  # approved in the session's chat (config [confirm] typed)
            detail = describe_tool(name, args)[1]
            log.info(
                "approved by typing in %s's chat (no spoken question): %s", agent, detail.splitlines()[0]
            )
            tr.record(name, "typed", True, detail, by=agent)
        elif t.asks and tr.tier(name) == "auto":  # promoted by the user (trust.py)
            log.info("allowed by trust, for %s: %s", agent, name)
            tr.record(name, "auto", True, describe_tool(name, args)[1], by=agent)
        elif t.asks:
            spoken, detail = describe_tool(name, args)
            who = agent.split("#", 1)[0] or "a session"
            log.info("permission request from %s: %s", agent, detail.splitlines()[0])
            # Ask with the floor, like any speaker: otherwise the "yes" can go to a session
            # that is listening for the answer to its own question.
            stop_ev = self._stop_event
            held = self.floor.owner() == agent  # its hold between exchanges: give it back after
            if not await self.floor.acquire(agent, SHARED_FLOOR_WAIT_S, cancel=stop_ev):
                if stop_ev.is_set():
                    return {
                        "status": "stopped",
                        "error": "The user said stop, so this was not asked or done.",
                    }
                return {"status": "floor_busy", "error": "The voice was busy, so this was not asked or done."}
            try:
                answer = await self._confirm(
                    f"From {who}: {spoken}",
                    f"{detail}\n(requested by session {agent})",
                    {"mini": True, "client": None},
                )
                offer = tr.record(name, "asked", None if answer is None else bool(answer), detail, by=agent)
                if offer and answer and answer != "always":
                    self.speaker.speak(offer)
                if answer == "always":
                    self.speaker.speak(tr.promote(name))
            finally:
                self.floor.release(agent, hold=held)
            if answer is None:
                return {
                    "status": "no_answer",
                    "error": "The user did not answer the spoken confirmation, so this was not done.",
                }
            if not answer:
                return {"status": "declined", "error": "The user declined this by voice."}
        origin = sessions.ORIGIN.set(agent)  # session_ask: whose late reply it is
        run = mail_phone.BY_PHONE.get(name, t.run) if agent == PHONE else t.run
        try:
            said = shared_tools.recipient(name, args) if typed else ""  # before the draft is used up
            text = await run(args)
            if typed:  # no question was asked aloud: say, briefly, that it went
                owner = self.floor.owner()
                line = f"Calling {said}." if name == "call_start" else f"Sent to {said}."
                if owner in (None, agent):
                    self.speaker.speak(line)
                else:
                    log.info("not said (%s has the floor): %s", owner, line)
            return {"status": "ok", "text": text}
        except ToolError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:  # report it to the caller; don't take the app down
            log.exception("shared tool %s failed", name)
            return {"status": "error", "error": f"failed: {exc}"}
        finally:
            sessions.ORIGIN.reset(origin)

    async def _run_external(
        self, agent: str, name: str, args: dict[str, Any], preview: bool, confirmed: bool
    ) -> dict[str, Any]:
        """A write on one of the user's MCP servers that the caller said yes to on the
        phone (two steps: preview, then confirmed). Only the phone, only a tool its rules
        offer there and that asks; it runs on the host session (Claude Code's sign-in)."""
        d = self.external.decision(name, "phone", args)
        if agent != PHONE or d is None or not d.allowed or not d.asks:
            return {
                "status": "error",
                "error": d.why if d and not d.allowed else f"{name} can't be used here",
            }
        spoken, detail = self.external.describe(name, args, "phone")
        if preview:
            return {"status": "ok", "text": spoken}
        if not confirmed:
            return {"status": "error", "error": f"{name} can't be used from the phone without a yes"}
        server, tool = extmcp.split(name) or ("", name)
        log.info("confirmed on the phone: %s", name)
        trust.default().record(f"{server}.{tool}", "phone", True, detail, by=agent)
        try:
            text = await self.external.call(name, args)
        except ToolError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "text": speech.to_speech(extmcp.spoken(text)) or "Done."}

    async def approve_pairing(self, name: str, match: str) -> bool | None:
        """Ask the room whether a device asking to pair may (one channel of
        phone_pairing.PairRequests; the web UI, the paired phones, the Mac's alert and the
        CLI are asked at the same time, and this question is withdrawn when one of them
        answers first). The name is the requester's own choice, so it is cleaned and
        always said after a fixed prefix (phone_pairing.spoken_prompt), never raw. The
        device shows the same `match` number. True for yes, False for no, None if
        nobody answered here."""
        who = "pairing"
        if not await self.floor.acquire(who, SHARED_FLOOR_WAIT_S, cancel=self._stop_event):
            return None
        try:
            answer = await self._confirm(
                phone_pairing.spoken_prompt(name, match),
                "",
                {"mini": True, "client": None},
                timeout=PAIR_APPROVE_S,
                show=False,  # the web UI shows the pairing request itself, with its details
            )
        finally:
            self.floor.release(who)
        return None if answer is None else bool(answer)

    # --- requests from the phone ---------------------------------------------------------

    def _private_tool(self, name: str) -> bool:
        """An answer that used this tool came from private data (mail, texts, a private MCP server)."""
        return bool(_PRIVATE_TOOL.match(trust.base(name))) or self.external.is_private(name)

    async def ask(
        self, agent: str, text: str, deadline: float = ASK_DEADLINE_S, via: str = ""
    ) -> dict[str, Any]:
        """Answer `text` through Scout's tiers (local intents, memory, tier 1, Claude) and
        return the reply as text: nothing is spoken or chimed in the room, the floor is
        not taken, and a room turn in progress is left alone (Claude runs on a session of
        its own). Requests run one at a time. Past `deadline` the answer is kept, so asking
        again returns it; an answer nobody collected comes with the next reply.

        via: one of the user's MCP servers (tools.toml): answered by a Claude session with
        only that server's phone tools. A write it would make comes back as "action" (its
        tool, arguments and read-back) for the phone's two steps, not done."""
        text = " ".join(text.split())
        if not text:
            return {"status": "error", "error": "nothing was asked"}
        if via and not any(s.name == via for s in self.external.phone_servers()):
            return {"status": "error", "error": f"{via} isn't offered on the phone"}
        key = f"{via}\n{text.casefold()}" if via else text.casefold()
        now = time.monotonic()
        for k in [k for k, (at, _, _) in self._ask_ready.items() if now - at > ASK_KEEP_S]:
            del self._ask_ready[k]
        result: dict[str, Any] | None = None
        if key not in self._ask_ready:
            task = self._ask_jobs.get(key)
            if task is None:
                task = self._spawn(self._ask_job(agent, text, key, via))
                self._ask_jobs[key] = task
            try:
                result = await asyncio.wait_for(asyncio.shield(task), deadline)
            except TimeoutError:
                log.info("phone ask still running after %.0f s: %s", deadline, text)
                return {"status": "pending", "text": ASK_STILL_WORKING}
        kept = self._ask_ready.pop(key, None)
        result = kept[2] if kept is not None else result
        if result is None:  # can't happen: it finished just now, or was kept from earlier
            return {"status": "pending", "text": ASK_STILL_WORKING}
        earlier = [(t, r) for t, r in (v[1:] for v in self._ask_ready.values()) if r.get("text")]
        self._ask_ready.clear()
        if earlier and result.get("status") == "ok":
            also = " ".join(f"Also, about {t!r} you asked earlier: {r['text']}" for t, r in earlier)
            result = {**result, "text": f"{result['text']} {also}"}
        return result

    async def _ask_job(self, agent: str, text: str, key: str, via: str = "") -> dict[str, Any]:
        try:
            async with self._ask_lock:
                result = await (
                    self._answer_server(agent, via, text) if via else self._answer_text(agent, text)
                )
        except Exception as exc:
            log.exception("phone ask failed")
            result = {"status": "error", "error": f"failed: {exc}"}
        finally:
            self._ask_jobs.pop(key, None)
        self._ask_ready[key] = (time.monotonic(), text, result)
        return result

    async def _answer_text(self, agent: str, text: str) -> dict[str, Any]:
        t0 = time.monotonic()
        self.emit("you", text=text, client=agent)
        said, tier, private = await self._local_text(text, agent)
        if said is not None:
            said = speech.speak_dates(said or "Done.")
            if agent == SIRI_AGENT and private and self.privacy.mode == "strict":
                said = SIRI_WITHHELD
            log.info("phone: answered locally (tier %s): %s -> %s", tier, text, said)
            ledger.record(tier, time.monotonic() - t0, client=agent)
            self.emit("local", text=said, client=agent)
            about = self._phone_refs[:]
            self._spoke("Scout", said, about, private, text, channel=agent)
            if agent != PHONE_AGENT:  # the phone service keeps the whole call
                await self._record_turn(text, said, agent, private, about=about)
            return {"status": "ok", "text": said, "tier": tier}
        if self._phone_brain is None:
            # No relaying to sessions; the tools' records are collected for this request.
            server, names = build_server(
                self.timers,
                memory=self.memory,
                on_refs=self._phone_refs.extend,
                always_load=self.cfg.claude.always_load_tools,
            )
            self._phone_brain = PhoneBrain(self.cfg.claude, server, names, PHONE_TOOLS, self.rules)
        brain = self._phone_brain
        replies: list[str] = []
        used: list[str] = []
        self._phone_refs.clear()
        try:
            async for kind, data in brain.ask(await self._with_context(text)):
                if kind in ("text", "spoken"):
                    replies.append(data)
                elif kind == "tool":
                    name, args = data
                    used.append(name)
                    self.emit("tool", name=name, input=_preview(args), client=agent)
                elif kind == "result":
                    ledger.record("claude", time.monotonic() - t0, client=agent, **ledger.from_result(data))
        except Exception as exc:
            log.exception("phone: Claude failed")
            self.emit("error", text=f"phone request failed: {exc}")
            await brain.reset()
            return {
                "status": "ok",
                "text": "Sorry, something went wrong talking to Claude.",
                "tier": "claude",
            }
        reply = speech.to_speech(" ".join(replies)) or "Done."
        self.emit("claude", text=reply, client=agent)
        about = list(refs.clean(self._phone_refs))
        private = any(self._private_tool(n) for n in used)
        if agent == SIRI_AGENT and private and self.privacy.mode == "strict":
            reply = SIRI_WITHHELD
        self._spoke("Scout", reply, about, private, text, channel=agent)
        if agent != PHONE_AGENT:  # the phone service keeps the whole call
            self._spawn(self._record_turn(text, reply, agent, False, about=about))
        return {"status": "ok", "text": reply, "tier": "claude"}

    async def _answer_server(self, agent: str, server: str, text: str) -> dict[str, Any]:
        """A phone request for one of the user's MCP servers: a Claude session of its own
        with that server's phone tools only (no local tiers: they don't know the server)."""
        t0 = time.monotonic()
        self.emit("you", text=text, client=agent)
        brain = self._server_brains.get(server)
        if brain is None:
            tools, names = build_server(self.timers, memory=self.memory)
            brain = PhoneBrain(self.cfg.claude, tools, names, set(), self.rules, self.external, only=server)
            self._server_brains[server] = brain
        brain.prepared = None
        replies: list[str] = []
        try:
            async for kind, data in brain.ask(text):
                if kind in ("text", "spoken"):
                    replies.append(data)
                elif kind == "tool":
                    self.emit("tool", name=data[0], input=_preview(data[1]), client=agent)
                elif kind == "result":
                    ledger.record("claude", time.monotonic() - t0, client=agent, **ledger.from_result(data))
        except Exception as exc:
            log.exception("phone: %s request failed", server)
            self.emit("error", text=f"phone request to {server} failed: {exc}")
            await brain.reset()
            return {"status": "error", "error": f"{server} couldn't answer just now"}
        if brain.prepared is not None:
            name, args, said = brain.prepared
            self.emit("claude", text=f"(prepared for the caller's yes) {said}", client=agent)
            return {"status": "ok", "text": said, "action": {"name": name, "args": args, "said": said}}
        reply = speech.to_speech(extmcp.spoken(" ".join(replies))) or "Done."
        self.emit("claude", text=reply, client=agent)
        return {"status": "ok", "text": reply, "tier": "claude"}

    async def _local_text(self, text: str, agent: str = PHONE) -> tuple[str | None, str, bool]:
        """Tiers 0 and 1 for a phone request, without speaking: (answer or None, tier,
        from private data). Nothing that changes the room (timers, volume, playback). The
        records the answer is about are left in self._phone_refs."""
        self._phone_refs.clear()
        if not self.cfg.claude.local_first:
            return None, "", False
        try:
            if agent == PHONE and (rule := behavior.told(text)) is not None:
                # Nobody in the room is asked; the call keeps it in its two steps.
                return (
                    f"That's a rule for how Scout behaves: {rule}. To keep it, call rule_add with "
                    "that rule: read it back and keep it only after the user's yes.",
                    "0",
                    False,
                )
            said = await local_intents.answer(text, self._local, skip=local_intents.PHONE_SKIP, channel=agent)
            if said is not None:
                self._phone_refs.extend(self._local.refs)
                return said, "0", self._local.private
            try:
                said = await asyncio.to_thread(memory.answer, text, self.memory)
            except (ToolError, OSError) as exc:
                log.warning("memory unavailable for a phone answer: %s", exc)
                said = None
            if said is not None:
                self._phone_refs.extend(refs.refs_of(said))  # the facts it said
                return said, "0", True
            if self.tier1 is not None:
                now = datetime.now().astimezone()
                decision = await self.tier1.decide(text, now)
                if decision is not None:
                    said = await tier1.run(decision, now, self.privacy.summaries)
                    if said is not None:  # None: the lookup couldn't answer it after all (Claude)
                        self.emit("tool", name=f"local:{decision['tool']}", input=_preview(decision["args"]))
                        self._phone_refs.extend(decision.get("refs") or [])
                        return said, "1", tier1.is_private(decision)
        except ToolError as exc:
            return f"Sorry, {exc}", "0", False
        except Exception:  # the local tiers never lose a request: Claude takes it
            log.exception("phone: local answer failed; asking Claude")
        return None, "", False

    def answer_confirm(self, approved: bool | str, confirm_id: int | None = None) -> None:
        """Answer the pending question. With `confirm_id`, only if it's still that
        question (a late or double tap must not answer the next one)."""
        if confirm_id is not None and confirm_id != self._confirm_id:
            return
        if self._confirm_fut is not None and not self._confirm_fut.done():
            self._confirm_fut.set_result(approved)

    async def stop(self) -> None:
        """Stop talking, interrupt the current Claude turn, and end whatever is
        waiting: a question, a session's listen or floor hold, a queued call."""
        self.speaker.stop()
        self.follow_up_until = 0
        self._queued = None
        self._held_words = []
        self._confirm_gen += 1
        self._stop_event.set()  # cancels floor waits (room turns, queued discuss calls)
        self._stop_event = asyncio.Event()
        self.answer_confirm(False)
        if self._listen is not None and not self._listen.fut.done():
            self._listen.fut.set_result("stop")
        if self._discuss_stop is not None:
            self._discuss_stop.set()
        if self.busy:
            self._silence_turn = True
            # The SDK interrupt can take a while; don't hold up the next utterance.
            self._spawn(self.brain.interrupt())
        owner = self.floor.owner()
        if owner not in (None, "timer") and not self.floor.active and not (owner == ROOM and self.busy):
            self.floor.release(owner)  # a hold between exchanges ("hang on", follow-up, a session)
        if self.state == "listening":
            self._set_state("idle")
        self.emit("stopped")

    async def reset(self, speak: bool = True, client: str | None = None) -> None:
        await self.stop()
        turn = self._turn
        if turn is not None and not turn.done():
            try:
                await asyncio.wait_for(asyncio.shield(turn), 15)
            except Exception:
                turn.cancel()
        await self.brain.reset()
        self.emit("reset")
        self._reply(
            "Okay, starting a new conversation.", speak and not self._floor_taken_by_session(), client
        )

    async def shutdown(self) -> None:
        self._silence_turn = True
        self.speaker.stop()
        if self.kiwix is not None:
            await self.kiwix.stop()
        self.answer_confirm(False)
        if self._listen is not None and not self._listen.fut.done():
            self._listen.fut.set_result("stop")
        if self._turn is not None and not self._turn.done():
            await self.brain.interrupt()
            self._turn.cancel()
        await self.brain.close()
        if self._phone_brain is not None:
            await self._phone_brain.close()
        for b in self._server_brains.values():
            await b.close()
        await self.external.close()
        await sessions.close()

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        self.emit("state", **self.snapshot())


def _preview(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()}
