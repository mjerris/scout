"""Places and driving: free OpenStreetMap services, no key, nothing to sign in to.

    near(what, place)    -> the closest matches (Overpass), with driving distance/time
    route(origin, dest)  -> driving distance and time (OSRM)

Towns are found with the weather geocoder (cached); addresses and other names with
Nominatim. Only place names and coordinates leave the Mac. Driving times are for a
car: they don't know an RV's height or weight (the dash GPS does).
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

import aiohttp

from . import weather
from .mac import ToolError

# The public Overpass server is sometimes too busy (504): retry it once, then a mirror.
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
TRY = aiohttp.ClientTimeout(total=12)
OSRM = "https://router.project-osrm.org"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "Scout/1.0 (personal voice assistant; github.com/mjerris/scout)"}
TIMEOUT = aiohttp.ClientTimeout(total=25)
MILES = 1609.344

# What people ask for -> OpenStreetMap tag filters (any one matches).
CATEGORIES: dict[str, list[str]] = {
    "gas": ['["amenity"="fuel"]'],
    "diesel": ['["amenity"="fuel"]["fuel:diesel"="yes"]'],
    "truck stop": ['["amenity"="fuel"]["hgv"="yes"]'],
    "dump station": ['["amenity"="sanitary_dump_station"]'],
    "water": ['["amenity"="water_point"]'],
    "campground": ['["tourism"="camp_site"]', '["tourism"="caravan_site"]'],
    "rv park": ['["tourism"="caravan_site"]'],
    "grocery": ['["shop"="supermarket"]', '["shop"="grocery"]'],
    "hardware": ['["shop"="hardware"]', '["shop"="doityourself"]'],
    "laundromat": ['["shop"="laundry"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "hospital": ['["amenity"="hospital"]'],
    "urgent care": ['["amenity"="clinic"]', '["healthcare"="clinic"]'],
    "vet": ['["amenity"="veterinary"]'],
    "restaurant": ['["amenity"="restaurant"]'],
    "coffee": ['["amenity"="cafe"]'],
    "rest area": ['["highway"="rest_area"]'],
    "post office": ['["amenity"="post_office"]'],
    "library": ['["amenity"="library"]'],
    "atm": ['["amenity"="atm"]'],
    "dog park": ['["leisure"="dog_park"]'],
}
ALIASES = {
    "gas station": "gas", "fuel": "gas",
    "gas stations": "gas", "dump": "dump station", "dump stations": "dump station", "rv dump": "dump station",
    "sani dump": "dump station", "potable water": "water", "water fill": "water", "campgrounds": "campground",
    "campsite": "campground", "camping": "campground", "rv parks": "rv park", "groceries": "grocery",
    "grocery store": "grocery", "supermarket": "grocery", "hardware store": "hardware", "laundry": "laundromat",
    "drugstore": "pharmacy", "er": "hospital", "emergency room": "hospital", "clinic": "urgent care",
    "veterinarian": "vet", "animal hospital": "vet", "restaurants": "restaurant", "food": "restaurant",
    "cafe": "coffee", "coffee shop": "coffee", "rest stop": "rest area", "truck stops": "truck stop",
}  # fmt: skip


def category(what: str) -> str | None:
    w = " ".join(str(what).lower().split()).removeprefix("a ").removeprefix("an ")
    w = ALIASES.get(w, w)
    if w in CATEGORIES:
        return w
    if w.endswith("s") and w[:-1] in CATEGORIES:
        return w[:-1]
    return None


def _check(text: Any, what: str) -> str:
    s = " ".join(str(text or "").split()).strip(" ,.?")
    if not s or len(s) > 100 or any(ord(c) < 32 for c in s):
        raise ToolError(f"{what} must be a short place name or address")
    return s


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _ql(text: str) -> str:
    """Text inside an Overpass QL "string" (re.escape leaves quotes alone)."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


class _Refused(ToolError):
    """The service refused the request itself (a 4xx other than 429): retrying won't help."""


async def _json(s: aiohttp.ClientSession, method: str, url: str, **kw: Any) -> Any:
    try:
        async with s.request(method, url, headers=HEADERS, **kw) as r:
            r.raise_for_status()
            return await r.json(content_type=None)
    except aiohttp.ClientResponseError as exc:
        if 400 <= exc.status < 500 and exc.status != 429:
            raise _Refused(f"the map service refused the request: {exc.status} {exc.message}") from None
        raise ToolError(f"the map service didn't answer: {exc}") from None
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ToolError(f"the map service didn't answer: {exc}") from None


