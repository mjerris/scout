"""Facts point to where they came from (docs/links.md, Facts): answers from a fact carry
its `memory:<id>` ref, the store links a fact to the journal turn it was told in
(`told_in`), older facts are matched to their turn by the upkeep, and "where did that
come from" says when and where it was told. Synthetic facts and turns only."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import urllib.parse
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from scout import index, links, memory, refs, tools
from scout.memory import Memory

BASE = "https://scout.example.ts.net"
DAY = dt.datetime(2026, 10, 12, 14, 0, 0)  # local time
T0 = DAY.timestamp()


def page(id: str) -> str:
    return f"{BASE}/r/{urllib.parse.quote(id, safe='')}"


@pytest.fixture
def mem(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "facts.md")


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index) and s.journal_root is not None
    s.web_base = lambda: BASE
    return s


def _fact(s: index.Index, text: str, ts: float) -> str:
    fid = memory.fact_id(text)
    s.put(fid, "memory", text, text, ts=ts)
    return fid


def _turn_id(ts: float) -> str:
    return f"journal:{dt.datetime.fromtimestamp(ts):%Y-%m-%dT%H:%M:%S}"


async def _run(a: Awaitable[str]) -> str:
    return await a


def _told(s: index.Index, fid: str) -> list[str]:
    return [ln["id"] for ln in s.links(fid) if ln["rel"] == "told_in" and ln["dir"] == "out"]


# --- refs on the answers --------------------------------------------------------------------


def test_fact_ids_are_the_index_ids(mem: Memory) -> None:
    mem.add("Pat is my manager")
    (doc,) = mem.as_documents()
    assert doc["id"] == memory.fact_id("Pat is my manager")
    assert memory.fact_id("Pat is my manager").startswith("memory:")


def test_memory_answers_carry_the_facts_they_spoke(mem: Memory) -> None:
    said = memory.answer("remember that my dentist is Dr. Lee", mem)
    assert said == "Okay, I'll remember that your dentist is Dr. Lee."
    assert refs.refs_of(said) == (memory.fact_id("my dentist is Dr. Lee"),)
    mem.add("Pat is my manager")
    mem.add("Pat likes oat milk")
    who = memory.answer("who is my dentist", mem)
    assert who == "You told me your dentist is Dr. Lee."
    assert refs.refs_of(who) == (memory.fact_id("my dentist is Dr. Lee"),)
    about = memory.answer("what do you know about Pat", mem)
    assert set(refs.refs_of(about)) == {
        memory.fact_id("Pat is my manager"),
        memory.fact_id("Pat likes oat milk"),
    }
    assert len(refs.refs_of(memory.answer("what do you remember", mem))) == 3
    assert refs.refs_of(memory.answer("forget about my dentist", mem)) == ()  # gone: nothing to point at
    assert refs.refs_of(memory.answer("what do you know about Sam", mem)) == ()


def test_the_remember_tool_and_recall_carry_refs(mem: Memory) -> None:
    fns = {name: fn for name, _, _, fn in tools.memory_tools(mem)}
    out = asyncio.run(_run(fns["remember"]({"fact": "Pat is my manager"})))
    fid = memory.fact_id("Pat is my manager")
    assert out == f"Remembered: Pat is my manager (ref {fid})"
    assert refs.from_tool("mcp__scout__remember", {"fact": "Pat is my manager"}, out) == [fid]
    got = asyncio.run(_run(fns["recall"]({"about": "Pat"})))
    assert f"Pat is my manager (ref {fid})" in got
    assert refs.from_tool("recall", {"about": "Pat"}, got) == [fid]


def test_facts_in_claudes_context_carry_their_refs(mem: Memory) -> None:
    from test_routing import make

    mem.add("Pat is my manager")

    async def go() -> str:
        r: Any = make()
        r.a.memory = mem
        return str(await r.a._with_context("when do I meet Pat"))

    assert f"  - Pat is my manager (ref {memory.fact_id('Pat is my manager')})" in asyncio.run(go())


# --- the told_in link -----------------------------------------------------------------------


def test_says_remember() -> None:
    assert links.says_remember("remember that my dentist is Dr. Lee", "")
    assert links.says_remember("Scout, note that the gate code is 4411", "")
    assert links.says_remember("Pat's my boss now", "remember(fact=Pat is my boss)")
    assert links.says_remember("ok", "local_search(query=x) · remember(fact=Pat is my boss)")
    assert not links.says_remember("who is my dentist", "")
    assert not links.says_remember("what do you remember about Pat", "recall(about=Pat)")


def test_a_remember_turn_links_its_fact(store: index.Index) -> None:
    fid = _fact(store, "my dentist is Dr. Lee", T0)
    store.journal_add(T0 + 1, "room", "remember that my dentist is Dr. Lee", "Okay.", refs=[fid])
    told = _turn_id(T0 + 1)
    assert _told(store, fid) == [told]
    # Asking about it later points at it ("about") without moving where it was told.
    store.journal_add(T0 + 90, "room", "who is my dentist", "You told me.", refs=[fid])
    assert _told(store, fid) == [told]
    r = store.resolve(fid)
    assert r is not None and r["url"] == page(told) and r["page"] == page(fid)
    assert [(x["id"], x["kind"]) for x in r["related"] if x["rel"] == "told_in" and x["dir"] == "out"] == [
        (told, "turn")
    ]


def test_a_claude_turn_with_the_remember_tool_links_its_fact(store: index.Index) -> None:
    fid = _fact(store, "Pat is my boss", T0)
    old = _fact(store, "Pat likes oat milk", T0 - 30 * 86400)  # found by a search in the same turn
    store.journal_add(
        T0 + 40, "room", "Pat's my boss now, keep that", "Got it.",
        tools=[("mcp__scout__local_search", {"query": "Pat"}), ("mcp__scout__remember", {"fact": "Pat is my boss"})],
        refs=[old, fid],
    )  # fmt: skip
    assert _told(store, fid) == [_turn_id(T0 + 40)]
    assert _told(store, old) == []  # told a month before this turn


def test_a_question_turn_is_not_where_a_fact_was_told(store: index.Index) -> None:
    fid = _fact(store, "my dentist is Dr. Lee", T0)
    store.journal_add(T0 + 5, "room", "who is my dentist", "You told me.", refs=[fid])
    assert _told(store, fid) == []


def test_told_again_moves_the_link_to_the_new_turn(store: index.Index) -> None:
    fid = _fact(store, "my dentist is Dr. Lee", T0)
    store.journal_add(T0 + 1, "room", "remember that my dentist is Dr. Lee", "Okay.", refs=[fid])
    later = T0 + 2 * 86400
    _fact(store, "my dentist is Dr. Lee", later)  # the same words again: the fact's time moves
    store.journal_add(later + 1, "phone", "remember that my dentist is Dr. Lee", "Okay.", refs=[fid])
    assert _told(store, fid) == [_turn_id(later + 1)]


def test_a_fact_with_no_turn_resolves_to_its_own_page(store: index.Index) -> None:
    fid = _fact(store, "the gate code is 4411", T0)
    r = store.resolve(fid)
    assert r is not None and r["url"] == page(fid) and r["related"] == []
    store.web_base = lambda: ""
    r = store.resolve(fid)
    assert r is not None and r["url"] is None and r["page"] is None


# --- backfill -------------------------------------------------------------------------------


def test_backfill_links_only_a_single_match_and_is_idempotent(store: index.Index) -> None:
    midnight = DAY.replace(hour=0).timestamp()  # facts.md with a date only
    a = _fact(store, "Pat is my manager", midnight)
    store.journal_add(T0, "room", "remember that Pat is my manager", "Okay.")
    b = _fact(store, "the wifi is called Basecamp", midnight)  # said twice that day: which one?
    store.journal_add(T0 + 60, "room", "note that the wifi is called Basecamp", "Okay.")
    store.journal_add(T0 + 120, "phone", "remember the wifi is called Basecamp", "Okay.")
    c = _fact(store, "Sam is my plumber", T0 + 300)  # by the tool: its words in the tools line
    store.journal_add(T0 + 301, "room", "Sam fixes my pipes, keep it", "Got it.",
                      tools=[("mcp__scout__remember", {"fact": "Sam is my plumber"})])  # fmt: skip
    store.journal_add(T0 + 302, "room", "remember Sam is my plumber", "Okay.")  # the tool's turn wins
    d = _fact(store, "the boat is blue", (DAY - dt.timedelta(days=3)).timestamp())  # no turn that day
    store.journal_add(T0 + 400, "room", "remember the boat is blue", "Okay.")
    e = _fact(store, "Lee owns the bakery", T0 + 500)  # its turn is earlier than the fact
    store.journal_add(T0 + 10, "room", "remember Lee owns the bakery", "Okay.")
    assert store.link_facts() == 2
    assert _told(store, a) == [_turn_id(T0)]
    assert _told(store, c) == [_turn_id(T0 + 301)]
    assert _told(store, b) == _told(store, d) == _told(store, e) == []
    before = {f: store.links(f) for f in (a, b, c, d, e)}
    assert store.link_facts() == 0
    assert {f: store.links(f) for f in (a, b, c, d, e)} == before


def test_backfill_matches_a_long_fact_cut_in_the_tools_line(store: index.Index) -> None:
    text = "my sister Jo lives in Portland and her kids are Max and Ivy and she teaches violin downtown"
    f = _fact(store, text, T0)
    store.journal_add(T0 + 5, "room", "Jo stuff, keep it", "Got it.", tools=[("remember", {"fact": text})])
    assert "…" in (store.get(_turn_id(T0 + 5)) or index.Hit("", "", "", "", "", 0, {})).body
    assert store.link_facts() == 1 and _told(store, f) == [_turn_id(T0 + 5)]


def test_upkeep_runs_the_backfill(store: index.Index) -> None:
    f = _fact(store, "Pat is my manager", T0)
    store.journal_add(T0 + 5, "room", "remember that Pat is my manager", "Okay.")
    store.upkeep()
    assert _told(store, f) == [_turn_id(T0 + 5)]


def test_told_turn_rules() -> None:
    def turn(id: str, q: str, tools: str = "", ts: float = T0 + 5) -> tuple[str, float, str, str]:
        return (id, ts, q, tools)

    assert links.told_turn("Pat is my manager", T0, [turn("j1", "remember that Pat is my manager")]) == "j1"
    assert links.told_turn("Pat is my manager", T0, [turn("j1", "who is Pat")]) is None
    assert links.told_turn("Pat is my manager", T0, [turn("j1", "remember to call Pat")]) is None  # few words
    two = [turn("j1", "remember Pat is my manager"), turn("j2", "note that Pat is my manager")]
    assert links.told_turn("Pat is my manager", T0, two) is None
    tool = [turn("j1", "remember Pat is my manager"), turn("j2", "ok", "remember(fact=Pat is my manager)")]
    assert links.told_turn("Pat is my manager", T0, tool) == "j2"


# --- saying where it came from --------------------------------------------------------------


class FactStore:
    """index.Index's get() and the contract's resolve() for two facts: one told in a turn
    in the room, one saved by hand."""

    def __init__(self) -> None:
        told = memory.fact_id("my dentist is Dr. Lee")
        hand = memory.fact_id("the gate code is 4411")
        turn = _turn_id(T0 + 1)
        self.hits = {
            told: index.Hit(told, "memory", "my dentist is Dr. Lee", "", "", T0, {}),
            hand: index.Hit(hand, "memory", "the gate code is 4411", "", "", T0, {}),
            turn: index.Hit(turn, "turn", "remember that my dentist is Dr. Lee", "", "room", T0 + 1, {}),
        }
        self.resolved: dict[str, dict[str, Any]] = {
            told: {"id": told, "kind": "memory", "title": "my dentist is Dr. Lee", "url": page(turn),
                   "page": page(told), "private": False,
                   "related": [{"id": turn, "rel": "told_in", "dir": "out", "kind": "turn",
                                "title": "remember that my dentist is Dr. Lee", "url": page(turn)}]},
            hand: {"id": hand, "kind": "memory", "title": "the gate code is 4411", "url": None,
                   "page": None, "private": False, "related": []},
        }  # fmt: skip

    def get(self, id: str) -> index.Hit | None:
        return self.hits.get(id)

    def resolve(self, id: str) -> dict[str, Any] | None:
        return self.resolved.get(id)


def test_where_a_fact_came_from_is_said() -> None:
    s = FactStore()
    told = s.resolved[memory.fact_id("my dentist is Dr. Lee")]
    assert asyncio.run(refs.where_from(told, s)) == "You told me on October 12, in the room."
    assert refs.best_url(told) == page(_turn_id(T0 + 1))
    s.hits[_turn_id(T0 + 1)].who = "phone"
    assert asyncio.run(refs.where_from(told, s)) == "You told me on October 12, on the phone."
    hand = s.resolved[memory.fact_id("the gate code is 4411")]
    assert asyncio.run(refs.where_from(hand, s)) == "It's a fact you saved on October 12."


def test_link_to_a_fact_with_no_link_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from scout import local_intents

    s = FactStore()
    fid = memory.fact_id("the gate code is 4411")
    log = refs.Log()
    log.add(refs.Said(time.time(), "Scout", "You told me the gate code is 4411.", (fid,)))
    ctx = local_intents.Context(tools.Timers(lambda label: None), store=lambda: s, said=lambda: log)
    out = asyncio.run(local_intents.answer("give me a link to that", ctx))
    assert out == "It's a fact you saved on October 12. It has no link."
    assert ctx.refs == [fid]


def test_open_link_names_a_fact_and_where_it_was_told() -> None:
    s = FactStore()
    told = s.resolved[memory.fact_id("my dentist is Dr. Lee")]
    out = refs.describe(told, "strict")
    assert "something you told me to remember: my dentist is Dr. Lee" in out
    assert f"link: {page(_turn_id(T0 + 1))}" in out
    assert f"from: {_turn_id(T0 + 1)} (a past conversation)" in out


def test_the_record_page_names_the_turn_a_fact_was_told_in(store: index.Index) -> None:
    from scout import recordpage

    fid = _fact(store, "my dentist is Dr. Lee", T0)
    store.journal_add(T0 + 1, "room", "remember that my dentist is Dr. Lee", "Okay.", refs=[fid])
    r = store.resolve(fid)
    assert r is not None
    out = recordpage.render(r, store.get(fid))
    assert "Told in" in out and recordpage.page_href(_turn_id(T0 + 1)) in out
