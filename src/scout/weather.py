"""Weather from Open-Meteo (open-meteo.com): free, no API key, no account, so nothing
to sign in to or re-authorize. Forecast data from national weather services (NOAA
in the US). Only the place's coordinates leave the Mac.

    forecast(place)   -> dict: current conditions, the next 24 hours, 7 days
    report(place)     -> compact text with exact values (for Claude)
    speak(...)        -> one or two spoken sentences (tier 0)

Places are geocoded with Open-Meteo's geocoder ("Luther, Michigan" picks the
Michigan one) and cached in Scout's data folder. `weather.home` in config.toml is
used when no place is named.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import aiohttp

from .config import DATA, load
from .mac import ToolError

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
PLACES = DATA / "state" / "places.json"
TIMEOUT = aiohttp.ClientTimeout(total=8)

# WMO weather codes, as people say them.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}  # fmt: skip
_COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]


def compass(degrees: float) -> str:
    return _COMPASS[round((degrees % 360) / 45) % 8]


def _clean_place(place: Any) -> str:
    s = re.sub(r"\s+", " ", str(place or "")).strip(" ,.?")
    if len(s) > 80 or any(ord(c) < 32 for c in s):
        raise ToolError("place must be a short place name, like 'Luther, Michigan'")
    return s


def _places() -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(PLACES.read_text())
        return data
    except (OSError, ValueError):
        return {}


async def geocode(place: str, session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    """{name, region, country, latitude, longitude, timezone} for a place name."""
    key = place.lower()
    cached = _places().get(key)
    if cached:
        return dict(cached)
    name, _, region = (p.strip() for p in place.partition(","))
    if not region:  # "luther michigan" (speech has no commas): try the last words as the region
        words = name.split()
        for k in (2, 1):
            if len(words) > k and _is_region(" ".join(words[-k:])):
                name, region = " ".join(words[:-k]), " ".join(words[-k:])
                break
    params = {"name": name, "count": "10", "language": "en", "format": "json"}
    own = session is None
    s = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        async with s.get(GEOCODE, params=params) as r:
            r.raise_for_status()
            results = (await r.json()).get("results") or []
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ToolError(f"couldn't look up {place!r}: {exc}") from None
    finally:
        if own:
            await s.close()
    if region:  # "Luther, Michigan" / "Paris, France" / "Austin, TX"
        reg = region.lower()
        results = [
            x for x in results
            if reg in str(x.get("admin1", "")).lower() or reg in str(x.get("country", "")).lower()
            or reg == str(x.get("country_code", "")).lower() or reg == _STATES.get(str(x.get("admin1", "")), "").lower()
        ] or results  # fmt: skip
    if not results:
        raise ToolError(f"I couldn't find a place called {place}.")
    best = results[0]
    found = {
        "name": best.get("name", name),
        "region": best.get("admin1", ""),
        "country": best.get("country", ""),
        "latitude": best["latitude"],
        "longitude": best["longitude"],
        "timezone": best.get("timezone", "auto"),
    }
    cache = _places()
    cache[key] = found
    try:
        PLACES.parent.mkdir(parents=True, exist_ok=True)
        PLACES.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass
    return found


async def forecast(place: Any = None, session: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    """The forecast for `place` (or weather.home): current, hourly for 24 h, daily for 7 days."""
    where = _clean_place(place) or load().weather.home
    if not where:
        raise ToolError("Which place? Name one, or set weather.home in Scout's config.")
    loc = await geocode(where, session)
    params = {
        "latitude": str(loc["latitude"]),
        "longitude": str(loc["longitude"]),
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,"
        "wind_direction_10m,precipitation",
        "hourly": "temperature_2m,precipitation_probability,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
        "wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "auto",
        "forecast_days": "7",
    }
    own = session is None
    s = session or aiohttp.ClientSession(timeout=TIMEOUT)
    try:
        async with s.get(FORECAST, params=params) as r:
            r.raise_for_status()
            data: dict[str, Any] = await r.json()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ToolError(f"couldn't get the weather: {exc}") from None
    finally:
        if own:
            await s.close()
    data["place"] = loc
    return data


def _label(loc: dict[str, Any]) -> str:
    return ", ".join(p for p in (loc.get("name"), loc.get("region")) if p)


def _hours(data: dict[str, Any]) -> list[dict[str, Any]]:
    h = data.get("hourly") or {}
    now = (data.get("current") or {}).get("time", "")
    rows = []
    for i, t in enumerate(h.get("time") or []):
        if t < now[:13]:
            continue
        rows.append({
            "time": t, "temp": h["temperature_2m"][i], "rain": h["precipitation_probability"][i],
            "code": h["weather_code"][i], "wind": h["wind_speed_10m"][i], "gust": h["wind_gusts_10m"][i],
            "dir": h["wind_direction_10m"][i],
        })  # fmt: skip
        if len(rows) >= 24:
            break
    return rows


def report(data: dict[str, Any]) -> str:
    """Compact text with exact values, for Claude."""
    c, d = data.get("current") or {}, data.get("daily") or {}
    lines = [
        f"Weather for {_label(data['place'])} (Open-Meteo; local time {c.get('time', '')}, {data.get('timezone', '')}):",
        f"Now: {c.get('temperature_2m')}°F (feels {c.get('apparent_temperature')}°F), "
        f"{CODES.get(int(c.get('weather_code', -1)), 'unknown')}, wind {compass(c.get('wind_direction_10m', 0))} "
        f"{c.get('wind_speed_10m')} mph gusting {c.get('wind_gusts_10m')}, precipitation {c.get('precipitation')} in.",
        "Next 24 h, every 3 h: "
        + "; ".join(
            f"{r['time'][11:16]} {r['temp']}°F {CODES.get(int(r['code']), '?')} rain {r['rain']}% wind {compass(r['dir'])} {r['wind']}/{r['gust']} mph"
            for r in _hours(data)[::3]
        ),
    ]
    for i, day in enumerate(d.get("time") or []):
        lines.append(
            f"{day}: {CODES.get(int(d['weather_code'][i]), '?')}, high {d['temperature_2m_max'][i]}°F, "
            f"low {d['temperature_2m_min'][i]}°F, rain {d['precipitation_probability_max'][i]}%, "
            f"wind to {d['wind_speed_10m_max'][i]} gusting {d['wind_gusts_10m_max'][i]} mph"
        )
    return "\n".join(lines)


async def report_for(place: Any = None) -> str:
    return report(await forecast(place))


def speak(data: dict[str, Any], when: str = "now") -> str:
    """One or two spoken sentences. `when`: now, today, tonight, tomorrow, wind, rain."""
    c, d = data.get("current") or {}, data.get("daily") or {}
    where = data["place"].get("name", "")
    if when in ("now", "today"):
        out = (
            f"In {where} it's {round(c.get('temperature_2m', 0))} and {CODES.get(int(c.get('weather_code', -1)), 'unclear')}, "
            f"wind {compass(c.get('wind_direction_10m', 0))} at {round(c.get('wind_speed_10m', 0))}"
        )
        if (c.get("wind_gusts_10m") or 0) >= (c.get("wind_speed_10m") or 0) + 8:
            out += f", gusting {round(c['wind_gusts_10m'])}"
        out += "."
        if d.get("time"):
            out += (
                f" Today's high {round(d['temperature_2m_max'][0])}, low {round(d['temperature_2m_min'][0])}"
                f", {d['precipitation_probability_max'][0]} percent chance of rain."
            )
        return out
    if when == "tonight":
        night = [r for r in _hours(data) if r["time"][11:13] >= "18" or r["time"][11:13] < "06"][:12]
        if not night:
            return speak(data, "now")
        low = min(r["temp"] for r in night)
        rain = max(r["rain"] for r in night)
        wind = max(r["wind"] for r in night)
        return (
            f"Tonight in {where}: down to {round(low)}, {CODES.get(int(night[0]['code']), 'unclear')}, "
            f"winds up to {round(wind)}, {rain} percent chance of rain."
        )
    if when == "wind":
        rows = _hours(data)
        peak = max(rows, key=lambda r: r["gust"]) if rows else None
        out = f"Wind in {where} is {compass(c.get('wind_direction_10m', 0))} at {round(c.get('wind_speed_10m', 0))}"
        out += f", gusting {round(c.get('wind_gusts_10m', 0))}." if c.get("wind_gusts_10m") else "."
        if peak:
            out += (
                f" Strongest in the next day around {_clock(peak['time'])}, gusts to {round(peak['gust'])}."
            )
        return out
    i = 1 if when == "tomorrow" or when == "rain-tomorrow" else 0
    if len(d.get("time") or []) <= i:
        return speak(data, "now")
    day = "Tomorrow" if i else "Today"
    if when.startswith("rain"):
        return f"{day} in {where}: {d['precipitation_probability_max'][i]} percent chance of rain, {CODES.get(int(d['weather_code'][i]), 'unclear')}."
    return (
        f"{day} in {where}: {CODES.get(int(d['weather_code'][i]), 'unclear')}, high {round(d['temperature_2m_max'][i])}, "
        f"low {round(d['temperature_2m_min'][i])}, {d['precipitation_probability_max'][i]} percent chance of rain."
    )


def _is_region(words: str) -> bool:
    w = words.lower()
    return w in _REGIONS or w.upper() in _STATES.values()


def _clock(iso: str) -> str:
    t = dt.datetime.fromisoformat(iso)
    return t.strftime("%-I %p") if t.minute == 0 else t.strftime("%-I:%M %p")


_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}  # fmt: skip

_REGIONS = {s.lower() for s in _STATES} | {
    "canada", "mexico", "england", "scotland", "ireland", "france", "germany", "spain", "italy",
    "japan", "australia", "uk", "usa", "ontario", "quebec", "british columbia",
}  # fmt: skip
