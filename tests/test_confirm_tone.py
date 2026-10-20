"""The confirm tone: once an action the user said yes to has gone through, one sound
([confirm] sound, macOS's Glass by default, played from where it is) says so instead
of a spoken "Sent to Pat Doe.". A no or an error is still said; with the sound off or
missing, the short spoken line comes back. Claude is told the sound already said it.
Fakes throughout: nothing is sent and no call is made."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf
from test_phone import ActionScout, _agent, paired  # noqa: F401  (paired: a fixture)
from test_routing import TONE, _shared, hear, make, run
from test_tune_voice import _speaker

import claude_agent_sdk
import scout.phone_service as ps
from scout import refs, shared_tools, tools, tts
from scout.config import Config
from scout.tools import Timers

# --- the speaker plays the file through the voice layer --------------------------------


def _aiff(path: Path, seconds: float = 0.5, rate: int = 48000) -> Path:
    """A stereo AIFF like Glass.aiff (48 kHz, two channels)."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    tone = 0.3 * np.sin(2 * np.pi * 880 * t)
    sf.write(path, np.stack([tone, tone], axis=1), rate, format="AIFF", subtype="PCM_24")
    return path


def test_the_default_is_macos_glass_played_from_where_it_is() -> None:
    assert Config().confirm.sound == "/System/Library/Sounds/Glass.aiff"


def test_the_sound_plays_through_the_voice_layer_like_a_chime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def go() -> tuple[bool, list[float], bool, bool]:
        spk, io = _speaker(monkeypatch)
        spk.confirm_sound = str(_aiff(tmp_path / "done.aiff"))
        ok = spk.confirm_tone()
        echo, words = spk.is_echo(), spk.speaking  # queued: the mic treats it as our own sound
        await spk.wait_idle()
        return ok, io.played, echo, words

    ok, played, echo, words = run(go())
    assert ok and played == [pytest.approx(0.5, abs=0.01)]  # mono, at the speaker's rate
    assert echo and not words  # gated like any playback, but not words to barge in on


def test_a_mono_file_at_another_rate_is_resampled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "mono.wav"
    sf.write(path, np.zeros(22050, np.float32), 44100)
    samples = tts.load_sound(str(path), 24000)
    assert samples is not None and samples.ndim == 1 and samples.size == 12000
    assert samples.dtype == np.float32


@pytest.mark.parametrize("which", ["off", "missing", "unreadable"])
def test_off_missing_or_unreadable_plays_nothing_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, which: str
) -> None:
    bad = tmp_path / "bad.aiff"
    bad.write_text("not audio")
    path = {"off": "", "missing": str(tmp_path / "gone.aiff"), "unreadable": str(bad)}[which]

    async def go() -> tuple[list[bool], list[float]]:
        spk, io = _speaker(monkeypatch)
        spk.confirm_sound = path
        got = [spk.confirm_tone(), spk.confirm_tone()]
        await spk.wait_idle()
        return got, io.played

    with caplog.at_level(logging.WARNING, logger="scout.tts"):
        got, played = run(go())
    assert got == [False, False] and played == []
    warned = [m for m in caplog.messages if "confirm sound" in m]
    assert len(warned) == (0 if which == "off" else 1)


# --- tier 0: "text Pat ..." and "call Pat" -----------------------------------------------


def test_a_confirmed_tier0_send_is_a_done_line_and_a_no_or_error_is_not() -> None:
    from scout import local_intents

    async def act_ok(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "text": "sent"}

    async def act(status: str) -> str:
        async def a(name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"status": status, "error": "Messages said no"}

        return await local_intents._done(a, "message_send", {}, "Sent to Pat Doe.", "Okay, not sent.")

    ok = run(local_intents._done(act_ok, "message_send", {}, "Sent to Pat Doe.", "Okay, not sent."))
    assert isinstance(ok, refs.Done) and ok == "Sent to Pat Doe."
    for status, said in (
        ("declined", "Okay, not sent."),
        ("error", "Sorry, Messages said no"),
        ("no_answer", ""),
    ):
        got = run(act(status))
        assert got == said and not isinstance(got, refs.Done)


