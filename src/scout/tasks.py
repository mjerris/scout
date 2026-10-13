"""Scout's task list (docs/tasks-design.md): what you owe and what others owe you, in
markdown files (taskfiles.py) through the store, mirrored to Reminders, and ordered by
plain code from fields the AI or you fill in.

The order (score) is deterministic, so it's stable and can say why: importance, how
soon it's due, an urgent flag, quick wins when nothing is pressing, people you've told
Scout about, and older undated tasks sinking. A pin from you overrides it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import index, reminders_mac
from .calendar_mac import parse_time
from .config import load
from .mac import ToolError

log = logging.getLogger(__name__)

IMPORTANCE = {"high": 3, "normal": 2, "low": 1}
EFFORTS = ("small", "medium", "large")
PIN = {"top": 1000.0, "high": 100.0, "low": -100.0}
_MAX_TITLE = 200


@dataclass
class Task:
    n: int
    title: str
    context: str
    fields: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.fields.get("status") or "open")

    @property
    def owner(self) -> str:
        return str(self.fields.get("owner") or "me")

    @property
    def mine(self) -> bool:
        return self.owner.lower() == "me"

    def due(self) -> dt.date | None:
        return _date(self.fields.get("due"))


def _date(v: Any) -> dt.date | None:
    s = str(v or "").strip()[:10]
    try:
        return dt.date.fromisoformat(s) if s else None
    except ValueError:
        return None


def of(h: index.Hit) -> Task:
    return Task(int(h.meta.get("n") or h.id.split(":", 1)[1]), h.title, h.body, dict(h.meta))


def all_tasks(store: index.Store | None = None) -> list[Task]:
    store = store or index.default()
    return [of(h) for h in store.recent(["task"], limit=5000)]


def score(t: Task, today: dt.date, people: Iterable[str] = ()) -> tuple[float, str]:
    """The task's place in the order, and the main reason for it."""
    pin = str(t.fields.get("pin") or "").lower()
    if pin in PIN:
        return PIN[pin], f"you made it {pin} priority"
    imp = IMPORTANCE.get(str(t.fields.get("importance") or "normal").lower(), 2)
    s, why = imp * 10.0, ("important" if imp == 3 else "")
    due = t.due()
    if due is not None:
        days = (due - today).days
        bump = 30 if days < 0 else 25 if days == 0 else 18 if days == 1 else 10 if days <= 7 else 3
        s += bump
        why = (
            f"overdue since {due:%b} {due.day}" if days < 0
            else "due today" if days == 0
            else "due tomorrow" if days == 1
            else f"due {due:%A}" if days <= 7
            else why
        )  # fmt: skip
    if str(t.fields.get("urgency") or "").lower() == "urgent":
        s += 8
        why = why or "marked urgent"
    if (due is None or (due - today).days > 7) and str(t.fields.get("effort") or "").lower() == "small":
        s += 3
        why = why or "a quick one"
    text = f"{t.title} {t.owner} {t.context}".lower()
    if any(p.lower() in text for p in people if p):
        s += 5
        why = why or "for someone you've told me about"
    found = _date(t.fields.get("found"))
    if due is None and found is not None and (today - found).days > 14:
        s -= ((today - found).days - 14) / 7
    if not t.mine:
        s -= 15  # what others owe you comes up when it's due or overdue
        why = why or f"waiting on {t.owner}"
    return s, why or "open"


def ordered(
    tasks: Sequence[Task], today: dt.date, people: Iterable[str] = (), who: str = "all"
) -> list[tuple[Task, float, str]]:
    want = [t for t in tasks if t.status == "open"]
    if who == "mine":
        want = [t for t in want if t.mine]
    elif who == "waiting":
        want = [t for t in want if not t.mine]
    people = list(people)
    out = [(t, *score(t, today, people)) for t in want]
    out.sort(key=lambda x: (-x[1], x[0].due() or dt.date.max, x[0].n))
    return out


