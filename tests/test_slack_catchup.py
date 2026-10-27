"""slack_catch_up: what was said in a Slack conversation since your last message there."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from scout import index, mcp_server, shared_tools, slack_catchup
from scout.mac import ToolError
from scout.taskfarm import SlackMe


def put(ix: index.Index, channel: str, ts: float, lines: list[tuple[str, str]], other: bool = False) -> None:
    ix.put(
        kind="slack",
        id=f"slack:{channel}:{ts:.6f}",
        title="Group DM with nina, omar, mike",
        body="\n".join(text for _, text in lines),
        ts=ts,
        meta={"source": "slack", "channel": channel, "conversation": "Group DM with nina, omar, mike",
              "ts": f"{ts:.6f}", "last_ts": f"{ts:.6f}", "lines": [[uid, []] for uid, _ in lines]},
    )  # fmt: skip


@pytest.fixture
def ix(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    monkeypatch.setattr(slack_catchup, "slack_me", lambda: SlackMe("UME", ("mike",)))
    s = index.default()
    assert isinstance(s, index.Index)
    return s


def catch_up(**a: Any) -> str:
    return asyncio.run(slack_catchup.catch_up(a))


def test_catch_up_starts_after_your_last_message(ix: index.Index) -> None:
    import time

    t = time.time() - 3600
    put(ix, "G1", t, [("UA", "Nina: morning"), ("UME", "Mike: hi both")])
    put(ix, "G1", t + 60, [("UB", "Omar: did the deploy go out?")])
    put(ix, "G1", t + 120, [("UA", "Nina: yes, at nine")])
    put(ix, "D9", t + 130, [("UC", "Sam: unrelated")])
    out = catch_up(ref=f"slack:G1:{t + 120:.6f}")
    head, *lines = out.split("\n")
    assert head.startswith("Group DM with nina, omar, mike: 2 messages since your last one there")
    assert lines == ["Omar: did the deploy go out?", "Nina: yes, at nine"]


def test_catch_up_without_a_message_of_yours_gives_the_newest(ix: index.Index) -> None:
    import time

    t = time.time() - 600
    put(ix, "G2", t, [("UA", "Nina: lunch?"), ("UB", "Omar: sure")])
    out = catch_up(ref=f"slack:G2:{t:.6f}")
    assert "you haven't written there" in out and out.endswith("Omar: sure")


def test_nothing_from_slack_said_is_a_clear_error(ix: index.Index) -> None:
    with pytest.raises(ToolError, match="nothing said recently is from Slack"):
        catch_up(ref="note:3")


def test_catch_up_reaches_the_room_the_phone_and_sessions() -> None:
    from scout import assistant

    assert not shared_tools.BY_NAME["slack_catch_up"].asks
    assert "mcp__voice_app__slack_catch_up" in assistant.PHONE_TOOLS
    assert "slack_catch_up" in {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