def test_the_room_plays_the_tone_for_a_done_line_and_says_a_no(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import local_intents

    async def answer(text: str, ctx: Any, *a: Any, **k: Any) -> str:
        if text == "keep":
            return refs.Done("Okay, I'll keep that rule. That makes 31 rules.", rest="That makes 31 rules.")
        return refs.Done("Sent to Pat Doe.") if text == "yes" else "Okay, not sent."

    monkeypatch.setattr(local_intents, "answer", answer)

    async def go(text: str, tone_ok: bool = True) -> list[str]:
        r = make()
        r.spk.tone_ok = tone_ok
        assert await r.a._answer_locally(text)
        return r.spk.said

    assert run(go("yes")) == [TONE]
    assert run(go("no")) == ["Okay, not sent."]
    assert run(go("yes", tone_ok=False)) == ["Sent to Pat Doe."]  # off or missing: the short line
    assert run(go("keep")) == [TONE, "That makes 31 rules."]  # news beyond "done" is still said


def test_a_done_line_still_says_what_comes_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go(tone_ok: bool) -> list[str]:
        r = make()
        r.spk.tone_ok = tone_ok
        r.a._confirmed(
            refs.Done("Okay, I'll keep that rule. That makes 31 rules.", rest="That makes 31 rules.")
        )
        return r.spk.said

    assert run(go(True)) == [TONE, "That makes 31 rules."]
    assert run(go(False)) == ["Okay, I'll keep that rule. That makes 31 rules."]


# --- another Claude session's send (run_shared_tool) ---------------------------------------

MAIL = {"to": ["sam@example.com"], "subject": "Lunch", "body": "Noon works."}


async def _asked(r: Any, name: str, args: dict[str, Any], words: str) -> dict[str, Any]:
    task = asyncio.create_task(r.a.run_shared_tool("proj#42", name, args))
    await asyncio.sleep(0.01)  # the question is asked
    await hear(r, words, started=time.monotonic())  # speech that starts after it
    return await asyncio.wait_for(task, 2)


def test_a_session_send_after_a_yes_plays_the_tone_and_tells_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "mail_send")

    async def go(words: str, tone_ok: bool = True) -> tuple[dict[str, Any], list[str]]:
        r = make()
        r.spk.tone_ok = tone_ok
        return await _asked(r, "mail_send", MAIL, words), r.spk.said

    result, said = run(go("yes"))
    assert said[-1] == TONE and len(ran) == 1
    assert result == {"status": "ok", "text": "done" + shared_tools.TONE_NOTE}
    result, said = run(go("no"))
    assert result["status"] == "declined" and TONE not in said and len(ran) == 1
    result, said = run(go("yes", tone_ok=False))  # no sound: Claude says it, as before
    assert result == {"status": "ok", "text": "done"} and TONE not in said