def people_from_facts(facts: Iterable[str]) -> list[str]:
    """Names in remembered facts ("Pat is my manager" -> Pat): their tasks rank higher."""
    out: list[str] = []
    for f in facts:
        for m in re.finditer(r"\b([A-Z][a-z]{1,20})\b", f):
            name = m.group(1)
            # A capital that only starts the sentence is a name only before "is", "was"...
            first = m.start() == 0 and not re.match(r"^[A-Z][a-z]+ (?:is|was|works)\b", f)
            if not first and name not in _NOT_NAMES and name not in out:
                out.append(name)
    return out


_NOT_NAMES = {"The", "My", "Our", "This", "That", "Dr", "Mr", "Ms", "Mrs"}


def _people() -> list[str]:
    try:
        from . import memory_client

        return people_from_facts(memory_client.memory().newest(200))
    except Exception:  # the memory service is down: no boost
        return []


# --- Reminders mirror ---------------------------------------------------------------------


async def _mirror_add(t_title: str, fields: dict[str, Any], n: int) -> str | None:
    cfg = load().tasks
    if not cfg.mirror:
        return None
    mine = str(fields.get("owner") or "me").lower() == "me"
    title = t_title if mine else f"{fields.get('owner')}: {t_title}"
    a: dict[str, Any] = {"title": title[:200], "list": cfg.mirror_list if mine else cfg.waiting_list,
                         "notes": f"Scout task {n}"}  # fmt: skip
    if fields.get("due"):
        a["due"] = str(fields["due"])
    try:
        r = await reminders_mac.add_data(a)
    except ToolError as exc:
        log.info("task %d not mirrored to Reminders: %s", n, exc)
        return None
    return str(r["id"]) if r.get("id") else None


async def _mirror_done(t: Task) -> None:
    rid = str(t.fields.get("reminder") or "")
    if not rid:
        return
    title = t.title if t.mine else f"{t.owner}: {t.title}"
    try:
        await reminders_mac.complete({"id": rid, "title": title[:200]})
    except ToolError as exc:
        log.info("task %d's reminder not marked done: %s", t.n, exc)


async def sync_from_reminders(store: index.Store | None = None) -> int:
    """Tasks whose mirrored reminder was completed in Reminders become done."""
    cfg = load().tasks
    if not cfg.mirror:
        return 0
    store = store or index.default()
    open_by_rid = {
        str(t.fields["reminder"]): t for t in await asyncio.to_thread(all_tasks, store)
        if t.status == "open" and t.fields.get("reminder")
    }  # fmt: skip
    if not open_by_rid:
        return 0
    done = 0
    for name in {cfg.mirror_list, cfg.waiting_list}:
        try:
            data = await reminders_mac.reminder_data(name, include_completed=True)
        except ToolError:
            continue
        for r in data.get("reminders", []):
            t = open_by_rid.get(str(r.get("id")))
            if t is not None and r.get("completed"):
                await asyncio.to_thread(store.task_update, t.n, {"status": "done", "done": _today_s()})
                done += 1
    return done


def _today_s() -> str:
    return dt.date.today().isoformat()


# --- tools ----------------------------------------------------------------------------------


