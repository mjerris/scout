"""Scout's journal: every exchange, in plain markdown files you can read, edit or back up.

    memory/journal/2026/10/2026-10-13.md

    # Monday, October 13, 2026

    ## 11:28:07 · phone
    **Q:** What's on my calendar tomorrow?
    **A:** Two things: standup at 9 and the dentist at 3.
    tools: calendar_events(start=2026-10-14, end=2026-10-15)

The files are the record; the local index (kind "turn") is built from them and rebuilt
when a file changes, so an edit or a deleted entry is reflected in search. Results Scout
already keeps elsewhere aren't copied in: the tools line names what was used (a mail
id, a place), and an answer built from mail or messages is kept only in "open" privacy
mode (otherwise its tools line keeps only ids). Such an entry's heading ends in
" · private". Only the memory service writes here (one writer, so the room and the phone never
interleave a line).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIVATE_ANSWER = "(answered from mail or messages; not kept here)"
_MAX_ARG = 80  # a tool argument longer than this is cut in the tools line
_ENTRY = re.compile(r"^## (\d\d:\d\d(?::\d\d)?) · ([^\n]*)\n(.*?)(?=^## |\Z)", re.S | re.M)
_DAY = re.compile(r"(\d{4}-\d\d-\d\d)\.md$")
_PRIVATE = " · private"
# What a private entry's tools line keeps outside "open" mode: references, not text.
_ID_KEYS = {"id", "ids", "mail_id", "message_id", "event_id", "note"}
_lock = threading.Lock()


@dataclass
class Entry:
    ts: float
    who: str  # "room", "phone", ...
    q: str
    a: str
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    private: bool = False  # the answer came from mail or messages


def path_for(root: Path, ts: float) -> Path:
    d = dt.datetime.fromtimestamp(ts)
    return root / "journal" / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.md"


def _line(text: str) -> str:
    return " ".join(str(text or "").split())


def tools_line(tools: Iterable[tuple[str, dict[str, Any]]], ids_only: bool = False) -> str:
    """The tools used, with their arguments; `ids_only` keeps only ids, numbers and yes/no."""
    parts = []
    for name, args in tools:
        shown = []
        for k, v in (args or {}).items():
            if v in (None, "", [], {}):
                continue
            if ids_only and k not in _ID_KEYS and not isinstance(v, (int, float)):
                continue
            s = _line(v if isinstance(v, str) else json.dumps(v))
            shown.append(f"{k}={s[: _MAX_ARG - 1] + '…' if len(s) > _MAX_ARG else s}")
        parts.append(f"{name.rsplit('__', 1)[-1]}({', '.join(shown)})")
    return " · ".join(parts)


def render(e: Entry, keep_private: bool) -> str:
    t = dt.datetime.fromtimestamp(e.ts)
    hide = e.private and not keep_private
    answer = PRIVATE_ANSWER if hide else _line(e.a) or "(none)"
    who = (_line(e.who) or "room") + (_PRIVATE if e.private else "")
    out = f"## {t:%H:%M:%S} · {who}\n**Q:** {_line(e.q) or '(none)'}\n**A:** {answer}\n"
    if e.tools:
        out += f"tools: {tools_line(e.tools, ids_only=hide)}\n"
    return out + "\n"


def append(root: Path, e: Entry, keep_private: bool) -> Path:
    """Add one exchange to its day's file (made with a heading if new)."""
    p = path_for(root, e.ts)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            d = dt.datetime.fromtimestamp(e.ts)
            p.write_text(f"# {d:%A, %B} {d.day}, {d:%Y}\n\n")
        last = b""
        with p.open("rb") as f:
            if f.seek(0, 2):
                f.seek(-1, 2)
                last = f.read(1)
        with p.open("a") as f:
            if last not in (b"", b"\n"):  # edited by hand: a new line, or the entry joins the last one
                f.write("\n")
            f.write(render(e, keep_private))
    return p


def parse(p: Path, text: str | None = None) -> list[dict[str, Any]]:
    """A day file's entries as index items (kind "turn"), id journal:<date>T<time>.

    A clock time in the hour that repeats when clocks fall back is read as the first
    pass, until the times in the file go back: from then on, as the second."""
    m = _DAY.search(p.name)
    if not m:
        return []
    day = m.group(1)
    text = p.read_text() if text is None else text
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    prev: float | None = None
    fell_back = False
    for hhmm, who, body in _ENTRY.findall(text):
        clock = hhmm if len(hhmm) == 8 else hhmm + ":00"
        try:
            naive = dt.datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        ts, second = naive.timestamp(), naive.replace(fold=1).timestamp()
        if second != ts and (fell_back or (prev is not None and ts <= prev)):
            fell_back, ts = True, second
        prev = ts
        private = who.endswith(_PRIVATE)
        if private:
            who = who[: -len(_PRIVATE)]
        q = re.search(r"^\*\*Q:\*\*[ \t]*(.*)$", body, re.M)
        a = re.search(r"^\*\*A:\*\*[ \t]*(.*)$", body, re.M)
        tools = re.search(r"^tools:[ \t]*(.*)$", body, re.M)
        key = f"journal:{day}T{clock}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key += f"-{seen[key]}"
        question = q.group(1).strip() if q else ""
        lines = [
            question,
            f"Scout: {a.group(1).strip()}" if a else "",
            f"tools: {tools.group(1)}" if tools else "",
        ]
        out.append(
            {
                "id": key,
                "title": question or body.strip().splitlines()[0] if body.strip() else question,
                "body": "\n".join(x for x in lines if x) or body.strip(),
                "who": who.strip(),
                "ts": ts,
                "meta": {
                    "file": str(p.relative_to(p.parents[3])) if len(p.parents) > 3 else p.name,
                    **({"private": True} if private else {}),
                },
            }
        )
    return out


def day_files(root: Path) -> list[Path]:
    return sorted(p for p in (root / "journal").glob("*/*/*.md") if _DAY.search(p.name))


def file_sig(p: Path) -> str:
    return hashlib.blake2b(p.read_bytes(), digest_size=8).hexdigest()


def id_prefix(p: Path) -> str | None:
    m = _DAY.search(p.name)
    return f"journal:{m.group(1)}T" if m else None
