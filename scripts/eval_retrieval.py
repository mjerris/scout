"""Measure how well Scout's store finds what a question is about (retrieval only, no model).

    uv run python scripts/eval_retrieval.py [--sets synthetic,long,recall,live] [--live PATH]
                                            [--floor] [--json PATH]

Prints recall@k (k = 1, 3, 5, 10), MRR and nDCG@10 per golden set, numbers only: no
query or record text is ever printed. Per-item detail (ids, ranks, similarities; the
synthetic sets' queries too) goes to .tmp/eval_retrieval/detail-<set>.jsonl.

The sets:
- synthetic: a year of 20,050 short turns with 50 hand-written questions
  (scripts/retrieval_synth.py), the measure behind index.py's "top 5 for 78-82%".
- long: 100 meeting summaries in markdown (four same-titled weekly syncs for each of 25
  projects, each covering four of the same eight sections), among 500 short turns; 50
  questions about one section of one project, answered by its meetings that cover it.
- recall: scripts/bench_recall.py's store; its answerable questions want the records that
  hold an expected answer word, and its unanswerable ones show how close a wrong record
  gets (what recall's floor keeps out).
- live: harvested from a COPY of the real store (state/memory.db, copied read-only into
  .tmp/eval_retrieval/live.db): each journal turn's refs line (its about links) gives its
  question as a query and the records it names as the answer; each task's found_in link
  gives its title as a query and the meeting, mail or Slack message it came from as the
  answer. The record a query came from is never counted. Task pairs don't depend on
  search (the farm read the source itself); turn pairs partly do (a local answer cites a
  record search found; a Claude turn's refs include what local_search returned), so they
  catch a search getting worse more than they say how good it is.

Growing the live set: it grows by itself as Scout works. Every local answer and every
Claude turn that used records writes a refs line in the journal, and every task the farm
finds keeps its source; rerun this script to pick them up. To add a pair by hand, give a
journal entry a `refs: <id>, <id>` line (ids from the record pages) for a question it
should have found, or set a task's `- source:` field; both become links when the store
next reads the file.

--floor prints the similarity of right and wrong hits (searched without the store's own
bar) and, for each candidate floor, the share of right hits kept and of top hits on
questions with nothing right kept out: what recall's floor ([claude]
recall_min_similarity) is set from. The synthetic stores are kept under
.tmp/eval_retrieval/ and reused: opening one with a new window scheme re-embeds its long
items the way the app does (index.WINDOWS_VERSION).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scout import index  # noqa: E402
from scout import retrieval_eval as ev  # noqa: E402

TMP = ROOT / ".tmp" / "eval_retrieval"
NOW = time.time()
LIVE = Path(
    os.environ.get("SCOUT_HOME") or Path.home() / "Library" / "Application Support" / "Scout"
) / "state" / "memory.db"
FLOORS = [round(0.5 + 0.01 * i, 2) for i in range(26)]


def _open(name: str, fill: Any, key: str) -> index.Index:
    """A synthetic store, built once (`key` names its data) and reused; embedded fully."""
    db = TMP / f"{name}.db"
    meta = TMP / f"{name}.key"
    if not (db.exists() and meta.exists() and meta.read_text() == key):
        for p in TMP.glob(f"{name}.db*"):
            p.unlink()
        ix = index.Index(db, index.LocalEmbedder())
        fill(ix)
        meta.write_text(key)
    else:
        ix = index.Index(db, index.LocalEmbedder())
    t0 = time.perf_counter()
    n = 0
    while got := ix.embed_pending(256):
        n += got
    if n:
        print(f"  {name}: embedded {n} items in {time.perf_counter() - t0:.0f}s")
    return ix


def synthetic() -> tuple[index.Index, list[ev.Case]]:
    import retrieval_synth as rs

    items = [(r, a, age, None) for r, a, age in rs.corpus()]
    items += [(r, a, age, i) for i, (r, a, _, _, age) in enumerate(rs.NEEDLES)]
    items.sort(key=lambda x: -x[2])
    want: dict[int, str] = {}
    for n, (_, _, _, nd) in enumerate(items):
        if nd is not None:
            want[nd] = f"turn:{n}"

    def fill(ix: index.Index) -> None:
        for n, (req, rep, age, _) in enumerate(items):
            ix.put(f"turn:{n}", "turn", title=req, body=f"{req}\nScout: {rep}", ts=NOW - age * 86400)

    ix = _open("synthetic", fill, "synthetic-v1")
    cases = [ev.Case(q, frozenset({want[i]}), tag=kind) for i, (_, _, q, kind, _) in enumerate(rs.NEEDLES)]
    return ix, cases


def long() -> tuple[index.Index, list[ev.Case]]:
    import retrieval_synth as rs

    docs, needles = rs.long_docs()
    short = rs.corpus()[:500]

    def fill(ix: index.Index) -> None:
        for id, title, body, weeks in docs:
            ix.put(id, "meeting", title, body, ts=NOW - weeks * 7 * 86400)
        for n, (req, rep, age) in enumerate(short):
            ix.put(f"turn:{n}", "turn", title=req, body=f"{req}\nScout: {rep}", ts=NOW - age * 86400)

    ix = _open("long", fill, "long-v2")
    return ix, [ev.Case(q, want, tag=section) for q, want, section in needles]


def recall_set() -> tuple[index.Index, list[ev.Case]]:
    import bench_recall as br

    records = br.RECORDS + br.distractors(300)

    def fill(ix: index.Index) -> None:
        for id_, kind, title, body, who, days, meta in records:
            ix.put(id_, kind, title, body, who, br.ago(days), meta)

    ix = _open("recall", fill, "recall-v1")
    cases = []
    for q, keys in br.ANSWERABLE:
        want = {r[0] for r in br.RECORDS if any(k in f"{r[2]} {r[3]}".lower() for k in keys)}
        cases.append(ev.Case(q, frozenset(want), tag="answerable"))
    cases += [ev.Case(q, frozenset(), tag="unanswerable") for q in br.UNANSWERABLE]
    return ix, cases


def live(src: Path, reuse: bool) -> tuple[index.Index, list[ev.Case]]:
    db = TMP / "live.db"
    if not (reuse and db.exists()):
        for p in TMP.glob("live.db*"):
            p.unlink()
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(db)
        with d:
            s.backup(d)
        s.close()
        d.close()
    ix = index.Index(db, index.LocalEmbedder())
    t0 = time.perf_counter()
    n = 0
    while got := ix.embed_pending(256):
        n += got
    if n:
        print(f"  live: embedded {n} items in {time.perf_counter() - t0:.0f}s")
    return ix, ev.harvest(ix._conn)


def _detail(name: str, runs: list[ev.Run], with_text: bool) -> None:
    with (TMP / f"detail-{name}.jsonl").open("w") as f:
        for r in runs:
            d: dict[str, Any] = {
                "tag": r.case.tag,
                "want": sorted(r.case.want),
                "skip": sorted(r.case.skip),
                "ranked": [[id, None if s is None else round(s, 4)] for id, s in r.ranked],
                "rank": next((i + 1 for i, id in enumerate(r.ids) if id in r.case.want), None),
            }
            if with_text:
                d["query"] = r.case.query
            f.write(json.dumps(d) + "\n")


def _floor_table(name: str, runs: list[ev.Run]) -> dict[str, Any]:
    s = ev.score_split(runs)
    print(f"  {name} similarity (searched with no bar):")
    for k, xs in s.items():
        p = ev.percentiles(xs)
        print(f"    {k:<10} n={len(xs):<4} " + " ".join(f"{a} {b:.3f}" for a, b in p.items()))
    right, miss = s["right"], s["miss_top"]
    print("    floor  right kept  nothing-right kept out")
    rows = []
    for f in FLOORS:
        kept = sum(x >= f for x in right) / len(right) if right else 0.0
        out = sum(x < f for x in miss) / len(miss) if miss else 1.0
        rows.append({"floor": f, "right_kept": kept, "miss_out": out})
        if round(f * 100) % 2 == 0:
            print(f"    {f:.2f}   {kept:6.0%}      {out:6.0%}")
    return {"split": s, "table": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="synthetic,long,recall,live")
    ap.add_argument("--live", type=Path, default=LIVE, help="the store to copy (read-only)")
    ap.add_argument("--reuse-live", action="store_true", help="use the copy already under .tmp/")
    ap.add_argument("--floor", action="store_true", help="similarity of right and wrong hits")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"windows_version": index.WINDOWS_VERSION}
    for name in a.sets.split(","):
        t0 = time.perf_counter()
        if name == "live":
            ix, cases = live(a.live, a.reuse_live)
        else:
            ix, cases = {"synthetic": synthetic, "long": long, "recall": recall_set}[name]()
        scored = [c for c in cases if c.want]
        runs = [ev.run_case(ix, c, k=10) for c in scored]
        res: dict[str, Any] = {"all": ev.aggregate(runs)}
        print(ev.format_metrics(name, res["all"]))
        for tag in sorted({c.tag for c in scored}):
            sub = [r for r in runs if r.case.tag == tag]
            res[tag] = ev.aggregate(sub)
            print("  " + ev.format_metrics(tag, res[tag]))
        _detail(name, runs, with_text=name != "live")
        if a.floor:
            bar = ix.min_similarity
            ix.min_similarity = 0.0
            raw = [ev.run_case(ix, c, k=10) for c in cases]
            ix.min_similarity = bar
            res["floor"] = _floor_table(name, raw)
        print(f"  ({time.perf_counter() - t0:.0f}s)")
        out[name] = res
        ix.close()
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
