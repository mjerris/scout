"""Weather: Open-Meteo answers turned into text for Claude and speech for tier 0,
and the everyday weather questions answered without Claude. No network in tests."""

import asyncio
from typing import Any

import pytest

from scout import local_intents, weather

DATA: dict[str, Any] = {
    "place": {"name": "Luther", "region": "Michigan", "country": "United States"},
    "timezone": "America/Detroit",
    "current": {"time": "2026-10-10T13:30", "temperature_2m": 74.5, "apparent_temperature": 69.7,
                "weather_code": 2, "wind_speed_10m": 13.0, "wind_gusts_10m": 28.0,
                "wind_direction_10m": 185, "precipitation": 0.0},
    "hourly": {
        "time": [f"2026-10-{10 + (13 + i) // 24:02d}T{(13 + i) % 24:02d}:00" for i in range(30)],
        "temperature_2m": [73 - i * 0.6 for i in range(30)],
        "precipitation_probability": [1] * 30,
        "weather_code": [3] * 30,
        "wind_speed_10m": [12 - i * 0.1 for i in range(30)],
        "wind_gusts_10m": [26 - i * 0.3 for i in range(30)],
        "wind_direction_10m": [180] * 30,
    },
    "daily": {"time": ["2026-10-10", "2026-10-11"], "weather_code": [3, 61],
              "temperature_2m_max": [76.6, 75.1], "temperature_2m_min": [51.4, 55.6],
              "precipitation_probability_max": [11, 60], "wind_speed_10m_max": [13.7, 10.1],
              "wind_gusts_10m_max": [30.9, 22.8], "sunrise": ["", ""], "sunset": ["", ""]},
}  # fmt: skip


def test_report_for_claude_has_exact_values() -> None:
    text = weather.report(DATA)
    assert text.startswith("Weather for Luther, Michigan")
    assert "wind south 13.0 mph gusting 28.0" in text and "2026-10-11: light rain, high 75.1°F" in text
    assert (
        "Next 24 h, every 3 h: 13:00 73.0°F cloudy rain 1% wind south 12.0/26.0 mph" in text
    )  # exact for Claude


@pytest.mark.parametrize(
    ("when", "spoken"),
    [
        ("now", "In Luther it's 74 and partly cloudy, wind south at 13, gusting 28. Today's high 77, low 51, 11 percent chance of rain."),
        ("tomorrow", "Tomorrow in Luther: light rain, high 75, low 56, 60 percent chance of rain."),
        ("rain-tomorrow", "Tomorrow in Luther: 60 percent chance of rain, light rain."),
    ],
)  # fmt: skip
def test_spoken_weather(when: str, spoken: str) -> None:
    assert weather.speak(DATA, when) == spoken


def test_tonight_and_wind_use_the_hourly_forecast() -> None:
    assert weather.speak(DATA, "tonight").startswith("Tonight in Luther: down to ")
    assert weather.speak(DATA, "wind") == (
        "Wind in Luther is south at 13, gusting 28. Strongest in the next day around 1 PM, gusts to 26."
    )


@pytest.mark.parametrize(
    ("text", "place", "start"),
    [
        ("What's the weather?", "", "In Luther it's 74"),
        ("what's the weather in Luther, Michigan", "luther michigan", "In Luther it's 74"),
        ("What's the weather forecast today for Luther, Michigan?", "luther michigan", "In Luther it's 74"),  # heard live
        ("what's the wind forecast for tonight", "", "Tonight in Luther"),
        ("will it rain tomorrow", "", "Tomorrow in Luther: 60 percent"),
        ("what's the weather like tomorrow", "", "Tomorrow in Luther: light rain"),
    ],
)  # fmt: skip
def test_weather_questions_are_answered_locally(text: str, place: str, start: str) -> None:
    asked: list[str] = []

    async def fake(p: str) -> dict[str, Any]:
        asked.append(p)
        return DATA

    ctx = local_intents.Context(timers=None, weather=fake)
    out = asyncio.run(local_intents.answer(text, ctx))
    assert out is not None and out.startswith(start), out
    assert asked == [place]


def test_weather_questions_that_need_claude_go_there() -> None:
    async def fake(p: str) -> dict[str, Any]:
        raise AssertionError("not a plain weather question")

    ctx = local_intents.Context(timers=None, weather=fake)
    for text in ("should I bring an umbrella to the game", "what was the weather like last week"):
        assert asyncio.run(local_intents.answer(text, ctx)) is None, text


def test_place_names_are_checked() -> None:
    from scout.mac import ToolError

    with pytest.raises(ToolError):
        weather._clean_place("x" * 81)
    assert weather._clean_place("  Luther,  Michigan? ") == "Luther, Michigan"


def test_spoken_place_names_without_commas_find_their_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Speech has no commas: "luther michigan" must still pick Luther, Michigan."""
    seen: list[dict[str, str]] = []

    class Resp:
        def __init__(self) -> None:
            self.status = 200

        async def __aenter__(self) -> "Resp":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> dict[str, Any]:
            return {"results": [
                {"name": "Luther", "admin1": "Oklahoma", "country": "United States", "country_code": "US", "latitude": 35.6, "longitude": -97.1},
                {"name": "Luther", "admin1": "Michigan", "country": "United States", "country_code": "US", "latitude": 44.04, "longitude": -85.68},
            ]}  # fmt: skip

    class Session:
        def get(self, url: str, params: dict[str, str]) -> Resp:
            seen.append(params)
            return Resp()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(weather, "PLACES", tmp_path / "places.json")
    found = asyncio.run(weather.geocode("luther michigan", Session()))  # type: ignore[arg-type]
    assert seen[0]["name"] == "luther" and found["region"] == "Michigan" and found["latitude"] == 44.04
