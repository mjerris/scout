"""The conversation loop: utterances → gate → floor → room assistant or a
Claude session talking through the MCP server → speech."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import gate, speech
from .asr import Transcriber
from .audio import Utterance, analyze
from .brain import Brain
from .config import ROOT, Config
from .floor import Floor
from .pronounce import Pronouncer
from .rules import Rules
from .tools import Timers, build_server
from .tts import Speaker

log = logging.getLogger(__name__)
ROOM = "room"


def _transcript_log() -> logging.Logger:
    """logs/transcript.jsonl, one event per line, rotated at midnight (kept 30 days)."""
    tx = logging.getLogger("claude_voice.transcript")
    if not tx.handlers:
        (ROOT / "logs").mkdir(exist_ok=True)
        h = logging.handlers.TimedRotatingFileHandler(
            ROOT / "logs" / "transcript.jsonl", when="midnight", backupCount=30, encoding="utf-8"
        )
        h.setFormatter(logging.Formatter("%(message)s"))
        tx.addHandler(h)
        tx.setLevel(logging.INFO)
        tx.propagate = False  # not in the main log
    return tx


@dataclass
class _Listen:
    """A Claude session (via MCP) waiting for the user's spoken reply."""

    agent: str
    fut: asyncio.Future
    since: float  # only speech that starts after this counts
    parts: list[str]
    extend: bool = False  # "hang on" asked for more time


