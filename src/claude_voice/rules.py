"""'Yes, always' approvals, saved per voice agent in state/voice_allow.json."""

from __future__ import annotations

import json
import re
import uuid
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_DESTRUCTIVE = re.compile(r"send|post|delete|remove|create|write|update|merge|publish|pay|transfer|drop")

# Tools whose "always" would be far broader than the one call approved; for
# these, "always" counts as a one-time yes.
NEVER_ALWAYS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


def rule_for(tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """The narrowest saved rule that covers this call, or None if it can't be saved."""
    if tool in NEVER_ALWAYS:
        return None
    if tool == "Bash":
        cmd = (args.get("command") or "").strip()
        return {"tool": "Bash", "command": cmd} if cmd else None
    if tool in ("WebFetch", "mcp__voice_app__open_url", "mcp__voice_app__new_tab"):
        host = urlparse(args.get("url", "")).netloc.lower()
        return {"tool": tool, "domain": host} if host else None
    if tool.startswith("mcp__"):
        # A blanket "always" for a tool that sends, posts or deletes would cover every
        # future call with any arguments; those stay one-time approvals.
        if _DESTRUCTIVE.search(tool.split("__")[-1]):
            return None
        return {"tool": tool}
    return None


def describe(rule: dict[str, Any]) -> str:
    if rule["tool"] == "Bash":
        return f"run: {rule['command']}"
    if "domain" in rule:
        verb = "fetch pages from" if rule["tool"] == "WebFetch" else "open pages on"
        return f"{verb} {rule['domain']}"
    parts = rule["tool"].split("__")
    return f"use {parts[-1].replace('_', ' ')}" + (f" ({parts[1]})" if len(parts) > 2 else "")


class Rules:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict[str, Any]] = []
        if path.exists():
            try:
                self.items = json.loads(path.read_text())
            except (OSError, ValueError):
                self.items = []
        for item in self.items:
            item.setdefault("id", uuid.uuid4().hex[:12])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1))
        tmp.replace(self.path)

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        want = rule_for(tool, args)
        if want is None:
            return False
        return any({k: v for k, v in r.items() if k not in ("added", "id")} == want for r in self.items)

    def add(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        rule = rule_for(tool, args)
        if rule is None:
            return None
        if not self.matches(tool, args):
            self.items.append({**rule, "added": time.time(), "id": uuid.uuid4().hex[:12]})
            self._save()
        return rule

    def remove(self, rule_id: str) -> None:
        before = len(self.items)
        self.items = [r for r in self.items if r.get("id") != rule_id]
        if len(self.items) != before:
            self._save()

    def listing(self) -> list[dict[str, Any]]:
        return [{"id": r["id"], "text": describe(r), "added": r.get("added")} for r in self.items]
