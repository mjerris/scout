"""Review fixes: always-ask commands hidden in a chain, cancelling a timer by number,
two-word region names, and the daily briefing's schedule."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from test_mail import _brain

from scout import briefing, weather
from scout.brain import matches_always_ask
from scout.tools import Timers

PUSH = ["Bash(git push:*)"]


@pytest.mark.parametrize(
    "command",
    [
        "cd ~/repo\ngit push origin main",
        "echo $(git push)",
        'bash -c "git push"',
        "sh -c 'git push origin main'",
        "echo `git push`",
        "(git push)",
        "eval git push",
    ],
)
def test_always_ask_sees_through_lines_substitution_and_sh_c(command: str) -> None:
    assert matches_always_ask(command, PUSH)


@pytest.mark.parametrize("command", ["ls -la", "git status", "ssh host git status", "echo push"])
def test_other_commands_are_not_always_ask(command: str) -> None:
    assert not matches_always_ask(command, PUSH)


def test_a_chained_push_is_never_saved_as_always(tmp_path: Path) -> None:
    async def go() -> tuple[list[str], list[dict[str, Any]]]:
        asked: list[str] = []
        b = _brain(tmp_path)

        async def confirm(spoken: str, detail: str) -> bool | str | None:
            asked.append(spoken)
            return "always"

        b.confirm = confirm
        args = {"command": "cd ~/repo\ngit push origin main"}
        await b._ask("Bash", args)
        await b._ask("Bash", args)
        return asked, b.rules.listing()

    asked, rules = asyncio.run(go())
    assert len(asked) == 2 and rules == []


def test_cancel_by_number_cancels_only_that_timer() -> None:
    async def go() -> tuple[list[str], list[str]]:
        t = Timers(lambda label: None)
        t.set(60, "tea")
        t.set(60, "pasta 10 min")
        gone = t.cancel("1")
        left = [label for label, _, _ in t.active.values()]
        t.cancel("all")
        return gone, left

    gone, left = asyncio.run(go())
    assert gone == ["tea"] and left == ["pasta 10 min"]


def test_cancel_by_label_still_works() -> None:
    async def go() -> list[str]:
        t = Timers(lambda label: None)
        t.set(60, "tea")
        t.set(60, "pasta")
        gone = t.cancel("pasta")
        t.cancel("all")
        return gone

    assert asyncio.run(go()) == ["pasta"]


def _session(results: list[dict[str, Any]], seen: list[dict[str, str]]) -> Any:
    class Resp:
        async def __aenter__(self) -> "Resp":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return {"results": results}

    class Session:
        def get(self, url: str, params: dict[str, str]) -> Resp:
            seen.append(params)
            return Resp()

        async def close(self) -> None:
            return None

    return Session()


@pytest.mark.parametrize("place", ["New Mexico", "West Virginia", "New England", "Northern Ireland"])
def test_a_two_word_region_is_not_split(place: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "PLACES", tmp_path / "places.json")
    seen: list[dict[str, str]] = []
    hit = {"name": place, "admin1": "", "country": "", "latitude": 1.0, "longitude": 2.0}
    asyncio.run(weather.geocode(place, _session([hit], seen)))
    assert seen[0]["name"] == place


def test_a_place_in_a_region_still_splits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "PLACES", tmp_path / "places.json")
    seen: list[dict[str, str]] = []
    hit = {
        "name": "Luther",
        "admin1": "Michigan",
        "country": "United States",
        "latitude": 1.0,
        "longitude": 2.0,
    }
    asyncio.run(weather.geocode("luther michigan", _session([hit], seen)))
    assert seen[0]["name"] == "luther"


def test_a_guess_outside_the_region_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "PLACES", tmp_path / "places.json")
    other = {
        "name": "Luther",
        "admin1": "Oklahoma",
        "country": "United States",
        "latitude": 1.0,
        "longitude": 2.0,
    }
    found = asyncio.run(weather.geocode("Luther, Michigan", _session([other], [])))
    assert found["region"] == "Oklahoma" and not (tmp_path / "places.json").exists()


def test_the_briefing_after_dst_ends_is_still_at_7_30(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    now = briefing.local_now()
    # 7:31 AM EDT on Sat Oct 31 2026; DST ends overnight.
    sat = dt.datetime(2026, 10, 31, 7, 31, tzinfo=now.tzinfo)
    nxt = briefing.next_at("07:30", sat)
    assert (nxt.hour, nxt.minute, nxt.utcoffset()) == (7, 30, dt.timedelta(hours=-5))
    assert nxt.timestamp() - sat.timestamp() == (24 * 60 + 59) * 60  # a 25-hour day


def test_the_briefing_sleeps_real_seconds_across_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    tz = briefing.local_now().tzinfo
    clock = [dt.datetime(2026, 10, 31, 7, 31, tzinfo=tz)]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    async def run() -> None:
        return None

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(briefing.daily("07:30", run, now=lambda: clock[0], sleep=sleep))
    assert slept == [(24 * 60 + 59) * 60.0]  # wall-clock subtraction would say 23 h 59 min


def test_the_briefing_sleep_follows_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncio's clock stops while the Mac sleeps; the wall clock doesn't.
    wall = [1000.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        wall[0] += seconds if len(slept) < 2 else 8 * 3600  # the lid closed during the 2nd chunk

    monkeypatch.setattr("scout.briefing.time.time", lambda: wall[0])
    monkeypatch.setattr("scout.briefing.asyncio.sleep", fake_sleep)
    asyncio.run(briefing._wall_sleep(8 * 3600))
    assert slept == [60.0, 60.0]
