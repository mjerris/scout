"""Measure what a Claude turn costs the room assistant and the phone's "ask Scout", per
configuration: tokens (uncached input, cache writes, cache reads, output), dollars, time to
the first spoken word and to the end of the turn.

    .venv/bin/python scripts/bench_brain.py [variant ...]     # default: every variant
    .venv/bin/python scripts/bench_brain.py --list

Each variant builds the real Brain (or PhoneBrain) options from your config.toml's
[claude] section, with Scout's own in-process tools (memory tools left out: they need the
memory service), then asks a fixed set of synthetic questions in one session: the first
turn is cold (the prompt cache is written), the next ones warm. One question makes it
call the weather tool. Nothing private is read and no tool output is printed; every
permission prompt is answered no. Results also go to .tmp/bench_brain-<time>.json.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import StreamEvent, SystemMessage

from scout import config
from scout.assistant import PHONE_TOOLS
from scout.brain import Brain, PhoneBrain
from scout.rules import Rules
from scout.tools import Timers, build_server

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"

PROMPTS = [
    "What's 17 times 23?",
    "What's the capital of Australia?",
    "What's the weather in Paris, France right now? Just the temperature.",
]

# name: (phone?, [claude] overrides, extra environment)
VARIANTS: dict[str, tuple[bool, dict[str, Any], dict[str, str]]] = {
    "old": (False, {"setting_sources": ["user"], "plugins": "enabled"}, {}),
    "old-notoolsearch": (
        False,
        {"setting_sources": ["user"], "plugins": "enabled"},
        {"ENABLE_TOOL_SEARCH": "false"},
    ),
    "bare": (False, {"setting_sources": [], "plugins": "none"}, {}),
    "bare-scout": (False, {"setting_sources": [], "plugins": "scout"}, {}),
    "bare-scout-notoolsearch": (
        False,
        {"setting_sources": [], "plugins": "scout"},
        {"ENABLE_TOOL_SEARCH": "false"},
    ),
    "bare-scout-noclaudeai": (
        False,
        {"setting_sources": [], "plugins": "scout"},
        {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    ),
    "bare-scout-lean": (
        False,
        {"setting_sources": [], "plugins": "scout"},
        {
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        },
    ),
    "old-sonnet": (
        False,
        {"setting_sources": ["user"], "plugins": "enabled", "model": "claude-sonnet-5-5"},
        {},
    ),
    "bare-scout-sonnet": (
        False,
        {"setting_sources": [], "plugins": "scout", "model": "claude-sonnet-5-5"},
        {},
    ),
    "config": (False, {}, {}),  # whatever config.toml and the defaults say now
    "phone-old": (True, {"setting_sources": ["user"], "plugins": "enabled"}, {}),
    "phone-config": (True, {}, {}),
}


async def _no(spoken: str, detail: str) -> bool:
    return False


def _brain(phone: bool, overrides: dict[str, Any], env: dict[str, str]) -> Brain:
    cfg = dataclasses.replace(config.load().claude, **overrides)
    server, names = build_server(Timers(lambda label: None))
    rules = Rules(TMP / "bench_rules.json")
    if phone:
        brain: Brain = PhoneBrain(cfg, server, names, PHONE_TOOLS, rules)
    else:
        brain = Brain(cfg, _no, server, names, rules, lambda kind, text: None)
    if env:
        base = brain._options

        def options() -> Any:
            opts = base()
            return dataclasses.replace(opts, env={**opts.env, **env})

        brain._options = options  # type: ignore[method-assign]
    return brain


async def _turn(brain: Brain, text: str, before: dict[str, float]) -> dict[str, Any]:
    """One question; `before` is the session's running cost per model so far (the CLI
    reports running totals), updated in place."""
    client = await brain._ensure()
    t0 = time.monotonic()
    first: float | None = None
    init: dict[str, Any] = {}
    tools: list[str] = []
    await client.query(text)
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            d = msg.data
            init = {
                "model": d.get("model"),
                "tools": len(d.get("tools") or []),
                "mcp_servers": [s.get("name") for s in d.get("mcp_servers") or []],
                "plugins": [p.get("name") for p in d.get("plugins") or []],
                "skills": len(d.get("skills") or []),
                "slash_commands": len(d.get("slash_commands") or []),
            }
        elif isinstance(msg, StreamEvent) and not msg.parent_tool_use_id:
            ev = msg.event
            delta = ev.get("delta", {}).get("type") if ev.get("type") == "content_block_delta" else None
            if first is None and delta == "text_delta":
                first = time.monotonic() - t0
            if ev.get("type") == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                    tools.append(str(block.get("name")))
        elif isinstance(msg, ResultMessage):
            u = msg.usage or {}
            row = {
                "input": int(u.get("input_tokens") or 0),
                "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cost_usd": 0.0,
                "turns": msg.num_turns,
                "first_s": round(first, 2) if first is not None else None,
                "total_s": round(time.monotonic() - t0, 2),
                "error": msg.is_error,
                "tools": tools,
                "cost_by_model": {},
            }
            for model, mu in (msg.model_usage or {}).items():
                total = float(mu.get("costUSD") or 0)
                row["cost_by_model"][model] = round(total - before.get(model, 0.0), 6)
                before[model] = total
            row["cost_usd"] = round(sum(row["cost_by_model"].values()), 6)
            row["in_total"] = row["input"] + row["cache_write"] + row["cache_read"]
            if init:
                row["init"] = init
            return row
    raise RuntimeError("no result")


async def run(name: str) -> list[dict[str, Any]]:
    phone, overrides, env = VARIANTS[name]
    brain = _brain(phone, overrides, env)
    rows = []
    running: dict[str, float] = {}
    try:
        for i, text in enumerate(PROMPTS):
            row = await _turn(brain, text, running)
            row["turn"] = "cold" if i == 0 else f"warm{i}"
            rows.append(row)
    finally:
        await brain.close()
    return rows


def _fmt(name: str, rows: list[dict[str, Any]]) -> list[str]:
    out = []
    for r in rows:
        tools = ",".join(t.rsplit("__", 1)[-1] for t in r["tools"]) or "-"
        out.append(
            f"{name:26} {r['turn']:6} {r['in_total']:>8} {r['input']:>6} {r['cache_write']:>7} "
            f"{r['cache_read']:>8} {r['output']:>6} {r['cost_usd']:>8.4f} "
            f"{r['first_s'] if r['first_s'] is not None else '-':>6} {r['total_s']:>6} "
            f"{'ERR ' if r['error'] else ''}{tools}"
        )
    return out


def main(argv: list[str]) -> int:
    if "--list" in argv:
        print("\n".join(VARIANTS))
        return 0
    names = argv or list(VARIANTS)
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        print(f"unknown variant(s): {unknown}; --list shows them", file=sys.stderr)
        return 2
    TMP.mkdir(exist_ok=True)
    results: dict[str, Any] = {}
    header = (
        f"{'variant':26} {'turn':6} {'in_total':>8} {'input':>6} {'c_write':>7} "
        f"{'c_read':>8} {'output':>6} {'cost$':>8} {'first':>6} {'total':>6} tools"
    )
    print(header, flush=True)
    for name in names:
        try:
            rows = asyncio.run(run(name))
        except Exception as exc:  # report and go on to the next variant
            print(f"{name:26} failed: {type(exc).__name__}: {exc}", flush=True)
            continue
        results[name] = rows
        print("\n".join(_fmt(name, rows)), flush=True)
        if rows and "init" in rows[0]:
            print(f"{'':26} init: {json.dumps(rows[0]['init'])}", flush=True)
    spent = sum(r["cost_usd"] for rows in results.values() for r in rows)
    out = TMP / f"bench_brain-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"prompts": PROMPTS, "results": results, "spent_usd": spent}, indent=1))
    print(f"\ntotal spent: ${spent:.4f}   ({out})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
