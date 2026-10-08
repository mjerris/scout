"""Tool argument checks, saved 'always' rules, and timers."""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from claude_voice import mac
from claude_voice.rules import Rules, rule_for
from claude_voice.speech import parse_answer
from claude_voice.tools import Timers, _human, build_server


@pytest.mark.parametrize("url", ["https://www.netflix.com", "http://example.com/a?b=c"])
def test_url_ok(url: str) -> None:
    assert mac.check_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ssh://host",
        "netflix.com",
        "https://x.com/a b",
        "https://x.com/\nrm -rf",
        "",
    ],
)
def test_url_rejected(url: str) -> None:
    with pytest.raises(mac.ToolError):
        mac.check_url(url)


def test_search_url_encodes_query() -> None:
    assert (
        mac.search_url("Netflix", "Nobody Wants This") == "https://www.netflix.com/search?q=Nobody+Wants+This"
    )
    assert mac.search_url("youtube", 'a"b & c') == "https://www.youtube.com/results?search_query=a%22b+%26+c"
    with pytest.raises(mac.ToolError):
        mac.search_url("evil.com", "x")


@pytest.mark.parametrize("name", ["Google Chrome", "Music", "Microsoft Word", "Arc"])
def test_app_name_ok(name: str) -> None:
    assert mac.check_app_name(name) == name


@pytest.mark.parametrize("name", ['Music" & do shell script "rm', "../../bin/sh", "", "a" * 100, "-a"])
def test_app_name_rejected(name: str) -> None:
    with pytest.raises(mac.ToolError):
        mac.check_app_name(name)


def test_check_int() -> None:
    assert mac.check_int("3", 1, 10, "tab") == 3
    for bad in ("x", 0, 11, None, "1; rm"):
        with pytest.raises(mac.ToolError):
            mac.check_int(bad, 1, 10, "tab")


def test_scripts_take_arguments_not_spliced_text() -> None:
    # Values reach AppleScript only as argv; the script texts are constants.
    for script in (mac._CHROME_SWITCH, mac._CHROME_NEW_TAB, mac._FULLSCREEN, mac._SET_VOLUME, mac._SET_MUTED):
        assert "on run argv" in script and "{" not in script.replace("{URL:u}", "")


@pytest.mark.parametrize(
    "text, answer",
    [
        ("yes always", "always"),
        ("Always.", "always"),
        ("always allow that", "always"),
        ("yes", True),
        ("no, not always", False),
        ("never", False),
    ],
)
def test_parse_answer(text: str, answer: bool | str | None) -> None:
    assert parse_answer(text) == answer


def test_rule_scope() -> None:
    assert rule_for("Bash", {"command": "osascript -e 'x'"}) == {
        "tool": "Bash",
        "command": "osascript -e 'x'",
    }
    assert rule_for("WebFetch", {"url": "https://Example.com/a"}) == {
        "tool": "WebFetch",
        "domain": "example.com",
    }
    assert rule_for("mcp__github__list_issues", {"q": "t"}) == {"tool": "mcp__github__list_issues"}
    # A blanket "always" for a tool that creates/sends/deletes would cover any future call.
    assert rule_for("mcp__github__create_issue", {"title": "t"}) is None
    assert rule_for("mcp__slack__send_message", {"text": "hi"}) is None
    # URL tools are remembered per site.
    assert rule_for("mcp__voice_app__open_url", {"url": "https://www.netflix.com/x"}) == {
        "tool": "mcp__voice_app__open_url",
        "domain": "www.netflix.com",
    }
    assert rule_for("Edit", {"file_path": "/x"}) is None
    assert rule_for("Write", {"file_path": "/x"}) is None


def test_rules_exact_command_only(tmp_path: Path) -> None:
    r = Rules(tmp_path / "allow.json")
    assert r.add("Bash", {"command": "osascript -e 'get volume settings'"})
    assert r.matches("Bash", {"command": "osascript -e 'get volume settings'"})
    assert not r.matches("Bash", {"command": "osascript -e 'do shell script \"rm -rf ~\"'"})
    assert r.add("Edit", {"file_path": "/x"}) is None
    # persisted and removable
    r2 = Rules(tmp_path / "allow.json")
    assert r2.matches("Bash", {"command": "osascript -e 'get volume settings'"})
    r2.remove(r2.listing()[0]["id"])
    assert not Rules(tmp_path / "allow.json").items


def test_timers_fire_and_cancel() -> None:
    async def go() -> tuple[list[str], list[str]]:
        fired: list[str] = []
        t = Timers(fired.append)
        t.set(1, "tea")
        t.set(60, "pasta")
        assert len(t.listing()) == 2
        assert t.cancel("pasta") == ["pasta"]
        await asyncio.sleep(1.1)
        return fired, t.listing()

    fired, left = asyncio.run(go())
    assert fired == ["tea"] and left == []


def test_timer_limits() -> None:
    async def go() -> None:
        t = Timers(lambda _: None)
        for bad in (0, 0.5, 24 * 3600 + 1):
            with pytest.raises(mac.ToolError):
                t.set(bad, "x")

    asyncio.run(go())


def test_human_durations() -> None:
    assert _human(90) == "1 minute 30 seconds"
    assert _human(3600 * 2 + 60 * 5) == "2 hours 5 minutes"
    assert _human(1) == "1 second"