def _fields(a: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if a.get("due"):
        out["due"] = parse_time(str(a["due"]), "due").date().isoformat()
    for k, allowed in (("importance", IMPORTANCE), ("effort", EFFORTS), ("urgency", ("urgent", "normal"))):
        if a.get(k):
            v = str(a[k]).lower()
            if v not in allowed:
                raise ToolError(f"{k} must be one of {', '.join(allowed)}")
            out[k] = v
    if a.get("owner"):
        out["owner"] = " ".join(str(a["owner"]).split())[:80] or "me"
    if a.get("source"):
        out["source"] = str(a["source"])[:200]
    return out


async def add(a: dict[str, Any], store: index.Store | None = None) -> dict[str, Any]:
    """A new task (from you, a tool or the farm). Returns {n, title, owner, due}."""
    title = " ".join(str(a.get("title") or "").split())
    if not title:
        raise ToolError("a task needs a title")
    if len(title) > _MAX_TITLE:
        raise ToolError(f"that title is too long (max {_MAX_TITLE} characters)")
    fields = _fields(a)
    store = store or index.default()
    n = await asyncio.to_thread(store.task_add, title, fields, str(a.get("context") or "")[:4000])
    rid = await _mirror_add(title, fields, n)
    if rid:
        await asyncio.to_thread(store.task_update, n, {"reminder": rid})
    return {"n": n, "title": title, "owner": fields.get("owner", "me"), "due": fields.get("due")}


async def add_text(a: dict[str, Any]) -> str:
    t = await add(a)
    who = "" if t["owner"] == "me" else f" (waiting on {t['owner']})"
    return f"Added task {t['n']}: {t['title']}{who}" + (f", due {t['due']}." if t["due"] else ".")


def _get(store: index.Store, n: Any) -> Task:
    try:
        num = int(n)
    except (TypeError, ValueError):
        raise ToolError("id must be a task number from tasks_list") from None
    h = store.get(f"task:{num}")
    if h is None:
        raise ToolError(f"there's no task {num}")
    return of(h)


async def done(a: dict[str, Any], store: index.Store | None = None) -> str:
    store = store or index.default()
    t = await asyncio.to_thread(_get, store, a.get("id"))
    status = "dropped" if a.get("drop") else "done"
    await asyncio.to_thread(store.task_update, t.n, {"status": status, "done": _today_s()})
    await _mirror_done(t)
    return f"{'Dropped' if status == 'dropped' else 'Done'}: task {t.n}, {t.title}."


async def update(a: dict[str, Any], store: index.Store | None = None) -> str:
    store = store or index.default()
    t = await asyncio.to_thread(_get, store, a.get("id"))
    fields = _fields(a)
    if a.get("title"):
        fields["title"] = " ".join(str(a["title"]).split())[:_MAX_TITLE]
    if "pin" in a:
        pin = str(a.get("pin") or "").lower()
        if pin not in ("", "none", *PIN):
            raise ToolError("pin must be top, high, low or none")
        fields["pin"] = None if pin in ("", "none") else pin
    if not fields and not a.get("context"):
        raise ToolError("nothing to change")
    await asyncio.to_thread(store.task_update, t.n, fields, str(a.get("context") or "")[:4000])
    return f"Updated task {t.n}: {fields.get('title') or t.title}."


def _line(t: Task, why: str) -> str:
    due = t.due()
    who = "" if t.mine else f" [waiting on {t.owner}]"
    return f"{t.n}. {t.title}{who}" + (f" (due {due.isoformat()})" if due else "") + f": {why}"


async def list_text(a: dict[str, Any], store: index.Store | None = None) -> str:
    """Open tasks in order; who: mine, waiting or all; query narrows by words and meaning."""
    store = store or index.default()
    who = str(a.get("who") or "all").lower()
    limit = max(1, min(int(a.get("limit") or 10), 50))
    today = dt.date.today()
    tasks = await asyncio.to_thread(all_tasks, store)
    if q := str(a.get("query") or "").strip():
        hits = await store.asearch(q, kinds=["task"], limit=50)
        keep = {int(h.id.split(":", 1)[1]) for h in hits}
        tasks = [t for t in tasks if t.n in keep]
    rows = ordered(tasks, today, await asyncio.to_thread(_people), who)[:limit]
    if not rows:
        return "No open tasks." if not a.get("query") else "No open tasks match."
    return "\n".join(_line(t, why) for t, _, why in rows)


async def why_text(a: dict[str, Any], store: index.Store | None = None) -> str:
    store = store or index.default()
    t = await asyncio.to_thread(_get, store, a.get("id"))
    src = t.fields.get("source") or "added by hand"
    return f"Task {t.n}: {t.title}\nsource: {src}\nfound: {t.fields.get('found', '?')}\n\n{t.context or '(no context)'}"


def speak_top(rows: Sequence[tuple[Task, float, str]], n: int = 3) -> str:
    """'What should I work on?' as a short spoken answer."""
    mine = [(t, why) for t, _, why in rows if t.mine][:n]
    if not mine:
        return "There's nothing on your list."
    first, rest = mine[0], mine[1:]
    out = f"First, {first[0].title}: {first[1]}."
    if rest:
        out += " Then " + "; then ".join(f"{t.title}" for t, _ in rest) + "."
    return out
