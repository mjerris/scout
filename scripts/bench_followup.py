"""Benchmark the follow-up rewrite for local recall (followup.py) on a synthetic store.

    uv run python scripts/bench_followup.py [model] [--no-semantic] [--json PATH]

Uses scripts/bench_recall.py's synthetic store (its 21 records, one a lure, among 300
distractors) and two-turn conversations over it: the first turn (what was asked, what
Scout said, the record it was about) goes into the refs log as the app records it; the
second is a follow-up ("what did she say about hiring?"), run the way the app runs it
(tier1.LocalModel.decide, then recall.answer). Each follow-up is run three ways: as
asked (no rewrite, today's behavior), rewritten by the refs then the model (followup.py),
and rewritten by the model alone.

Reports, per way:
- answers: answerable follow-ups answered locally with the expected fact; "wrong" counts
  a local answer without it;
- false-local: follow-ups that should go to Claude (no answer in the store, an action or
  judgment, a lure) answered locally. Must be 0;
- latency: the rewrite itself (p50/p95) and decide + recall end to end on follow-ups.

And for bench_recall.py's 100 ordinary requests: how many plain code takes for follow-ups
(each would pay a rewrite), and what the detection costs the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_recall as br  # noqa: E402

from scout import followup, index, recall, refs, tier1  # noqa: E402


@dataclass
class Case:
    asked: str  # the first turn: what the user asked
    said: str  # ...what Scout answered
    ids: tuple[str, ...]  # ...and the records it was about
    text: str  # the follow-up
    keys: tuple[str, ...] = ()  # one must be in the answer; () = should go to Claude
    group: str = ""


BUDGET = (
    "what did Pat say about the Q4 budget",
    "Pat said the Q4 budget is approved at 48,000 dollars.",
    ("mail:1",),
)
VENDOR = (
    "what was decided in the vendor meeting",
    "They decided to go with Acme for the fiber install.",
    ("meeting:1",),
)
FURNACE = ("who is the furnace technician", "Your technician is Luis.", ("mail:2",))
FLIGHT = (
    "what's the confirmation code for my Denver flight",
    "The confirmation code is HXQ7PL.",
    ("mail:3",),
)
OIL = ("when did I last change the generator oil", "You changed it on September 14.", ("note:1",))
PROPANE = ("how much did the propane refill cost", "It was 41 dollars.", ("note:3",))
HIRING = ("when does Pat want the hiring plan", "Pat wants the hiring plan by October 20.", ("meeting:2",))
DOG = ("what's the name of the dog at site 14", "The dog at site 14 is Biscuit.", ("note:6",))
STORAGE = ("what's my storage unit combination", "The combination is 3-19-27.", ("note:5",))
TWO = ("anything from Pat or Delta", "Pat wrote about the Q4 budget, and Delta sent your flight confirmation.",
       ("mail:1", "mail:3"))  # fmt: skip

ANSWERABLE = [
    (BUDGET, "and what's the travel cap?", ("6,000", "6000")),
    (BUDGET, "what did she say about hiring?", ("january",)),
    (BUDGET, "who was that from?", ("pat",)),
    (VENDOR, "who was at that meeting?", ("sam",)),
    (VENDOR, "what about the purchase order?", ("sam", "friday")),
    (FURNACE, "how much will it cost?", ("129",)),
    (FURNACE, "and when is he coming?", ("october 15",)),
    (FLIGHT, "and what time does it depart?", ("7:05", "7 05")),
    (STORAGE, "which unit is it?", ("212",)),
    (OIL, "and when is the next one due?", ("150",)),
    (OIL, "how much oil did it take?", ("1.8", "1 point 8")),
    (("what's the front tire pressure on the RV", "80 psi in the front.", ("memory:2",)), "what about the rear?", ("85",)),
    (PROPANE, "how many gallons was that?", ("14.2", "14 point 2")),
    (HIRING, "what is she worried about?", ("roadmap",)),
    (("which logo did we agree on for the launch page", "The blue logo.", ("slack:2",)), "who said that?", ("alex",)),
    (("what did we decide about the pricing change", "To present it at the November board meeting.", ("meeting:3",)),
     "who owns the slides for that?", ("mike",)),
    (("when is the staging database going down", "Saturday from 9 to 11.", ("slack:1",)), "who mentioned it?", ("jordan",)),
    (("when did I replace the water filter", "On October 2.", ("note:4",)), "what model is it?", ("wf-22", "wf 22")),
    (DOG, "and what's the neighbor's name?", ("carol",)),
    (("what's the travel cap in the Q4 budget", "Travel is capped at 6,000.", ("mail:1",)), "what about hiring?", ("january",)),
    (TWO, "what's the confirmation code in the other one?", ("hxq7pl",)),
    (TWO, "how much is the travel cap in the first one?", ("6,000", "6000")),
]  # fmt: skip

TO_CLAUDE = [
    (BUDGET, "what about Sam?", "no-answer"),
    (BUDGET, "and send it to Sam", "action"),
    (BUDGET, "what do you think about it?", "judgment"),
    (FURNACE, "what about the roof?", "no-answer"),
    (PROPANE, "what about the transmission fluid?", "no-answer"),
    (FLIGHT, "what about my Chicago flight?", "no-answer"),
    (DOG, "and the cat?", "no-answer"),
    (HIRING, "what did she say about the roof?", "no-answer"),
    (VENDOR, "should we switch to the other one?", "judgment"),
    (STORAGE, "and delete that note", "action"),
    (VENDOR, "what about letting Sam know?", "action"),
    (("what did the bank's security alert email say", "That email looked suspicious, so I didn't read it.",
      ("mail:4",)), "what did it ask me to do?", "lure"),
]  # fmt: skip


# Written after the rewrite was tuned on the sets above, and run as they were: a check
# that the numbers above aren't only the tuning (--held-out).
HELD_OUT = [
    (("what's my wifi password", "The password is correct-horse-battery-42.", ("note:2",)),
     "and what's the network called?", ("starfield",)),
    (FLIGHT, "where is it flying from?", ("detroit",)),
    (FLIGHT, "and what's the flight number?", ("1442",)),
    (HIRING, "and when does she want it?", ("october 20",)),
    (STORAGE, "where is it?", ("northside",)),
    (PROPANE, "where did I buy it?", ("tractor supply",)),
    (DOG, "what kind of dog is it?", ("golden retriever",)),
    (FURNACE, "who is it with?", ("comfort heating", "luis")),
    (VENDOR, "and who did they go with?", ("acme",)),
    (OIL, "what oil did it use?", ("10w-30", "10w30", "10w 30")),
]  # fmt: skip
HELD_OUT_CLAUDE = [
    (STORAGE, "what about the gym locker?", "no-answer"),
    (FLIGHT, "and book me a seat on it", "action"),
    (OIL, "what about the truck?", "no-answer"),
    (HIRING, "should she worry about that?", "judgment"),
    (("when is Pat's birthday", "Pat's birthday is March 3.", ("memory:3",)), "and how old is she turning?", "no-answer"),
]  # fmt: skip


def cases(held_out: bool = False) -> list[Case]:
    yes, no = (HELD_OUT, HELD_OUT_CLAUDE) if held_out else (ANSWERABLE, TO_CLAUDE)
    out = [Case(*h, t, k, "answerable") for h, t, k in yes]
    out += [Case(*h, t, (), f"claude-{g}") for h, t, g in no]
    return out


@dataclass
class Result:
    case: Case
    way: str
    route: str
    outcome: str  # "local" or "claude"
    answer: str = ""
    standalone: str = ""
    rewrite_s: float = 0.0
    total_s: float = 0.0
    reason: str = ""

    @property
    def right(self) -> bool:
        if not self.case.keys:
            return (
                self.outcome == "claude" or self.route != "recall"
            )  # another lookup says when it finds nothing
        return self.outcome == "local" and any(k in self.answer.lower() for k in self.case.keys)


_REWRITES: list[tuple[str, float]] = []  # (standalone or "", seconds) for the current case
_standalone = followup.standalone


async def _timed(text: str, log_: refs.Log, store: Any, model: Any) -> followup.Rewrite | None:
    t0 = time.perf_counter()
    got = await _standalone(text, log_, store, model)
    _REWRITES.append((got.question if got else "", time.perf_counter() - t0))
    return got


followup.standalone = _timed  # type: ignore[assignment]  # bench only: time each rewrite
_resolve = followup.resolve


def _history(c: Case) -> refs.Log:
    log_ = refs.Log()
    log_.add(refs.Said(time.time() - 60, "Scout", c.said, c.ids, False, "room", c.asked))
    return log_


async def run(lm: tier1.LocalModel, store: index.Index, c: Case, way: str) -> Result:
    lm.rewrite = way != "as asked"
    followup.resolve = _resolve if way == "refs+model" else (lambda text, turns: None)  # type: ignore[assignment]
    lm.said = _history(c)
    _REWRITES.clear()
    br._LOG.clear()
    t0 = time.perf_counter()
    d = await lm.decide(c.text, br.NOW)
    sa, rw = _REWRITES[-1] if _REWRITES else ("", 0.0)
    if d is None:
        return Result(
            c, way, "claude", "claude", standalone=sa, rewrite_s=rw, total_s=time.perf_counter() - t0
        )
    if d["tool"] != "recall":
        return Result(
            c, way, d["tool"], "local", standalone=sa, rewrite_s=rw, total_s=time.perf_counter() - t0
        )
    args = d["args"]
    got = await recall.answer(
        str(args.get("question") or c.text), str(args.get("query") or ""), lm, store, br.NOW,
        asked=str(args.get("asked") or ""), also=list(args.get("refs") or []),
    )  # fmt: skip
    r = Result(c, way, "recall", "local" if got else "claude", standalone=sa, rewrite_s=rw,
               total_s=time.perf_counter() - t0, reason=br._LOG[-1] if br._LOG else "")  # fmt: skip
    if got is not None:
        r.answer = got.text
    return r


def q(xs: list[float], p: float) -> float:
    return sorted(xs)[min(len(xs) - 1, round(p * (len(xs) - 1)))] if xs else 0.0


def report(results: list[Result]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for way in WAYS:
        rs = [r for r in results if r.way == way]
        ans = [r for r in rs if r.case.keys]
        right = [r for r in ans if r.right]
        wrong = [r for r in ans if r.outcome == "local" and r.route == "recall" and not r.right]
        claude = [r for r in rs if not r.case.keys]
        false_local = [r for r in claude if r.outcome == "local" and r.route == "recall"]
        rewrites = [r.rewrite_s for r in rs if r.rewrite_s]
        model_rw = [r.rewrite_s for r in rs if r.rewrite_s > 0.05]
        totals = [r.total_s for r in rs]
        print(f"\n== {way}")
        print(f"   answered right locally  {br.pct(len(right), len(ans))}  (wrong: {len(wrong)})")
        print(f"   FALSE-LOCAL             {br.pct(len(false_local), len(claude))}")
        print(
            f"   rewrite latency         p50 {q(rewrites, 0.5):.2f}s p95 {q(rewrites, 0.95):.2f}s"
            f" (model calls: {len(model_rw)}, p50 {q(model_rw, 0.5):.2f}s p95 {q(model_rw, 0.95):.2f}s)"
        )
        print(f"   decide+recall           p50 {q(totals, 0.5):.2f}s p95 {q(totals, 0.95):.2f}s")
        for r in rs:
            mark = "ok " if r.right else "WRONG" if r in wrong else "FL!" if r in false_local else "-- "
            sa = f" => {r.standalone!r}" if r.standalone else ""
            what = r.answer or r.reason or r.outcome
            print(f"   {mark} [{r.case.group}] {r.case.text!r}{sa} -> {r.route}: {what[:110]}")
        out[way] = {
            "right": [len(right), len(ans)],
            "wrong": len(wrong),
            "false_local": [len(false_local), len(claude)],
            "rewrite_p50_s": q(rewrites, 0.5),
            "rewrite_p95_s": q(rewrites, 0.95),
            "total_p50_s": q(totals, 0.5),
            "total_p95_s": q(totals, 0.95),
        }
    return out


WAYS = ("as asked", "refs+model", "model only")


async def ordinary(lm: tier1.LocalModel, store: index.Index) -> dict[str, Any]:
    """bench_recall.py's requests: which plain code takes for follow-ups, and what the
    detection costs; those it takes are run with a conversation before them."""
    cs = br.cases()
    t0 = time.perf_counter()
    for _ in range(100):
        flagged = [c for c in cs if followup.is_followup(c.text)]
    per = (time.perf_counter() - t0) / (100 * len(cs))
    print(f"\n== bench_recall.py's {len(cs)} requests")
    print(f"   taken for follow-ups    {len(flagged)}; detection costs {per * 1e6:.0f} µs a request")
    bad = []
    lm.rewrite = True
    followup.resolve = _resolve  # type: ignore[assignment]
    for c in flagged:
        lm.said = _history(cases()[0])
        d = await lm.decide(c.text, br.NOW)
        local = False
        if d is not None and d["tool"] == "recall":
            a = d["args"]
            got = await recall.answer(str(a.get("question") or c.text), str(a.get("query") or ""), lm, store,
                                      br.NOW, asked=str(a.get("asked") or ""), also=list(a.get("refs") or []))  # fmt: skip
            local = got is not None
        verdict = "local" if local else (d or {}).get("tool", "claude")
        print(f"   [{c.group}] {c.text!r} -> {verdict}")
        if c.want == "claude" and local:
            bad.append(c.text)
    print(f"   FALSE-LOCAL among them  {len(bad)}")
    return {"flagged": [c.text for c in flagged], "detect_us": per * 1e6, "false_local": bad}


async def bench(name: str, store: index.Index, held_out: bool = False) -> dict[str, Any]:
    lm = tier1.LocalModel(name, timeout_s=15.0, extra=True)
    lm.store = store
    await lm.load()
    try:
        results = [await run(lm, store, c, way) for c in cases(held_out) for way in WAYS]
        out = report(results)
        if not held_out:
            out["ordinary"] = await ordinary(lm, store)
        out["cases"] = [
            {"way": r.way, "group": r.case.group, "text": r.case.text, "standalone": r.standalone,
             "route": r.route, "outcome": r.outcome, "answer": r.answer, "right": r.right,
             "rewrite_s": round(r.rewrite_s, 3), "total_s": round(r.total_s, 3)}
            for r in results
        ]  # fmt: skip
        return out
    finally:
        lm._pool.shutdown(wait=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default=br.MODELS[0])
    ap.add_argument("--no-semantic", action="store_true")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--held-out", action="store_true", help="the follow-ups written after tuning")
    a = ap.parse_args()
    tmp = ROOT / ".tmp" / "bench_followup"
    store = br.build_store(tmp, not a.no_semantic)
    try:
        out = asyncio.run(bench(a.model, store, a.held_out))
    finally:
        store.close()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
