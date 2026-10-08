"""The conversation loop: utterances → wake/confirm/stop handling → Claude → speech."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from . import gate, speech
from .asr import Transcriber
from .audio import Utterance
from .brain import Brain
from .config import Config
from .tts import Speaker

log = logging.getLogger(__name__)


class Assistant:
    def __init__(self, cfg: Config, asr: Transcriber, speaker: Speaker):
        self.cfg = cfg
        self.asr = asr
        self.speaker = speaker
        self.brain = Brain(cfg.claude, self._confirm)
        self.utterances: asyncio.Queue[Utterance] = asyncio.Queue()
        self.state = "idle"  # idle | listening | thinking | speaking | confirming
        self.mic_muted = False
        self.follow_up_until = 0.0
        self._turn: asyncio.Task | None = None
        self._silence_turn = False  # set by stop(): drain the turn without speaking
        self._queued: str | None = None  # follow-up spoken over the end of a reply
        self._ref_db: float | None = None  # your voice level on accepted wake requests
        self._confirm_fut: asyncio.Future[bool] | None = None
        self._confirm_started = 0.0
        self.history: deque[dict] = deque(maxlen=300)
        self._listeners: set[asyncio.Queue[dict]] = set()
        # No initial prompt: Whisper spells "Claude" fine without one, and on
        # silence it tends to echo a prompt back as if it had been said.
        self._wake_prompt: str | None = None

    # --- events for the web UI -------------------------------------------------

    def emit(self, kind: str, **data: Any) -> None:
        ev = {"type": kind, "ts": time.time(), **data}
        if kind != "state":
            self.history.append(ev)
        for q in list(self._listeners):
            q.put_nowait(ev)

    def subscribe(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue()
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict]) -> None:
        self._listeners.discard(q)

    def snapshot(self) -> dict:
        return {"state": self.state, "mic_muted": self.mic_muted, "confirming": self._confirm_fut is not None}

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

    async def _handle(self, utt: Utterance) -> None:
        t = await self.asr.transcribe(utt.pcm, prompt=self._wake_prompt)
        text = t.text
        if not text:
            return
        a = utt.stats
        log.info("heard%s: %s  [conf %.2f, no-speech %.2f, %.1f words/s, %.0f dB over noise]",
                 " (echo)" if utt.echo else "", text, t.avg_logprob, t.no_speech_prob,
                 len(gate.norm(text).split()) / max(a.voiced_ms / 1000, 0.3), a.level_db - a.floor_db)
        if gate.junk(text):
            return  # not speech at all; nothing to show
        cmd = speech.strip_wake(text, self.cfg.wake.names, self.cfg.wake.max_position)
        confirming = self._confirm_fut is not None and not self._confirm_fut.done()

        # While the assistant is talking, the mic hears it too: an explicit
        # "<wake> stop" always counts; otherwise keep only what follows the
        # assistant's own words, if anything.
        if utt.echo:
            if cmd is not None and speech.is_stop(cmd):
                self.emit("heard", text=text)
                await self.stop()
                return
            residual = speech.strip_own_speech(text, self.speaker.recent_speech())
            if not residual:
                return
            # Leftovers are often misheard bits of our own speech: during an approval
            # only a clear yes/no counts; otherwise apply the follow-up rules.
            if confirming:
                if speech.parse_yes_no(residual) is None:
                    return
            elif self._reject("residual", residual, gate.check("residual", residual, t, a, self._ref_db, self.cfg.gate)):
                return
            log.info("heard after own speech: %s", residual)
            text, started = residual, time.monotonic()
            cmd = speech.strip_wake(text, self.cfg.wake.names, self.cfg.wake.max_position)
            if self.busy and not confirming and not (cmd is not None and speech.is_stop(cmd)):
                # Spoke over the end of the reply: run it once this turn finishes.
                self.emit("heard", text=text)
                self._queued = cmd if cmd is not None else text
                return
        else:
            started = utt.started
            if confirming:
                if self._reject("answer", text, gate.check("confirm", text, t, a, self._ref_db, self.cfg.gate)):
                    return
            elif cmd is not None:
                if self._reject("request", text, gate.check("wake", text, t, a, self._ref_db, self.cfg.gate)):
                    return
                self._ref_db = a.level_db if self._ref_db is None else 0.7 * self._ref_db + 0.3 * a.level_db
            elif not self.busy and started <= self.follow_up_until:
                if self._reject("follow-up", text, gate.check("followup", text, t, a, self._ref_db, self.cfg.gate)):
                    return

        if self._confirm_fut is not None and not self._confirm_fut.done():
            if started < self._confirm_started:
                return
            self.emit("heard", text=text)
            answer = speech.parse_yes_no(cmd if cmd is not None else text)
            if answer is None:
                self.speaker.speak("Sorry, was that a yes or a no?")
                self._confirm_started = time.monotonic()
            else:
                self._confirm_fut.set_result(answer)
            return

        if self.busy:
            if cmd is not None and speech.is_stop(cmd):
                self.emit("heard", text=text)
                await self.stop()
            elif cmd is not None:
                self.emit("heard", text=text)
                self.speaker.speak("I'm still working on the last request. Say stop to cancel it.")
            return

        if cmd is None:
            if started > self.follow_up_until:
                return
            cmd = text  # follow-up window: no wake word needed
        self.emit("heard", text=text)

        if not cmd.strip():
            self.speaker.chime("wake")
            self._set_state("listening")
            self.follow_up_until = time.monotonic() + max(self.cfg.wake.follow_up_seconds, 6)
            return
        if speech.is_stop(cmd):
            self.follow_up_until = 0
            self._set_state("idle")
            return
        if speech.is_reset(cmd):
            await self.reset()
            return
        self.start_turn(cmd)

    # --- turns ---------------------------------------------------------------------

    def start_turn(self, text: str, speak: bool = True) -> bool:
        if self.busy:
            return False
        self._turn = asyncio.create_task(self._run_turn(text, speak))
        return True

    async def _run_turn(self, text: str, speak: bool) -> None:
        self._silence_turn = False
        self.follow_up_until = 0
        self.emit("you", text=text)
        self._set_state("thinking")
        if speak:
            self.speaker.chime("ack")
        try:
            async for kind, data in self.brain.ask(text):
                if kind == "text":
                    self.emit("claude", text=data)
                    if speak and not self._silence_turn:
                        said = speech.to_speech(data)
                        if said:
                            self._set_state("speaking")
                            self.speaker.speak(said)
                elif kind == "tool":
                    name, args = data
                    self.emit("tool", name=name, input=_preview(args))
                elif kind == "result":
                    self.emit("result", cost=data.total_cost_usd, turns=data.num_turns,
                              error=data.is_error, session=data.session_id)
            await self.speaker.wait_idle()
        except Exception as exc:
            log.exception("Claude turn failed")
            self.emit("error", text=str(exc))
            if speak:
                self.speaker.chime("error")
                self.speaker.speak("Sorry, something went wrong talking to Claude.")
            await self.brain.reset()
        finally:
            self._set_state("idle")
            if not self._silence_turn:
                self.follow_up_until = time.monotonic() + self.cfg.wake.follow_up_seconds
            queued, self._queued = self._queued, None
            if queued and not self._silence_turn and queued.strip():
                asyncio.get_running_loop().call_soon(self._start_queued, queued)

    def _start_queued(self, text: str) -> None:
        if speech.is_reset(text):
            asyncio.create_task(self.reset())
        elif not speech.is_stop(text):
            self.start_turn(text)

    async def _confirm(self, spoken: str, description: str) -> bool | None:
        """True/False for a spoken or clicked answer, None if nobody answered."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._confirm_fut = fut
        self.emit("confirm", text=description)
        self._set_state("confirming")
        self.speaker.speak(spoken)
        await self.speaker.wait_idle()
        self._confirm_started = time.monotonic()
        ok: bool | None
        try:
            ok = await asyncio.wait_for(fut, self.cfg.claude.confirm_timeout_s)
        except TimeoutError:
            ok = None
            self.speaker.speak("No answer, so I skipped that.")
        finally:
            self._confirm_fut = None
        self.emit("confirm_done", approved=bool(ok))
        self._set_state("thinking")
        return ok

    def answer_confirm(self, approved: bool) -> None:
        if self._confirm_fut is not None and not self._confirm_fut.done():
            self._confirm_fut.set_result(approved)

    async def stop(self) -> None:
        """Stop talking and interrupt the current Claude turn."""
        self.speaker.stop()
        self.follow_up_until = 0
        self._queued = None
        self.answer_confirm(False)
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
        if self.busy:
            await self.brain.interrupt()
            self._turn.cancel()
        await self.brain.close()

    def set_mic_muted(self, muted: bool) -> None:
        self.mic_muted = muted
        self.emit("state", **self.snapshot())


def _preview(args: dict) -> dict:
    return {k: (v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()}
