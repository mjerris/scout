"""Setting Scout up by voice (onboarding.py): the walkthrough's steps with scripted
answers, config.toml written with a backup and checked first, "later" resuming where it
stopped, no secret ever asked aloud, and the MCP servers of a made-up Claude Code
config offered once."""

from __future__ import annotations

import asyncio
import json
import stat
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scout import config, onboarding


def test_what_asks_for_the_walkthrough() -> None:
    for t in ("Set me up.", "walk me through setup", "Walk me through the set up", "Can you set me up?",
              "continue setup", "start setup over", "help me set things up", "resume the setup"):  # fmt: skip
        assert onboarding.is_request(t), t
    for t in ("set up a meeting with Pat", "what's the setup for the launch", "set an alarm"):
        assert not onboarding.is_request(t), t
    assert onboarding.from_the_start("start setup over") and not onboarding.from_the_start("set me up")


# --- config.toml ---------------------------------------------------------------------------


OLD = """# my settings
[tasks]
me = ["Old Name"]  # me
mirror = true

[slack]
skip = [
  "random",
  "noise",
]
every_min = 15.0

[[weird]]
x = 1
"""


def test_config_edits_keep_everything_else() -> None:
    new = onboarding.edit_toml(OLD, {
        ("tasks", "me"): ["Pat Lee", "Old Name"],  # replaced where it is
        ("slack", "skip"): ["random"],  # a value over several lines, replaced whole
        ("slack", "announce_dms"): "all",  # added to its table
        ("lists", "backend"): "scout",  # a new table
    })  # fmt: skip
    doc = tomllib.loads(new)
    assert doc["tasks"] == {"me": ["Pat Lee", "Old Name"], "mirror": True}
    assert doc["slack"] == {"skip": ["random"], "every_min": 15.0, "announce_dms": "all"}
    assert doc["lists"] == {"backend": "scout"} and doc["weird"] == [{"x": 1}]
    assert new.startswith("# my settings\n") and "noise" not in new