class Assistant:
    def __init__(self, cfg: Config, asr: Transcriber, speaker: Speaker, pronounce: Pronouncer | None = None):
        self.cfg = cfg
        self.asr = asr
        self.speaker = speaker
        self.pronounce = pronounce or Pronouncer(ROOT / "pronounce.txt")
        self.floor = Floor(cfg.floor.hold_seconds)
        self.rules = Rules(ROOT / "state" / "voice_allow.json")
        self.timers = Timers(self._timer_done)
        server, names = build_server(self.timers)
        self.brain = Brain(cfg.claude, self._confirm, server, names, self.rules, self._notify)
        self.utterances: asyncio.Queue[Utterance] = asyncio.Queue()
        self.state = "idle"  # idle | listening | thinking | speaking | confirming | agent
        self.mic_muted = False
        self.follow_up_until = 0.0
        self._turn: asyncio.Task | None = None
        self._silence_turn = False  # set by stop(): drain the turn without speaking
        self._queued: str | None = None  # follow-up spoken over the end of a reply
        self._held_words: list[str] = []  # words before a "hang on"
        self._held_until = 0.0  # ...kept only while the wait window is open
        self._ref_db: float | None = None  # your voice level on accepted wake requests
        self._confirm_fut: asyncio.Future | None = None
        self._confirm_lock = asyncio.Lock()  # parallel tool calls ask one at a time
        self._confirm_gen = 0  # bumped by stop(): queued questions are dropped, not asked
        self._confirm_started = 0.0
        self._listen: _Listen | None = None
        self._discuss_stop: asyncio.Event | None = None  # set by stop() during a discuss call
        # Where the current room turn's speech goes: the mini's speakers and/or
        # the web page (by client id) that asked.
        self._out: dict = {"mini": True, "client": None}
        self.history: deque[dict] = deque(maxlen=300)
        self._listeners: set[asyncio.Queue[dict]] = set()
        self._saved: deque = deque()
        self._bg: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive until done

    # --- events: web page, transcript file ------------------------------------------

    def emit(self, kind: str, **data: Any) -> None:
        ev = {"type": kind, "ts": time.time(), **data}
        if kind not in ("state", "say"):
            self.history.append(ev)
            self._write_transcript(ev)
        for q in list(self._listeners):
            q.put_nowait(ev)

    def _write_transcript(self, ev: dict) -> None:
        _transcript_log().info(json.dumps(ev, default=str))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._listeners.discard(q)

    def _timer_done(self, label: str) -> None:
        self.emit("timer", text=f"{label} done")
        self._spawn(self._announce_timer(label))

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

    def say(self, text: str) -> None:
        """Speak where the current turn's replies go."""
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

    def remove_rule(self, index: int) -> None:
        self.rules.remove(index)
        self.emit("rules", rules=self.rules.listing())

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "mic_muted": self.mic_muted,
            "confirming": self._confirm_fut is not None,
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
        while True:
            utt = await self.utterances.get()
            if self.mic_muted:
                continue
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
        folder = ROOT / "state" / "utterances"
        folder.mkdir(parents=True, exist_ok=True)
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
        t = await self.asr.transcribe(utt.pcm)
        t.text = self.pronounce.stt(t.text)
        self._save_utterance(utt, t)
        text = t.text
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
        cmd = speech.strip_wake(text, self.cfg.wake.names, self.cfg.wake.max_position)
        confirming = self._confirm_fut is not None and not self._confirm_fut.done()
        listening = self._listen is not None and not self._listen.fut.done()
        started = utt.started

        # Echo only if our speech actually overlapped the clip; a reply that merely
        # starts in the echo tail is the user's alone (and may reuse our words).
        echo = utt.echo and self.speaker.overlap(utt.started, utt.ended or time.monotonic()) >= 0.3
        if echo:
            # The mic heard the assistant too: "<wake> stop" always counts;
            # otherwise keep only the words that weren't ours.
            if cmd is not None and speech.is_stop(cmd):
                self.emit("heard", text=text)
                await self.stop()
                return
            residual = speech.strip_own_speech(text, self.speaker.recent_speech(since=utt.started))
            if not residual or gate.junk(residual):
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
            cmd = speech.strip_wake(text, self.cfg.wake.names, self.cfg.wake.max_position)
            if (
                not listening
                and self.busy
                and not confirming
                and not (cmd is not None and speech.is_stop(cmd))
            ):
                # Spoke over the end of the reply: run it once this turn finishes.
                self.emit("heard", text=text)
                self._queued = cmd if cmd is not None else text
                return

        checked: dict[str, str | None] = {}

        def check(kind: str) -> str | None:
            # Clip stats describe a mixed clip when it held our voice; skip them then.
            if kind not in checked:
                checked[kind] = None if echo else gate.check(kind, text, t, a, self._ref_db, self.cfg.gate)
            return checked[kind]

        if not echo and cmd is not None and not confirming and not listening and check("wake") is None:
            self._ref_db = a.level_db if self._ref_db is None else 0.7 * self._ref_db + 0.3 * a.level_db
        await self._route(text, cmd, started, check, direct=False)

    async def _route(
        self,
        text: str,
        cmd: str | None,
        started: float,
        check,
        direct: bool,
        speak: bool = True,
        client: str | None = None,
    ) -> str | None:
        reply = lambda msg: self._reply(msg, speak, client)  # noqa: E731
        """Where a heard utterance goes. Shared by the mic and web push-to-talk.
        `check(kind)` returns a gate rejection reason (None = passes); `direct`
        means the user addressed us explicitly (push-to-talk), no wake word needed.
        Returns a reason when the utterance was ignored."""
        confirming = self._confirm_fut is not None and not self._confirm_fut.done()
        via = {"via": "web"} if direct else {}

        listening = self._listen is not None and not self._listen.fut.done()
        # "Stop" ends whatever is going on: speech, a turn, a pending question, a session's listen.
        if speech.is_stop(cmd if cmd is not None else (text if direct or confirming or listening else "")):
            self.emit("heard", text=text, **via)
            await self.stop()
            return None

        # A Claude session is waiting for the user's reply: it gets the speech.
        if self._listen is not None and not self._listen.fut.done():
            if started < self._listen.since - 0.5:
                return "said before the question"
            if self._reject("reply", text, check("direct")):
                return "gate"
            self.emit("heard", text=text, to=self._listen.agent, **via)
            self._listen_got(text)
            return None

        # Another session has the floor (it's mid-sentence or between turns).
        owner = self.floor.owner()
        if owner not in (None, ROOM) and not confirming and cmd is None and not direct:
            self._reject("request", text, f"{owner} has the floor")
            return "floor"
        # (A "Hey Claude" or push-to-talk request goes ahead; its turn queues for the floor.)

        if confirming:
            if self._reject("answer", text, check("confirm")):
                return "gate"
            if started < self._confirm_started - 0.5:
                return "said before the question"
            self.emit("heard", text=text, **via)
            answer = speech.parse_answer(cmd if cmd is not None else text)
            if answer is None:
                # 3. Nothing counts as an answer until the re-ask (which says "yes"
                # and "no" itself) has finished playing.
                self._confirm_started = float("inf")
                reply("Sorry, was that a yes or a no?")
                self._spawn(self._reopen_confirm())
            else:
                self._confirm_fut.set_result(answer)
            return None

        if cmd is not None or direct:
            if self._reject("request", text, check("wake")):
                return "gate"
        elif (
            not self.busy
            and started <= self.follow_up_until
            and self._reject("follow-up", text, check("followup"))
        ):
            return "gate"

        if self.busy:
            if cmd is not None or direct:
                self.emit("heard", text=text, **via)
                reply("I'm still working on the last request. Say stop to cancel it.")
                return "busy with another request"
            return "busy"

        if cmd is None:
            if not direct and started > self.follow_up_until:
                return "no wake word"
            cmd = text  # follow-up window or push-to-talk: no wake word needed
        self.emit("heard", text=text, **via)
        self._room_command(cmd, speak, client)
        return None

    def _room_command(self, cmd: str, speak: bool = True, client: str | None = None) -> None:
        cmd, waiting = speech.split_wait(cmd)
        if waiting:
            # "…, hang on": keep what was said and keep listening.
            if self._held_words and time.monotonic() > self._held_until:
                self._held_words = []
            if cmd:
                self._held_words.append(cmd)
            self._held_until = time.monotonic() + self.cfg.wake.wait_seconds + 5
            if speak:
                self.speaker.chime("wake")
            self._set_state("listening")
            self.follow_up_until = time.monotonic() + self.cfg.wake.wait_seconds
            return
        if self._held_words:
            if time.monotonic() <= self._held_until:
                cmd = " ".join([*self._held_words, cmd]).strip()
            self._held_words = []
        if not cmd.strip():
            if speak:
                self.speaker.chime("wake")
            self._set_state("listening")
            self.follow_up_until = time.monotonic() + max(self.cfg.wake.follow_up_seconds, 6)
            return
        if speech.is_stop(cmd):
            self.follow_up_until = 0
            self._set_state("idle")
            return
        if speech.is_reset(cmd):
            self._spawn(self.reset())
            return
        self.start_turn(cmd, speak, client)

    # --- push-to-talk from the web page -------------------------------------------------

    async def _reopen_confirm(self) -> None:
        await self.speaker.wait_idle()
        if self._confirm_fut is not None and not self._confirm_fut.done():
            self._confirm_started = time.monotonic()

    async def submit_text(self, text: str, speak: bool = True, client: str | None = None) -> str | None:
        """A request typed on the web page: same routing as speech (stop, reset,
        approvals, a listening session), no gate. Returns why it was ignored, if it was."""
        cmd = speech.strip_wake(text, self.cfg.wake.names, self.cfg.wake.max_position)
        return await self._route(
            text, cmd, time.monotonic(), lambda kind: None, direct=True, speak=speak, client=client
        )

    async def submit_audio(self, pcm: bytes, speak: bool = True, client: str | None = None) -> dict:
        """A clip recorded on the web page: no wake word needed, same gate and routing."""
        t = await self.asr.transcribe(pcm)
        t.text = self.pronounce.stt(t.text)
        a = analyze(pcm)
        if not t.text or gate.junk(t.text):
            return {"text": t.text, "ignored": "no speech heard"}
        reason = gate.check("direct", t.text, t, a, None, self.cfg.gate)
        if self._reject("web", t.text, reason):
            return {"text": t.text, "ignored": reason}
        cmd = speech.strip_wake(t.text, self.cfg.wake.names, self.cfg.wake.max_position)
        ignored = await self._route(
            t.text, cmd, time.monotonic(), lambda kind: None, direct=True, speak=speak, client=client
        )
        return {"text": t.text, **({"ignored": ignored} if ignored else {})}

    # --- Claude sessions talking through the MCP server ------------------------------------

    def _listen_got(self, text: str) -> None:
        lst = self._listen
        body, waiting = speech.split_wait(text)
        if waiting:
            if body:
                lst.parts.append(body)
            self.speaker.chime("wake")
            lst.since = time.monotonic()
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
    ) -> dict:
        """Speak `message` for a Claude session and (optionally) return the
        user's spoken reply, filtered by the same gate as the room assistant."""
        agent = (agent or "session").strip()[:40] or "session"
        if agent in (ROOM, "timer"):
            return {"status": "error", "error": "reserved agent name"}
        if voice and voice not in self.speaker.voices():
            return {"status": "error", "error": f"unknown voice {voice!r}; see voice_status for the list"}
        if not await self.floor.acquire(agent, wait_for_floor):
            return {"status": "floor_busy", **self.floor.status()}
        prev_state = self.state
        stopped = self._discuss_stop = asyncio.Event()
        try:
            self._set_state("agent")
            if message:
                self.emit("agent_said", agent=agent, text=message)
                self.speaker.speak(speech.to_speech(message), voice=voice)
                await self.speaker.wait_idle()
            if stopped.is_set():
                return {"status": "stopped", "text": ""}
            if not listen:
                return {"status": "ok", "spoke": bool(message)}
            fut = asyncio.get_running_loop().create_future()
            lst = self._listen = _Listen(agent, fut, time.monotonic(), [])
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
                return {"status": "no_reply", "text": ""}
            if speech.is_stop(text):
                return {"status": "stopped", "text": text}
            return {"status": "ok", "text": text}
        finally:
            self._listen = None
            self._discuss_stop = None
            self.floor.release(agent, hold=hold and not stopped.is_set())
            self._set_state(prev_state if prev_state != "agent" else "idle")

    def voice_status(self) -> dict:
        return {
            "state": self.state,
            "mic_muted": self.mic_muted,
            "floor": self.floor.status(),
            "room_busy": self.busy,
            "voices": self.speaker.voices(),
            "default_voice": self.speaker.voice,
        }

    # --- room turns ---------------------------------------------------------------------

    def start_turn(self, text: str, speak: bool = True, client: str | None = None) -> bool:
        if self.busy:
            return False
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

    async def _run_turn(self, text: str, speak: bool, client: str | None = None) -> None:
        self._out = {"mini": speak, "client": client}
        self._silence_turn = False
        self.follow_up_until = 0
        got = await self.floor.acquire(ROOM, self.cfg.floor.room_wait_seconds)
        if got and self._silence_turn:  # "stop" while we waited for the floor
            self.floor.release(ROOM)
            self._out = {"mini": True, "client": None}
            self._set_state("idle")
            return
        if not got:
            owner = self.floor.owner()
            self.emit("error", text=f"{owner} has the floor; request dropped: {text}")
            self.say("Another session is using the voice right now. Try again in a moment.")
            self._out = {"mini": True, "client": None}
            return
        self.emit("you", text=text)
        self._set_state("thinking")
        ticks = asyncio.create_task(self._working_ticks()) if speak and self.cfg.tts.working_sound else None
        self.chime("ack")
        try:
            async for kind, data in self.brain.ask(text):
                if kind == "text":
                    self.emit("claude", text=data)
                    if not self._silence_turn:
                        said = speech.to_speech(data)
                        if said:
                            self._set_state("speaking")
                            self.say(said)
                elif kind == "tool":
                    name, args = data
                    self.emit("tool", name=name, input=_preview(args))
                    if self.state == "speaking" and not self.speaker.busy:
                        self._set_state("thinking")
                elif kind == "result":
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
            self._set_state("idle")
            follow = 0.0 if self._silence_turn else self.cfg.wake.follow_up_seconds
            self.follow_up_until = time.monotonic() + follow
            # Keep the floor through the follow-up window so other sessions don't cut in.
            self.floor.release(ROOM, hold=follow > 0, ttl=follow)
            self._out = {"mini": True, "client": None}
            queued, self._queued = self._queued, None
            if queued and not self._silence_turn and queued.strip():
                asyncio.get_running_loop().call_soon(self._start_queued, queued)

    def _start_queued(self, text: str) -> None:
        if speech.is_reset(text):
            self._spawn(self.reset())
        elif not speech.is_stop(text):
            self._room_command(text)

    async def _confirm(self, spoken: str, description: str) -> bool | str | None:
        """True/False for a spoken or clicked answer, None if nobody answered."""
        gen = self._confirm_gen
        async with self._confirm_lock:
            if gen != self._confirm_gen:
                return False  # the user said stop while this one was waiting its turn
            return await self._confirm_one(spoken, description)

    async def _confirm_one(self, spoken: str, description: str) -> bool | str | None:
        if self._silence_turn:  # the turn was stopped; don't ask about its tools
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._confirm_started = float("inf")  # no speech counts until the question has been asked
        self._confirm_fut = fut
        self.emit("confirm", text=description)
        self._set_state("confirming")
        self.say(spoken)
        await self.speaker.wait_idle()
        self._confirm_started = time.monotonic()
        ok: bool | str | None
        try:
            ok = await asyncio.wait_for(fut, self.cfg.claude.confirm_timeout_s)
        except TimeoutError:
            ok = None
            self.say("No answer, so I skipped that.")
        finally:
            self._confirm_fut = None
        self.emit("confirm_done", approved=bool(ok), always=ok == "always")
        self._set_state("thinking")
        return ok

    def answer_confirm(self, approved: bool | str) -> None:
        if self._confirm_fut is not None and not self._confirm_fut.done():
            self._confirm_fut.set_result(approved)

    async def stop(self) -> None:
        """Stop talking and interrupt the current Claude turn."""
        self.speaker.stop()
        self.follow_up_until = 0
        self._queued = None
        self._held_words = []
        self._confirm_gen += 1
        self.answer_confirm(False)
        if self._listen is not None and not self._listen.fut.done():
            self._listen.fut.set_result("stop")
        if self._discuss_stop is not None:
            self._discuss_stop.set()
        if self.busy:
            self._silence_turn = True
            await self.brain.interrupt()
        self.emit("stopped")

    async def reset(self) -> None:
        await self.stop()
        if self._turn is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._turn), 15)
            except Exception:
                self._turn.cancel()
        await self.brain.reset()
        self.emit("reset")
        self.speaker.speak("Okay, starting a new conversation.")

    async def shutdown(self) -> None:
        self._silence_turn = True
        self.speaker.stop()
        self.answer_confirm(False)
        if self._listen is not None and not self._listen.fut.done():
            self._listen.fut.set_result("stop")
        if self.busy:
            await self.brain.interrupt()
            self._turn.cancel()
        await self.brain.close()

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        self.emit("state", **self.snapshot())


def _preview(args: dict) -> dict:
    return {k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()}
