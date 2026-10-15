"""How well the store finds the record a question is about: recall@k, MRR and nDCG@k
over a golden set (a query and the records it should find), and the similarity of the
right and the wrong hits (what a relevance floor is set from).

The golden set comes from synthetic data (scripts/eval_retrieval.py) or from a store's
own links (harvest): a journal turn's refs line names the records its answer was about,
so its question should find them; a task's found_in link names the meeting, mail or
Slack message it came from, so its title should find that. Only ids and numbers come out
of here: format_metrics never shows a query or a record's text.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import index

KS = (1, 3, 5, 10)
GOLDEN_RELS = {"about": "turn", "found_in": "task"}  # link rel: the tag of the cases it gives


@dataclass(frozen=True)
class Case:
    query: str
    want: frozenset[str]  # the records it should find
    skip: frozenset[str] = frozenset()  # never counted (the record the query came from)
    tag: str = ""


@dataclass
class Run:
    case: Case
    ranked: list[tuple[str, float | None]] = field(default_factory=list)  # (record, similarity)

    @property
    def ids(self) -> list[str]:
        return [id for id, _ in self.ranked]


class Searcher(Protocol):
    def search(
        self,
        query: str,
        kinds: Sequence[str] | None = None,
        since: float | None = None,
        limit: int = 8,
        semantic: bool | None = True,
    ) -> list[index.Hit]: ...


def base_id(id: str) -> str:
    """The record a chunk belongs to ("granola:x#3" is part of "granola:x")."""
    return id.split("#", 1)[0]


def recall_at_k(ranked: Sequence[str], want: set[str] | frozenset[str], k: int) -> float:
    return len(want & set(ranked[:k])) / len(want) if want else 0.0


def reciprocal_rank(ranked: Sequence[str], want: set[str] | frozenset[str]) -> float:
    return next((1 / (i + 1) for i, id in enumerate(ranked) if id in want), 0.0)


def ndcg_at_k(ranked: Sequence[str], want: set[str] | frozenset[str], k: int) -> float:
    """Binary relevance: each wanted record found counts 1/log2(rank + 1)."""
    dcg = sum(1 / math.log2(i + 2) for i, id in enumerate(ranked[:k]) if id in want)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(want), k)))
    return dcg / ideal if ideal else 0.0


def run_case(store: Searcher, case: Case, k: int = 10, semantic: bool | None = True) -> Run:
    """The store's best k records for the case's query: chunks counted as their record,
    the query's own record left out."""
    hits = store.search(case.query, None, None, 4 * k + len(case.skip), semantic)
    out: dict[str, float | None] = {}
    for h in hits:
        id = base_id(h.id)
        if id in case.skip or id in out:
            continue
        out[id] = h.similarity
        if len(out) == k:
            break
    return Run(case, list(out.items()))


def aggregate(runs: Sequence[Run], ks: Sequence[int] = KS) -> dict[str, Any]:
    """Mean recall@k (each k), MRR and nDCG@k (the largest k) over the runs."""
    n = len(runs)
    out: dict[str, Any] = {"n": n}
    if not n:
        return out
    for k in ks:
        out[f"recall@{k}"] = sum(recall_at_k(r.ids, r.case.want, k) for r in runs) / n
    out["mrr"] = sum(reciprocal_rank(r.ids, r.case.want) for r in runs) / n
    kmax = max(ks)
    out[f"ndcg@{kmax}"] = sum(ndcg_at_k(r.ids, r.case.want, kmax) for r in runs) / n
    return out


def score_split(runs: Sequence[Run]) -> dict[str, list[float]]:
    """Similarities (meaning ranked hits only): `right`, the best-ranked right hit of each
    case that found one; `wrong_top`, the top wrong hit of each case; `miss_top`, the top
    hit of each case that found nothing right (what a floor must keep out)."""
    right: list[float] = []
    wrong: list[float] = []
    miss: list[float] = []
    for r in runs:
        good = [s for id, s in r.ranked if id in r.case.want]
        bad = [s for id, s in r.ranked if id not in r.case.want]
        if good and good[0] is not None:
            right.append(good[0])
        if bad and bad[0] is not None:
            wrong.append(bad[0])
        if not good and r.ranked and r.ranked[0][1] is not None:
            miss.append(r.ranked[0][1])
    return {"right": right, "wrong_top": wrong, "miss_top": miss}


def percentiles(xs: Sequence[float]) -> dict[str, float]:
    if not xs:
        return {}
    s = sorted(xs)

    def at(p: float) -> float:
        return s[round(p * (len(s) - 1))]

    return {"min": s[0], "p10": at(0.1), "p25": at(0.25), "p50": at(0.5), "p90": at(0.9), "max": s[-1]}


def harvest(conn: sqlite3.Connection) -> list[Case]:
    """Golden cases from a store's links: each turn's about links and each task's
    found_in link, the source record's title as the query. Both records must exist."""
    rows = conn.execute(
        "SELECT l.src, l.dst, l.rel, s.title FROM links l JOIN docs s ON s.id = l.src "  # noqa: S608 (placeholders only)
        "JOIN docs d ON d.id = l.dst "
        f"WHERE l.rel IN ({','.join('?' * len(GOLDEN_RELS))}) AND trim(s.title) != '' ORDER BY l.src",
        tuple(GOLDEN_RELS),
    ).fetchall()
    by: dict[str, tuple[str, str, set[str]]] = {}
    for src, dst, rel, title in rows:
        by.setdefault(src, (str(title), GOLDEN_RELS[rel], set()))[2].add(base_id(str(dst)))
    return [Case(q, frozenset(want), frozenset({src}), tag) for src, (q, tag, want) in by.items()]


def format_metrics(name: str, m: dict[str, Any]) -> str:
    """One line of numbers (no text from the cases)."""
    parts = [f"{name:<14} n={m.get('n', 0):<4}"]
    parts += [f"{k} {v:.3f}" for k, v in m.items() if k != "n" and isinstance(v, float)]
    return "  ".join(parts)