async def _overpass(s: aiohttp.ClientSession, query: str) -> Any:
    for n, url in enumerate(OVERPASS):
        try:
            return await _json(s, "POST", url, data={"data": query}, timeout=TRY)
        except _Refused:
            raise
        except ToolError:
            if n == 0:
                await asyncio.sleep(2)
    raise ToolError("The map service is busy right now. Try again in a minute.")


CACHE_S = 86400.0  # places don't move: reuse a search for a day
_CACHE: dict[str, tuple[float, Any]] = {}
_last_nominatim = 0.0


async def locate(place: str, s: aiohttp.ClientSession) -> dict[str, Any]:
    """{name, latitude, longitude} for a town (weather geocoder) or an address (Nominatim)."""
    global _last_nominatim
    try:
        loc = await weather.geocode(place, s)
        return {"name": weather._label(loc), "latitude": loc["latitude"], "longitude": loc["longitude"]}
    except ToolError:
        pass
    wait = 1.0 - (time.monotonic() - _last_nominatim)  # Nominatim's policy: at most one a second
    if wait > 0:
        await asyncio.sleep(wait)
    _last_nominatim = time.monotonic()
    found = await _json(
        s, "GET", NOMINATIM, params={"q": place, "format": "jsonv2", "limit": "1", "countrycodes": "us,ca"}
    )
    if not found:
        raise ToolError(f"I couldn't find {place} on the map.")
    f = found[0]
    return {
        "name": str(f.get("display_name", place)).split(",")[0],
        "latitude": float(f["lat"]),
        "longitude": float(f["lon"]),
    }


def _address(tags: dict[str, str]) -> str:
    street = " ".join(p for p in (tags.get("addr:housenumber"), tags.get("addr:street")) if p)
    return ", ".join(p for p in (street, tags.get("addr:city")) if p)


