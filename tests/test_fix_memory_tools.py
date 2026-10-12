"""Review fixes in the pulse, the Starlink watcher and maps: quiet hours don't use up
what's said once, a bad quiet_hours or a failed announce doesn't end the watcher, and a
quote in a place name stays inside the Overpass string (and a refused query isn't
reported as a busy server)."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from scout import maps, pulse, starlink, weather
from scout.mac import ToolError

from test_maps import CADILLAC, Resp
from test_pulse import TZ, ev

# --- #6: quiet hours ------------------------------------------------------------------


def test_what_comes_due_in_quiet_hours_is_said_after(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [ev("Standup", 8, 5, 15), ev("Dentist", 16, 0, 60), ev("Team sync", 16, 30)]

    async def e(start: str, end: str) -> dict[str, Any]:
        return {"events": events}

    async def r() -> dict[str, Any]:
        return {"reminders": []}

    times = [dt.datetime(2026, 10, 12, h, m, tzinfo=TZ) for h, m in ((0, 5), (7, 45), (8, 0), (9, 0))]
    said: list[tuple[str, str]] = []
    clock: list[dt.datetime] = []

    def now() -> dt.datetime:
        clock.append(times[len(clock)])
        return clock[-1]

    async def say(text: str) -> None:
        said.append((clock[-1].strftime("%H:%M"), text))

    async def sleep(_: float) -> None:
        if len(clock) == len(times):
            raise asyncio.CancelledError

    monkeypatch.setattr("scout.pulse.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pulse.run(say, pulse.Pulse(10, 15, e, r), "22:00-08:00", now=now))
    assert said == [
        ("08:00", "Heads up: Standup in 5 minutes."),
        ("08:00", "Today, Dentist overlaps Team sync at 4:30 PM."),
    ]


# --- #43: the Starlink watcher survives a bad quiet_hours and a failed announce -------


class DeadDish:
    def __init__(self) -> None:
        self.polls = 0

    async def aget(self, what: str) -> dict[str, Any]:
        self.polls += 1
        raise ToolError("can't reach the dish")


@pytest.mark.parametrize(("quiet_hours", "fails"), [("10pm-8am", False), ("", True)])
def test_the_starlink_watch_keeps_going(
    monkeypatch: pytest.MonkeyPatch, quiet_hours: str, fails: bool
) -> None:
    cfg = SimpleNamespace(
        announce=True, announce_outage_s=0.0, ignore_alerts=[], quiet_hours=quiet_hours, poll_s=0.01
    )
    monkeypatch.setattr(starlink, "load", lambda: SimpleNamespace(starlink=cfg))
    d = DeadDish()
    monkeypatch.setattr(starlink, "dish", lambda: d)
    announced: list[str] = []

    async def announce(text: str) -> None:
        announced.append(text)
        if fails:
            raise RuntimeError("the speaker is gone")

    async def go() -> bool:
        task = asyncio.create_task(starlink.watch(announce))
        await asyncio.sleep(0.2)
        alive = not task.done()
        task.cancel()
        return alive

    assert asyncio.run(go()) is True
    assert d.polls > 2  # still polling after the outage was (or failed to be) announced
    assert announced == ([] if not fails else ["I can't reach the Starlink dish."])


# --- #45: a quote in a place name ------------------------------------------------------


class Overpass:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.queries: list[str] = []

    def request(self, method: str, url: str, **kw: Any) -> Any:
        assert "interpreter" in url
        self.queries.append(kw["data"]["data"])
        if self.status != 200:
            return Refusing(url, self.status)
        return Resp({"elements": []})

    async def close(self) -> None:
        return None


class Refusing(Resp):
    def __init__(self, url: str, status: int) -> None:
        super().__init__(None, status)
        self.url = yarl.URL(url)

    def raise_for_status(self) -> None:
        info = aiohttp.RequestInfo(self.url, "POST", CIMultiDictProxy(CIMultiDict()), self.url)
        raise aiohttp.ClientResponseError(info, (), status=self.status, message="Bad Request")


@pytest.fixture
def _cadillac(monkeypatch: pytest.MonkeyPatch) -> None:
    async def geocode(place: str, session: Any = None) -> dict[str, Any]:
        return CADILLAC

    monkeypatch.setattr(weather, "geocode", geocode)
    monkeypatch.setattr(maps, "_CACHE", {})
    monkeypatch.setattr(maps, "TRY", None)


@pytest.mark.usefixtures("_cadillac")
def test_a_quote_in_a_name_stays_inside_the_query_string() -> None:
    fake = Overpass()
    asyncio.run(maps.near('Joe\'s "Diner"', "Cadillac", session=fake))  # type: ignore[arg-type]
    assert '["name"~"Joe\'s\\\\ \\"Diner\\"",i]' in fake.queries[0]


@pytest.mark.usefixtures("_cadillac")
def test_a_refused_query_is_not_retried_or_called_busy() -> None:
    fake = Overpass(status=400)
    with pytest.raises(ToolError) as err:
        asyncio.run(maps.near("Walmart", "Cadillac", session=fake))  # type: ignore[arg-type]
    assert "busy" not in str(err.value) and "400" in str(err.value)
    assert len(fake.queries) == 1