def test_forbidden_calls(tmp_path: Path) -> None:
    from claude_voice.brain import Brain
    from claude_voice.config import ROOT, ClaudeConfig

    async def never_asked(spoken: str, detail: str) -> bool | None:
        raise AssertionError("forbidden calls must not ask")

    server, names = build_server(Timers(lambda label: None))
    b = Brain(ClaudeConfig(), never_asked, server, names, Rules(tmp_path / "r.json"), lambda kind, text: None)
    assert b._forbidden("Edit", {"file_path": str(ROOT / "src/claude_voice/brain.py")})
    assert b._forbidden("Write", {"file_path": str(ROOT / "config.toml")})
    assert b._forbidden("mcp__voice__discuss", {})
    assert b._forbidden("Edit", {"file_path": str(tmp_path / "notes.txt")}) is None
    assert b._forbidden("Bash", {"command": "ls"}) is None


def test_bad_permission_mode_fails_at_startup(tmp_path: Path) -> None:
    from claude_voice.brain import Brain
    from claude_voice.config import ClaudeConfig

    async def never(spoken: str, detail: str) -> bool | None:
        return None

    server, names = build_server(Timers(lambda label: None))
    with pytest.raises(ValueError, match="permission_mode"):
        Brain(
            ClaudeConfig(permission_mode="acceptedits"),
            never,
            server,
            names,
            Rules(tmp_path / "r.json"),
            lambda k, t: None,
        )


def _brain(tmp_path: Path, **cfg: Any) -> Any:
    from claude_voice.brain import Brain
    from claude_voice.config import ClaudeConfig

    async def never(spoken: str, detail: str) -> bool | None:
        return None

    server, names = build_server(Timers(lambda label: None))
    return Brain(ClaudeConfig(**cfg), never, server, names, Rules(tmp_path / "r.json"), lambda k, t: None)


def test_app_files_are_forbidden_in_any_spelling(tmp_path: Path) -> None:
    from claude_voice.config import ROOT

    b = _brain(tmp_path, cwd=str(Path.home()))
    rel = os.path.relpath(ROOT / "config.toml", Path.home())
    for path in [str(ROOT / "config.toml"), str(ROOT).upper() + "/CONFIG.TOML", rel]:
        assert b._forbidden("Edit", {"file_path": path}), path
    assert b._forbidden("Bash", {"command": f"perl -pi -e 's/a/b/' {ROOT}/config.toml"})
    home_rel = "~/" + os.path.relpath(ROOT, Path.home()).upper()  # "~/SRC/CLAUDE-VOICE"
    assert b._forbidden("Bash", {"command": f"node -e 1 {home_rel}/state/voice_allow.json"})
    assert b._forbidden("Edit", {"file_path": str(tmp_path / "notes.txt")}) is None


def test_secrets_are_never_readable(tmp_path: Path) -> None:
    b = _brain(tmp_path)
    for path in ["~/.ssh/id_ed25519", "~/.aws/credentials", "~/.netrc"]:
        assert b._forbidden("Read", {"file_path": path}), path
    assert b._forbidden("Bash", {"command": "cat ~/.ssh/id_ed25519"})
    assert b._forbidden("Read", {"file_path": "~/Documents/notes.txt"}) is None


def test_bash_and_fetch_always_reach_the_voice_prompt(tmp_path: Path) -> None:
    import json

    for policy in ("strict", "settings", "settings_no_hooks"):
        settings = json.loads(_brain(tmp_path, approval_policy=policy)._options().settings)
        ask = settings["permissions"]["ask"]
        assert "Bash" in ask and "WebFetch" in ask and "mcp__voice_app__open_url" in ask
        assert "WebFetch" not in settings["permissions"]["allow"]


def test_only_safe_single_commands_skip_the_prompt(tmp_path: Path) -> None:
    b = _brain(tmp_path)
    assert b._auto_allowed("Bash", {"command": "ls -la"})
    assert not b._auto_allowed("Bash", {"command": "ls; rm -rf x"})
    assert not b._auto_allowed("Bash", {"command": "perl -e 1"})


def test_always_ask_commands_ignore_saved_rules(tmp_path: Path) -> None:
    async def go() -> list[str]:
        asked: list[str] = []
        b = _brain(tmp_path)

        async def confirm(spoken: str, detail: str) -> bool | str | None:
            asked.append(spoken)
            return True

        b.confirm = confirm
        b.rules.add("Bash", {"command": "git push origin main"})  # an old "yes, always"
        await b._ask("Bash", {"command": "git push origin main"})
        await b._ask("Bash", {"command": "git -C ~/proj push origin main"})
        return asked

    assert asyncio.run(go()) == ["Run git push?", "Run git push?"]


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ("[audio]\ninput_device = 1\n", None),
        ('[web]\nhosts = "lan"\n', "list of strings"),
        ('[web]\nport = "8765"\n', "whole number"),
        ("wake = 3\n", "section"),
        ("[tts]\nspeed = 1\n", None),
    ],
)
def test_config_values_are_type_checked(tmp_path: Path, toml: str, error: str | None) -> None:
    from claude_voice.config import load

    f = tmp_path / "c.toml"
    f.write_text(toml)
    if error is None:
        cfg = load(f)
        assert isinstance(cfg.audio.input_device, str) and isinstance(cfg.tts.speed, float)
    else:
        with pytest.raises(ValueError, match=error):
            load(f)
