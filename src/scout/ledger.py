"""The AI call ledger: one line per request in state/ledger.jsonl, saying which tier
answered (0 plain code, 1 the local model, claude), how long it took, and for Claude
the cost, turns and tokens. It shows whether the local tiers really keep Claude rare.

"How much have you spent today?" and "how many did you answer yourself this week?"
read it in plain code.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA

log = logging.getLogger(__name__)
PATH = DATA / "state" / "ledger.jsonl"
_lock = threading.Lock()


def record(tier: str, latency_s: float, path: Path | None = None, **extra: Any) -> None:
    """tier: "0", "1" or "claude"; extra: cost_usd, turns, input_tokens, output_tokens..."""
    row = {"ts": round(time.time(), 3), "tier": tier, "latency_s": round(latency_s, 3), **extra}
    p = path or PATH
    try:
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(row) + "\n")
    except OSError as exc:
        log.warning("ledger not written: %s", exc)


def from_result(result: Any) -> dict[str, Any]:
    """The useful numbers from a Claude ResultMessage."""
    usage = getattr(result, "usage", None) or {}
    return {
        "cost_usd": round(float(getattr(result, "total_cost_usd", 0) or 0), 5),
        "turns": getattr(result, "num_turns", None),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "error": bool(getattr(result, "is_error", False)),
    }


def rows(since: float, path: Path | None = None) -> list[dict[str, Any]]:
    out = []
    try:
        with (path or PATH).open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def summary(since: float, path: Path | None = None) -> dict[str, Any]:
    rs = rows(since, path)
    by = {t: [r for r in rs if r.get("tier") == t] for t in ("0", "1", "claude")}
    claude = by["claude"]
    lat = sorted(r["latency_s"] for r in claude) if claude else []
    return {
        "requests": len(rs),
        "tier0": len(by["0"]),
        "tier1": len(by["1"]),
        "claude": len(claude),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in claude), 4),
        "claude_median_s": lat[len(lat) // 2] if lat else None,
    }


def start_of(period: str, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now().astimezone()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        day -= dt.timedelta(days=day.weekday())
    elif period == "month":
        day = day.replace(day=1)
    return day.timestamp()


def speak(period: str, s: dict[str, Any]) -> str:
    when = {"today": "today", "week": "this week", "month": "this month"}[period]
    if not s["requests"]:
        return f"No requests {when} yet."
    local = s["tier0"] + s["tier1"]
    n = s["requests"]
    out = f"{when.capitalize()}: {n} request{'s' * (n != 1)}, {local} answered here on the Mac"
    out += f" and {s['claude']} by Claude" if s["claude"] else ", none needed Claude"
    if s["claude"]:
        out += (
            f", about {s['cost_usd']:.2f} dollars of usage"
            if s["cost_usd"] >= 0.01
            else ", under a cent of usage"
        )
    return out + "."
