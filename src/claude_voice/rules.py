"""'Yes, always' approvals, saved per voice agent in state/voice_allow.json."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Tools whose "always" would be far broader than the one call approved; for
# these, "always" counts as a one-time yes.
NEVER_ALWAYS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


def rule_for(tool: str, args: dict[str, Any]) -> dict | None:
    """The narrowest saved rule that covers this call, or None if it can't be saved."""
    if tool in NEVER_ALWAYS:
        return None
    if tool == "Bash":
        cmd = (args.get("command") or "").strip()
        return {"tool": "Bash", "command": cmd} if cmd else None
    if tool == "WebFetch":
        host = urlparse(args.get("url", "")).netloc.lower()
        return {"tool": "WebFetch", "domain": host} if host else None
    if tool.startswith("mcp__"):
        return {"tool": tool}
    return None


def describe(rule: dict) -> str:
    if rule["tool"] == "Bash":
        return f"run: {rule['command']}"
    if rule["tool"] == "WebFetch":
        return f"fetch pages from {rule['domain']}"
    parts = rule["tool"].split("__")
    return f"use {parts[-1].replace('_', ' ')}" + (f" ({parts[1]})" if len(parts) > 2 else "")


class Rules:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[dict] = []
        if path.exists():
            try:
                self.items = json.loads(path.read_text())
            except (OSError, ValueError):
                self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1))
        tmp.replace(self.path)

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        want = rule_for(tool, args)
        if want is None:
            return False
        return any({k: v for k, v in r.items() if k != "added"} == want for r in self.items)

    def add(self, tool: str, args: dict[str, Any]) -> dict | None:
        rule = rule_for(tool, args)
        if rule is None:
            return None
        if not self.matches(tool, args):
            self.items.append({**rule, "added": time.time()})
            self._save()
        return rule

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]
            self._save()

    def listing(self) -> list[dict]:
        return [{"index": i, "text": describe(r), "added": r.get("added")} for i, r in enumerate(self.items)]
