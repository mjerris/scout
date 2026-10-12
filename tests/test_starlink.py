"""Starlink: the dish's status turned into facts, spoken answers, announcements and
daily usage. Shapes are the dish's own (api_version 43); no network in tests."""

import asyncio
import datetime as dt
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import pytest

from scout import local_intents, starlink

NOW = 1791800000.0

ONLINE: dict[str, Any] = {"dish_get_status": {
    "device_state": {"uptime_s": "223649"},
    "obstruction_stats": {"fraction_obstructed": 0.013464338, "valid_s": 223300.0},
    "alerts": {},
    "downlink_throughput_bps": 110728170.0, "uplink_throughput_bps": 518793.47,
    "pop_ping_latency_ms": 21.657515,
    "upsu_stats": {"dish_power": 89.15689},
    "software_update_state": "IDLE",
    "dl_bandwidth_restricted_reason": "NO_LIMIT", "ul_bandwidth_restricted_reason": "NO_LIMIT",
}}  # fmt: skip

DOWN: dict[str, Any] = {"dish_get_status": {
    "device_state": {"uptime_s": "100"},
    "outage": {"cause": "OBSTRUCTED", "start_timestamp_ns": "1", "duration_ns": "75000000000"},
    "alerts": {},
}}  # fmt: skip


def hist(
    outages: Sequence[tuple[float, float, str]] = (), drop: float = 0.0, current: int = 2000
) -> dict[str, Any]:
    return {"dish_get_history": {
        "current": str(current),
        "pop_ping_drop_rate": [drop] * 900,
        "pop_ping_latency_ms": [25.0] * 900,
        "downlink_throughput_bps": [8_000_000.0] * 900,  # 1 MB/s
        "uplink_throughput_bps": [800_000.0] * 900,  # 0.1 MB/s
        "outages": [
            {"cause": c, "start_timestamp_ns": str(int(s * 1e9)), "duration_ns": str(int(d * 1e9))}
            for s, d, c in outages
        ],
    }}  # fmt: skip


def test_online_status_is_spoken_with_recent_dropouts() -> None:
    f = starlink.facts(ONLINE, hist([(NOW - 600, 2.1, "NO_PINGS"), (NOW - 7200, 1.0, "NO_DOWNLINK")]), NOW)
    assert f["online"] and f["outage"] is None and len(f["outages_hour"]) == 1
    assert starlink.speak(f) == (
        "Starlink is online: 22 millisecond latency, moving 111 megabits down right now. "
        "1 short dropout in the last hour, the longest 2 seconds."
    )


def test_an_outage_says_why() -> None:
    f = starlink.facts(DOWN, None, NOW)
    assert not f["online"]
    assert starlink.speak(f) == "Starlink is down: obstructed, for 75 seconds so far."


def test_why_slow_names_the_causes_or_clears_starlink() -> None:
    bad = starlink.facts(
        ONLINE, hist([(NOW - 60 * i, 1.0, "OBSTRUCTED") for i in range(1, 7)], drop=0.05), NOW
    )
    said = starlink.speak(bad, "slow")
    assert (
        said.startswith("Starlink: 5 percent of pings dropped")
        and "6 dropouts in the last hour (obstructed)" in said
    )
    quiet = dict(ONLINE["dish_get_status"], downlink_throughput_bps=2e6)
    ok = starlink.facts({"dish_get_status": quiet}, hist(), NOW)
    assert starlink.speak(ok, "slow").startswith("Starlink looks healthy")


def test_report_for_claude_has_exact_values() -> None:
    text = starlink.report(starlink.facts(ONLINE, hist(), NOW), {"today": 2.5e9, "month": 40e9})
    assert "latency 21.657515 ms" in text and "(current traffic, not capacity)" in text
    assert "data used today 2.5 gigabytes, this month 40.0 gigabytes" in text


def test_announces_long_outages_recovery_and_new_alerts() -> None:
    w = starlink.Watch(outage_after_s=60)
    on = starlink.facts(ONLINE, None, NOW)
    brief = starlink.facts(
        dict(
            DOWN,
            dish_get_status=dict(
                DOWN["dish_get_status"], outage={"cause": "NO_PINGS", "duration_ns": "5000000000"}
            ),
        ),
        None,
        NOW,
    )
    assert w.update(on, NOW) == []
    assert w.update(brief, NOW + 30) == []  # 5 s in: not yet
    assert w.update(brief, NOW + 90) == ["Starlink is down: no pings."]
    assert w.update(brief, NOW + 120) == []  # said once
    assert w.update(on, NOW + 200) == ["Starlink's back, after 3 minutes."]
    hot = starlink.facts(
        {
            "dish_get_status": dict(
                ONLINE["dish_get_status"], alerts={"thermal_throttle": True, "is_heating": True}
            )
        },
        None,
        NOW,
    )
    assert w.update(hot, NOW + 300) == ["Starlink alert: thermal throttle."]  # is_heating is ignored
    assert w.update(hot, NOW + 330) == []
    assert w.update(None, NOW + 400) == []  # unreachable: wait
    assert w.update(None, NOW + 470) == ["I can't reach the Starlink dish."]


@pytest.mark.parametrize(
    ("spec", "hour", "quiet"),
    [("22:00-08:00", 23, True), ("22:00-08:00", 7, True), ("22:00-08:00", 12, False), ("", 3, False), ("09:00-17:00", 10, True)],
)  # fmt: skip
def test_quiet_hours(spec: str, hour: int, quiet: bool) -> None:
    assert starlink.in_quiet_hours(spec, dt.datetime(2026, 10, 12, hour, 30)) is quiet


def test_usage_counts_each_second_once(tmp_path: Path) -> None:
    u = starlink.Usage(tmp_path / "usage.json")
    day = dt.date(2026, 10, 12)
    first = u.add(hist(current=2000), day)  # first look: the last minute only
    assert first == pytest.approx(60 * 1.1e6)
    assert u.add(hist(current=2300), day) == pytest.approx(300 * 1.1e6)
    assert u.add(hist(current=2300), day) == 0.0
    assert u.add(hist(current=5000), day) == pytest.approx(900 * 1.1e6)  # a gap: only what the ring holds
    assert u.summary(day) == {"today": pytest.approx(1260 * 1.1e6), "month": pytest.approx(1260 * 1.1e6)}


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ("is starlink down", "Starlink is online"),
        ("how's the starlink", "Starlink is online"),
        ("are we online", "Starlink is online"),
        ("is the internet out", "Starlink is online"),
        ("why is the internet so slow", "Starlink looks healthy"),
        ("how much data have we used today", "Starlink today: 2.5 gigabytes."),
    ],
)
def test_starlink_questions_are_answered_locally(text: str, start: str) -> None:
    async def status() -> dict[str, Any]:
        quiet = dict(ONLINE["dish_get_status"], downlink_throughput_bps=2e6)
        return starlink.facts({"dish_get_status": quiet}, hist(), NOW)

    ctx = local_intents.Context(
        timers=None,
        starlink_on=lambda: True,
        starlink=status,
        starlink_usage=lambda: {"today": 2.5e9, "month": 0.0},
    )
    out = asyncio.run(local_intents.answer(text, ctx))
    assert out is not None and out.startswith(start), out


def test_starlink_questions_go_to_claude_when_it_is_off() -> None:
    async def status() -> dict[str, Any]:
        raise AssertionError("turned off")

    ctx = local_intents.Context(timers=None, starlink_on=lambda: False, starlink=status)
    for text in ("is starlink down", "why is the internet so slow", "how much data have we used"):
        assert asyncio.run(local_intents.answer(text, ctx)) is None, text
