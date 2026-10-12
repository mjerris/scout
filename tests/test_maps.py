"""Maps: nearby places and drive routes from OpenStreetMap, spoken and for Claude.
The map services are replaced by a fake session that answers by URL."""

import asyncio
import json
from typing import Any

import pytest

from scout import local_intents, maps, weather
from scout.mac import ToolError

CADILLAC = {
    "name": "Cadillac",
    "region": "Michigan",
    "country": "United States",
    "latitude": 44.25,
    "longitude": -85.40,
    "timezone": "America/Detroit",
}
LUTHER = {
    "name": "Luther",
    "region": "Michigan",
    "country": "United States",
    "latitude": 44.04,
    "longitude": -85.68,
    "timezone": "America/Detroit",
}


class Resp:
    def __init__(self, data: Any, status: int = 200) -> None:
        self.data, self.status = data, status

    async def __aenter__(self) -> "Resp":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import aiohttp

            raise aiohttp.ClientError(f"HTTP {self.status}")

    async def json(self, content_type: Any = None) -> Any:
        return self.data


class FakeMaps:
    """Overpass, OSRM table and route; `busy` makes Overpass answer 504 that many times."""

    def __init__(self, busy: int = 0) -> None:
        self.busy = busy
        self.calls: list[str] = []

    def request(self, method: str, url: str, **kw: Any) -> Resp:
        self.calls.append(url)
        if "interpreter" in url:
            if self.busy:
                self.busy -= 1
                return Resp(None, 504)
            return Resp({"elements": [
                {"type": "node", "lat": 44.26, "lon": -85.41, "tags": {"amenity": "sanitary_dump_station", "name": "Mitchell State Park Dump", "addr:city": "Cadillac"}},
                {"type": "way", "center": {"lat": 44.30, "lon": -85.45}, "tags": {"amenity": "sanitary_dump_station"}},
                {"type": "node", "lat": 44.40, "lon": -85.60, "tags": {"amenity": "sanitary_dump_station", "name": "Far Away Dump"}},
            ]})  # fmt: skip
        if "/table/" in url:
            return Resp({"durations": [[0, 300, 600, 1500]], "distances": [[0, 1600, 8000, 30000]]})
        if "/route/" in url:
            return Resp({"code": "Ok", "routes": [{"distance": 104607, "duration": 5400}]})
        raise AssertionError(url)

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _places(monkeypatch: pytest.MonkeyPatch) -> None:
    async def geocode(place: str, session: Any = None) -> dict[str, Any]:
        return {
            "cadillac michigan": CADILLAC,
            "cadillac": CADILLAC,
            "luther michigan": LUTHER,
            "luther": LUTHER,
        }[place.lower().replace(",", "")]

    monkeypatch.setattr(weather, "geocode", geocode)
    monkeypatch.setattr(maps, "_CACHE", {})
    monkeypatch.setattr(maps, "TRY", None)


def test_nearest_by_driving_time_named_first() -> None:
    fake = FakeMaps()
    d = asyncio.run(maps.near("dump stations", "Cadillac, Michigan", session=fake))  # type: ignore[arg-type]
    assert d["what"] == "dump station" and d["near"] == "Cadillac, Michigan"
    assert [p["name"] for p in d["places"]] == [
        "Mitchell State Park Dump",
        "Far Away Dump",
        "an unnamed dump station",
    ]
    assert maps.speak_near(d) == (
        "The closest dump station to Cadillac is Mitchell State Park Dump, Cadillac, 1.0 miles, about 5 minutes. "
        "Next is Far Away Dump, 5.0 miles."
    )
    asyncio.run(maps.near("dump station", "Cadillac", session=fake))  # type: ignore[arg-type]
    assert sum("interpreter" in u for u in fake.calls) == 1  # the second search came from the cache


def test_a_busy_map_server_is_retried_then_said_plainly() -> None:
    d = asyncio.run(maps.near("campground", "Luther", session=FakeMaps(busy=1)))  # type: ignore[arg-type]
    assert d["places"]
    with pytest.raises(ToolError, match="busy right now"):
        asyncio.run(maps.near("grocery", "Luther", session=FakeMaps(busy=3)))  # type: ignore[arg-type]


def test_route_is_spoken_and_reported() -> None:
    r = asyncio.run(maps.route("Luther, Michigan", "Cadillac", session=FakeMaps()))  # type: ignore[arg-type]
    assert maps.speak_route(r) == "Luther to Cadillac is 65 miles, about 1 hour 30 minutes by car."


@pytest.mark.parametrize(
    ("text", "kind", "args"),
    [
        ("how far is Cadillac from Luther", "route", ("luther", "cadillac")),
        ("how far from Luther to Cadillac", "route", ("luther", "cadillac")),
        ("how long does it take to drive from Luther to Cadillac", "route", ("luther", "cadillac")),
        ("what's the distance between Luther and Cadillac?", "route", ("luther", "cadillac")),
        ("where's the nearest dump station near Cadillac", "near", ("dump station", "cadillac")),
        ("find the closest laundromat to Luther, Michigan", "near", ("laundromat", "luther michigan")),
    ],
)
def test_spoken_map_questions(text: str, kind: str, args: tuple[str, str]) -> None:
    got: list[tuple[str, str, str]] = []

    async def route(a: str, b: str) -> dict[str, Any]:
        got.append(("route", a, b))
        return {"from": a, "to": b, "miles": 65, "minutes": 90}

    async def near(w: str, p: str) -> dict[str, Any]:
        got.append(("near", w, p))
        return {"what": w, "near": p, "places": [], "radius_miles": 25}

    ctx = local_intents.Context(timers=None, route=route, near=near)
    assert asyncio.run(local_intents.answer(text, ctx))
    assert got == [(kind, *args)]


def test_unusual_places_go_to_claude() -> None:
    async def near(w: str, p: str) -> dict[str, Any]:
        raise AssertionError("tier 0 shouldn't search for a name")

    ctx = local_intents.Context(timers=None, near=near)
    assert asyncio.run(local_intents.answer("where's the nearest Costco near Cadillac", ctx)) is None


def test_bad_places_are_refused() -> None:
    with pytest.raises(ToolError, match="short place name"):
        asyncio.run(maps.route("x" * 200, "Cadillac", session=FakeMaps()))  # type: ignore[arg-type]
    assert json.dumps(maps.category("Dump Stations")) == '"dump station"'