def test_writing_config_keeps_a_private_copy_first(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    old = OLD.replace("[[weird]]\nx = 1\n", "")
    path.write_text(old)
    backup = onboarding.write_config({("lists", "backend"): "reminders"}, path, now=0)
    assert (
        backup is not None and backup.read_text() == old and backup.name.startswith("config.toml.bak-setup-")
    )
    assert config.load(path).lists.backend == "reminders"
    for p in (path, backup):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # A value Scout's config refuses leaves the file as it was.
    with pytest.raises(ValueError):
        onboarding.write_config({("notes", "apple"): "yes please"}, path)
    assert config.load(path).notes.apple is True and not list(tmp_path.glob("*.check"))


# --- the walkthrough -----------------------------------------------------------------------


class Room:
    """Scripted answers; everything said is kept."""

    def __init__(self, yes: Sequence[bool | str | None], text: Sequence[str | None] = ()) -> None:
        self.yes, self.text = list(yes), list(text)
        self.said: list[str] = []
        self.asked: list[str] = []
        self.access: list[str] = []

    async def ask_yes(self, spoken: str, detail: str) -> bool | str | None:
        self.asked.append(spoken)
        return self.yes.pop(0) if self.yes else None

    async def ask_text(self, spoken: str) -> str | None:
        self.asked.append(spoken)
        return self.text.pop(0) if self.text else None

    async def say(self, text: str) -> None:
        self.said.append(text)

    async def grant(self, what: str) -> str:
        self.access.append(what)
        return ""


def walk(tmp_path: Path, room: Room, cfg: config.Config | None = None, servers: dict[str, Any] | None = None,
         ) -> onboarding.Walkthrough:  # fmt: skip
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": servers or {}}))
    if cfg is None:
        cfg = config.Config()
        cfg.claude.cwd = str(tmp_path / "room")
    return onboarding.Walkthrough(
        cfg, room.ask_yes, room.ask_text, room.say, room.grant,
        config_path=tmp_path / "config.toml", tools_path=tmp_path / "tools.toml",
        state_path=tmp_path / "onboarding.json", claude_json=cj, now=lambda: 1.0e9,
    )  # fmt: skip


N_STEPS = len(onboarding.STEPS)


def test_a_new_install_goes_through_every_step_and_writes_its_answers(tmp_path: Path) -> None:
    #           name ok, phone no, lists yes, notes no, privacy yes, mail yes, calendar yes,
    #           contacts no, slack yes, github no, granola no, reference later
    room = Room([True, False, True, False, True, True, True, False, True, False, False, "later"],
                ["My name is pat lee."])  # fmt: skip
    w = walk(tmp_path, room)
    said = asyncio.run(w.run())
    assert said.startswith("Okay, we'll pick up from here")
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.tasks.me == ["Pat Lee"] and cfg.lists.backend == "reminders" and cfg.notes.apple is False
    assert cfg.privacy.mode == "balanced" and cfg.index.mail is True and cfg.contacts.enabled is False
    assert room.access == ["reminders", "calendar"]
    # Secrets are never asked for aloud: only the name is a spoken answer; the token steps
    # say where the token goes.
    assert room.asked[0] == "First, what should I call you?" and len(room.asked) == 1 + 1 + N_STEPS
    slack_said = next(s for s in room.said if "Slack app" in s)
    assert "slack section" in slack_said and "never ask for it out loud" in slack_said
    assert not any(k in (tmp_path / "config.toml").read_text() for k in ("token", "api_key"))
    st = onboarding.read_state(tmp_path / "onboarding.json")
    assert st["later"] == "reference" and st["answers"]["slack"] == "yes"
    # "later" picks up at that step.
    room2 = Room([True])
    w2 = walk(tmp_path, room2, config.load(tmp_path / "config.toml"))
    assert asyncio.run(w2.run()).startswith("That's everything")
    assert room2.said[0] == "Picking up where we left off." and len(room2.asked) == 1
    assert "references" in room2.asked[0]


def test_no_answer_is_later_and_steps_already_set_up_are_passed_over(tmp_path: Path) -> None:
    cfg = config.Config()
    cfg.claude.cwd = str(tmp_path)
    cfg.tasks.me = ["Pat"]
    cfg.slack.token = "xoxp-not-said"  # noqa: S105 (a made-up token)
    cfg.phone.enabled = True
    room = Room([None])
    w = walk(tmp_path, room, cfg)
    asyncio.run(w.run())
    assert room.asked[0].endswith("Use the Reminders app for them?")  # name and phone were set up
    assert onboarding.read_state(tmp_path / "onboarding.json")["later"] == "lists"
    room = Room([False] * 20)
    asyncio.run(walk(tmp_path, room, cfg).run())
    assert not any("Set up Slack?" in a for a in room.asked) and not any(
        "xoxp" in s for s in room.said + room.asked
    )


def test_mcp_servers_in_claude_code_are_offered_and_the_answers_kept(tmp_path: Path) -> None:
    servers = {"widgets": {"command": "uvx", "args": ["widgets-mcp"]}, "wiki": {"url": "https://wiki.example/mcp"},
               "scout": {"command": "x"}, "bad name!": {"command": "x"}}  # fmt: skip
    (tmp_path / "tools.toml").write_text('[[server]]\nfrom_claude = "wiki"\n')
    cfg = config.Config()
    cfg.claude.cwd = str(tmp_path)
    cfg.tasks.me = ["Pat"]
    room = Room([False] * N_STEPS + [True])
    asyncio.run(walk(tmp_path, room, cfg, servers).run())
    assert room.asked[-1].startswith("Use your widgets server in Scout too?")  # wiki is listed already
    doc = tomllib.loads((tmp_path / "tools.toml").read_text())
    assert doc["server"][1] == {"from_claude": "widgets", "channels": ["room"], "private": True}
    assert stat.S_IMODE((tmp_path / "tools.toml").stat().st_mode) == 0o600
    assert list(tmp_path.glob("tools.toml.bak-setup-*"))
    st = onboarding.read_state(tmp_path / "onboarding.json")
    assert st["mcp"] == {"widgets": "yes"} and st["finished"]
    # Going through it again asks nothing about servers.
    room = Room([])
    asyncio.run(walk(tmp_path, room, cfg, servers).run())
    assert room.asked == []


def test_a_new_server_later_is_asked_about_once(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {"widgets": {"command": "x"}}}))
    cfg = config.Config()
    cfg.claude.cwd = str(tmp_path)
    cfg.tasks.me = ["Pat"]  # an install in use
    (tmp_path / "config.toml").write_text('[tasks]\nme = ["Pat"]\n')
    sp, tools = tmp_path / "onboarding.json", tmp_path / "tools.toml"
    st = onboarding.startup_state(cfg, tmp_path / "config.toml", sp, cj)
    assert st["auto"] is False and st["mcp_seen"] == ["widgets"]
    assert onboarding.new_servers(cfg, tools, st, 100.0, cj) == []  # the baseline: not new
    cj.write_text(json.dumps({"mcpServers": {"widgets": {"command": "x"}, "gizmo": {"command": "y"}}}))
    assert onboarding.new_servers(cfg, tools, st, 100.0, cj) == ["gizmo"]
    st["mcp"]["gizmo"] = "no"
    assert onboarding.new_servers(cfg, tools, st, 100.0, cj) == []
    st["mcp"], st["mcp_asked"] = {}, {"gizmo": 100.0}  # asked, nobody answered: again a day later
    assert onboarding.new_servers(cfg, tools, st, 200.0, cj) == []
    assert onboarding.new_servers(cfg, tools, st, 100.0 + onboarding.NEW_MCP_RETRY_S, cj) == ["gizmo"]


