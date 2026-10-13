"""The AI call ledger: which tier answered each request, Claude's cost and tokens, and
the spoken summary."""

import asyncio
import datetime as dt
import time
from pathlib import Path
from types import SimpleNamespace

from scout import ledger, local_intents


def test_records_and_sums_by_tier(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    ledger.record("0", 0.01, p)
    ledger.record("1", 0.6, p)
    result = SimpleNamespace(total_cost_usd=0.0321, num_turns=2, is_error=False,
                             usage={"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 9000,
                                    "cache_creation_input_tokens": 30000})  # fmt: skip
    ledger.record("claude", 3.2, p, **ledger.from_result(result))
    ledger.record("claude", 2.0, p, cost_usd=0.01)
    s = ledger.summary(0, p)
    assert s == {
        "requests": 4,
        "tier0": 1,
        "tier1": 1,
        "claude": 2,
        "cost_usd": 0.0421,
        "tokens": 40280,
        "claude_median_s": 3.2,
    }
    row = ledger.rows(0, p)[2]
    assert (row["input_tokens"], row["cache_write_tokens"], row["cache_read_tokens"]) == (1200, 30000, 9000)
    assert ledger.tokens(row) == 40280
    assert (
        ledger.speak("today", s)
        == "Today: 4 requests, 2 answered here on the Mac and 2 by Claude, about 0.04 dollars of usage and about 40 thousand tokens."
    )
    assert ledger.speak("week", ledger.summary(time.time() + 10, p)) == "No requests this week yet."


def test_periods_start_at_midnight_monday_and_the_first() -> None:
    now = dt.datetime(2026, 10, 14, 15, 30).astimezone()  # a Wednesday
    assert dt.datetime.fromtimestamp(ledger.start_of("today", now)).day == 14
    assert dt.datetime.fromtimestamp(ledger.start_of("week", now)).day == 12
    assert dt.datetime.fromtimestamp(ledger.start_of("month", now)).day == 1


def test_spoken_usage_question() -> None:
    ledger.record("0", 0.01)
    ctx = local_intents.Context(timers=None)
    out = asyncio.run(local_intents.answer("how much have you spent today", ctx))
    assert out == "Today: 1 request, 1 answered here on the Mac, none needed Claude."