def test_a_session_send_that_fails_is_not_toned(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.mac import ToolError

    async def fail(args: dict[str, Any]) -> str:
        raise ToolError("Mail said no")

    monkeypatch.setitem(
        shared_tools.BY_NAME, "mail_send", dataclasses.replace(shared_tools.BY_NAME["mail_send"], run=fail)
    )

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        return await _asked(r, "mail_send", MAIL, "yes"), r.spk.said

    result, said = run(go())
    assert result == {"status": "error", "error": "Mail said no"} and TONE not in said


def test_typed_approval_plays_the_tone_unless_another_session_has_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shared(monkeypatch, "mail_send")

    async def go(other: bool, tone_ok: bool = True) -> tuple[dict[str, Any], list[str]]:
        r = make()
        r.a.cfg.confirm.typed = True
        r.spk.tone_ok = tone_ok
        if other:
            assert await r.a.floor.acquire("other#1", 1)
        return await r.a.run_shared_tool("proj#42", "mail_send", {**MAIL, "approved": "typed"}), r.spk.said

    assert run(go(False)) == ({"status": "ok", "text": "done" + shared_tools.TONE_NOTE}, [TONE])
    assert run(go(False, tone_ok=False)) == ({"status": "ok", "text": "done"}, ["Sent to sam@example.com."])
    assert run(go(True)) == ({"status": "ok", "text": "done"}, [])  # not over someone else: Claude says it


def test_from_the_phone_nothing_plays_in_the_room(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _shared(monkeypatch, "reminder_add")

    async def go() -> tuple[dict[str, Any], list[str]]:
        r = make()
        return await r.a.run_shared_tool("phone", "reminder_add", {"title": "x"}, confirmed=True), r.spk.said

    result, said = run(go())
    assert result == {"status": "ok", "text": "done"} and said == [] and len(ran) == 1


# --- the room's own Claude: its voice_app tools --------------------------------------------


def _server_tools(monkeypatch: pytest.MonkeyPatch, on_done: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    real = claude_agent_sdk.create_sdk_mcp_server

    def capture(name: str, tools: list[Any]) -> Any:
        captured.update({t.name: t for t in tools})
        return real(name, tools=tools)

    async def done(args: dict[str, Any]) -> str:
        if args.get("fail"):
            from scout.mac import ToolError

            raise ToolError("Reminders said no")
        return "Added x to Reminders."

    shared = tuple(
        dataclasses.replace(t, run=done) if t.name in ("reminder_add", "reminders_list") else t
        for t in shared_tools.SHARED
    )
    monkeypatch.setattr(tools, "SHARED", shared)
    monkeypatch.setattr(tools, "create_sdk_mcp_server", capture)
    tools.build_server(Timers(lambda label: None), on_done=on_done)
    return captured


def test_a_room_tool_that_asks_plays_the_tone_and_its_result_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    played: list[str] = []

    def on_done(name: str) -> bool:
        played.append(name)
        return True

    t = _server_tools(monkeypatch, on_done)
    out = run(t["reminder_add"].handler({"title": "x"}))
    assert played == ["reminder_add"]
    assert out["content"][0]["text"] == "Added x to Reminders." + shared_tools.TONE_NOTE
    out = run(t["reminders_list"].handler({}))  # a read: no tone
    assert played == ["reminder_add"] and out["content"][0]["text"] == "Added x to Reminders."
    out = run(t["reminder_add"].handler({"title": "x", "fail": True}))  # an error: no tone
    assert played == ["reminder_add"] and out.get("is_error")


def test_a_room_tool_without_the_tone_returns_its_plain_result(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _server_tools(monkeypatch, lambda name: False)
    out = run(t["reminder_add"].handler({"title": "x"}))
    assert out["content"][0]["text"] == "Added x to Reminders."


def test_the_room_tone_is_where_the_turn_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> list[tuple[bool, list[str]]]:
        out = []
        for mini, silenced in ((True, False), (False, False), (True, True)):
            r = make()
            r.a._out = {"mini": mini, "client": None if mini else "web1"}
            r.a._silence_turn = silenced
            out.append((r.a._tool_done("reminder_add"), r.spk.said))
        return out

    assert run(go()) == [(True, [TONE]), (False, []), (False, [])]


# --- the phone: one word over the call ------------------------------------------------------


def test_on_the_phone_a_done_action_is_one_spoken_word(paired: Any) -> None:  # noqa: F811
    """The room's sound can't reach the call (it would be the Apple file served on the
    public URL, and the agent answers every tool call in words anyway): "Done." instead."""
    import re as re_mod

    class Failing(ActionScout):
        async def __call__(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
            if body.get("confirmed"):
                self.calls.append((path, body, timeout))
                return {"status": "error", "error": "Reminders said no"}
            return await super().__call__(path, body, timeout)

    async def go(scout: Any, name: str, args: dict[str, Any]) -> str:
        agent = _agent(paired, scout)
        first = (await agent._prepare_handler(name)(args, {"call_id": "call-a"})).to_dict()
        code = re_mod.search(r"code (\d{4})", first["response"]).group(1)  # type: ignore[union-attr]
        return str((await agent._confirm({"code": code}, {"call_id": "call-a"})).to_dict()["response"])

    done = run(go(ActionScout(), "reminder_add", {"title": "call Sam"}))
    assert done.endswith(ps.DONE_NOTE) and '"Done."' in ps.DONE_NOTE
    failed = run(go(Failing(), "reminder_add", {"title": "call Sam"}))
    assert failed == "Not done: Reminders said no"  # an error is still said
    asked = run(go(ActionScout(), "session_ask", {"session": "scout-dev", "message": "x"}))
    assert ps.DONE_NOTE not in asked  # its reply is still to come: the agent says so