async def near(
    what: Any,
    place: Any,
    radius_miles: float = 25,
    limit: int = 5,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """The closest `what` (a category like dump station, or a name like Walmart) to `place`."""
    w = _check(what, "what")
    where = _check(place, "near")
    radius = max(1.0, min(float(radius_miles or 25), 60.0)) * MILES
    cat = category(w)
    filters = CATEGORIES[cat] if cat else [f'["name"~"{_ql(re.escape(w))}",i]']
    own = session is None
    s = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        loc = await locate(where, s)
        lat, lon = loc["latitude"], loc["longitude"]
        parts = "".join(f"nwr{f}(around:{radius:.0f},{lat},{lon});" for f in filters)
        q = f"[out:json][timeout:20];({parts});out center tags 60;"
        key = f"{q}"
        cached = _CACHE.get(key)
        if cached and time.time() - cached[0] < CACHE_S:
            data = cached[1]
        else:
            data = await _overpass(s, q)
            _CACHE[key] = (time.time(), data)
        places = []
        for e in data.get("elements", []):
            tags = e.get("tags") or {}
            plat = e.get("lat") or (e.get("center") or {}).get("lat")
            plon = e.get("lon") or (e.get("center") or {}).get("lon")
            if plat is None or plon is None:
                continue
            name = tags.get("name") or tags.get("brand") or tags.get("operator")
            places.append({
                "named": bool(name), "name": name or f"an unnamed {cat or w}", "address": _address(tags), "latitude": plat, "longitude": plon,
                "miles": round(miles_between((lat, lon), (plat, plon)), 1),
                "hours": tags.get("opening_hours", ""), "phone": tags.get("phone") or tags.get("contact:phone", ""),
            })  # fmt: skip
        # Named places first (an unnamed shop says little), then the nearest.
        places.sort(key=lambda p: (not p["named"], p["miles"]))
        places = places[:limit]
        if places:  # driving distance and time from the place, one call for all
            coords = ";".join(
                f"{p['longitude']},{p['latitude']}" for p in [{"longitude": lon, "latitude": lat}, *places]
            )
            table = await _json(
                s,
                "GET",
                f"{OSRM}/table/v1/driving/{coords}",
                params={"sources": "0", "annotations": "duration,distance"},
            )
            durs, dists = (table.get("durations") or [[]])[0], (table.get("distances") or [[]])[0]
            for i, p in enumerate(places, start=1):
                if i < len(durs) and durs[i] is not None:
                    p["drive_minutes"] = round(durs[i] / 60)
                    p["drive_miles"] = (
                        round(dists[i] / MILES, 1) if i < len(dists) and dists[i] is not None else None
                    )
            places.sort(key=lambda p: (not p["named"], p.get("drive_minutes", 10**6)))
        return {
            "what": cat or w,
            "near": loc["name"],
            "places": places,
            "radius_miles": round(radius / MILES),
        }
    finally:
        if own:
            await s.close()


async def route(origin: Any, dest: Any, session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    a, b = _check(origin, "from"), _check(dest, "to")
    own = session is None
    s = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        la, lb = await locate(a, s), await locate(b, s)
        url = f"{OSRM}/route/v1/driving/{la['longitude']},{la['latitude']};{lb['longitude']},{lb['latitude']}"
        data = await _json(s, "GET", url, params={"overview": "false"})
        if data.get("code") != "Ok" or not data.get("routes"):
            raise ToolError(f"I couldn't find a driving route from {la['name']} to {lb['name']}.")
        r = data["routes"][0]
        return {"from": la["name"], "to": lb["name"], "miles": round(r["distance"] / MILES), "minutes": round(r["duration"] / 60),
                "straight_miles": round(miles_between((la["latitude"], la["longitude"]), (lb["latitude"], lb["longitude"])))}  # fmt: skip
    finally:
        if own:
            await s.close()


def _dur(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if not h:
        return f"{m} minutes"
    return f"{h} hour{'s' if h > 1 else ''}" + (f" {m} minutes" if m else "")


def _short(name: str) -> str:
    return name.split(",")[0]


def speak_near(d: dict[str, Any]) -> str:
    ps = d["places"]
    what = d["what"]
    if not ps:
        return f"I didn't find any {what} within {d['radius_miles']} miles of {_short(d['near'])}."
    first = ps[0]
    out = f"The closest {what} to {_short(d['near'])} is {first['name']}"
    if first.get("address"):
        out += f", {first['address']}"
    if first.get("drive_minutes") is not None:
        out += f", {first.get('drive_miles') or first['miles']} miles, about {_dur(first['drive_minutes'])}."
    else:
        out += f", {first['miles']} miles away."
    if len(ps) > 1:
        nxt = ps[1]
        out += f" Next is {nxt['name']}, {nxt.get('drive_miles') or nxt['miles']} miles."
    return out


def speak_route(r: dict[str, Any]) -> str:
    return (
        f"{_short(r['from'])} to {_short(r['to'])} is {r['miles']} miles, about {_dur(r['minutes'])} by car."
    )


def _line(p: dict[str, Any]) -> str:
    return (
            f"- {p['name']}" + (f", {p['address']}" if p["address"] else "")
            + f": {p.get('drive_miles') or p['miles']} mi" + (f", {p['drive_minutes']} min drive" if p.get("drive_minutes") is not None else "")
            + (f"; hours {p['hours']}" if p["hours"] else "") + (f"; phone {p['phone']}" if p["phone"] else "")
            + f" ({p['latitude']:.5f}, {p['longitude']:.5f})"
    )  # fmt: skip


def report_near(d: dict[str, Any]) -> str:
    lines = [
        f"{d['what']} near {d['near']} (OpenStreetMap, within {d['radius_miles']} miles; drive times are for a car):"
    ]
    lines.extend(_line(p) for p in d["places"])
    return "\n".join(lines) if d["places"] else lines[0] + "\nnothing found"


async def near_text(a: dict[str, Any]) -> str:
    return report_near(
        await near(a.get("what"), a.get("near"), a.get("radius_miles") or 25, int(a.get("limit") or 5))
    )


async def route_text(a: dict[str, Any]) -> str:
    r = await route(a.get("origin"), a.get("destination"))
    return f"Driving {r['from']} to {r['to']}: {r['miles']} miles, {r['minutes']} minutes for a car (OSRM; ignores RV size limits); straight line {r['straight_miles']} miles."