def test_only_a_new_install_starts_the_walkthrough_by_itself(tmp_path: Path) -> None:
    cfg = config.Config()
    cfg.claude.cwd = str(tmp_path)
    assert onboarding.startup_state(cfg, tmp_path / "none.toml", tmp_path / "a.json")["auto"] is True
    (tmp_path / "config.toml").write_text("[pulse]\nenabled = true\n")
    assert onboarding.is_new_install(cfg, tmp_path / "config.toml")
    cfg.slack.token = "xoxp-x"  # noqa: S105 (a made-up token)
    assert not onboarding.is_new_install(cfg, tmp_path / "config.toml")


@pytest.mark.parametrize(("heard", "name"), [("My name is pat lee.", "Pat Lee"), ("Call me Mike", "Mike"),
                                              ("it's Jo-Ann", "Jo-Ann"), ("I don't know what you mean by that", "")])  # fmt: skip
def test_a_spoken_name(heard: str, name: str) -> None:
    assert onboarding.spoken_name(heard) == name


# --- in the room -----------------------------------------------------------------------------


@pytest.fixture
def setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from scout import extmcp

    monkeypatch.setattr(onboarding, "CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(onboarding, "STATE", tmp_path / "onboarding.json")
    monkeypatch.setattr(extmcp, "PATH", tmp_path / "tools.toml")
    monkeypatch.setattr(extmcp, "CLAUDE_JSON", tmp_path / "claude.json")
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"widgets": {"command": "x"}}}))
    return tmp_path


def test_set_me_up_in_the_room_asks_by_voice_and_later_stops_it(setup_home: Path) -> None:
    from test_routing import hear, make, run

    async def go() -> list[str]:
        r = make()
        task = asyncio.create_task(r.a._answer_locally("Hey, walk me through setup"))
        for words in ("My name is Pat Lee", "yes", "not now"):
            await asyncio.sleep(0.05)
            await hear(r, words, started=time.monotonic())
        assert await task
        return r.spk.said

    said = run(go())
    assert said[1] == "First, what should I call you?" and said[2] == "I'll call you Pat Lee. Is that right?"
    assert said[3].endswith("Set up the phone?")
    assert said[-1] == "Okay, we'll pick up from here next time you say set me up."
    assert config.load(setup_home / "config.toml").tasks.me == ["Pat Lee"]
    assert onboarding.read_state(setup_home / "onboarding.json")["later"] == "phone"


def test_stop_ends_the_walkthrough_without_a_no(setup_home: Path) -> None:
    from test_routing import make, run

    async def go() -> None:
        r = make()
        (setup_home / "config.toml").write_text('[tasks]\nme = ["Pat"]\n')
        task = asyncio.create_task(r.a._answer_locally("set me up"))
        await asyncio.sleep(0.05)
        await r.a.stop()
        await task

    run(go())
    st = onboarding.read_state(setup_home / "onboarding.json")
    assert st["later"] == "phone" and "phone" not in st["answers"]


def test_a_new_server_is_asked_about_at_a_quiet_moment(setup_home: Path) -> None:
    from test_routing import hear, make, run

    (setup_home / "config.toml").write_text('[tasks]\nme = ["Pat"]\n')

    async def go() -> list[str]:
        r = make()
        st = onboarding.startup_state(config.load(setup_home / "config.toml"), setup_home / "config.toml",
                                      setup_home / "onboarding.json", setup_home / "claude.json")  # fmt: skip
        (setup_home / "claude.json").write_text(
            json.dumps({"mcpServers": {"widgets": {"command": "x"}, "gizmo": {"command": "y"}}})
        )
        task = asyncio.create_task(r.a._offer_new_servers({"mini": True, "client": None}, st))
        await asyncio.sleep(0.05)
        await hear(r, "yes", started=time.monotonic())
        await task
        again = asyncio.create_task(r.a._offer_new_servers({"mini": True, "client": None}, st))
        await again  # answered: nothing asked
        return r.spk.said

    said = run(go())
    assert [s for s in said if s.startswith("Use your")] == [
        "Use your gizmo server in Scout too? In the room, with nothing sent without asking."
    ]
    assert onboarding.listed_servers(setup_home / "tools.toml") == {"gizmo"}
    assert onboarding.read_state(setup_home / "onboarding.json")["mcp"] == {"gizmo": "yes"}


@pytest.mark.parametrize(
    ("heard", "later"),
    [("later", True), ("Not now.", True), ("ask me tomorrow", True), ("maybe another time", True),
     ("no, not now", False), ("yes", False), ("no", False), ("yes, later", False)],
)  # fmt: skip
def test_later_as_an_answer(heard: str, later: bool) -> None:
    from scout import speech

    assert speech.says_later(heard) is later
