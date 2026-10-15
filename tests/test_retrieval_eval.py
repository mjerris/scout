"""The retrieval measure (retrieval_eval.py, scripts/eval_retrieval.py): recall@k, MRR
and nDCG@k over a golden set, the golden set harvested from a store's links, and the
similarities of right and wrong hits. Synthetic records only."""

import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from scout import index, retrieval_eval as ev

from test_index import FakeEmbedder


def test_recall_at_k_counts_the_share_of_wanted_records_found() -> None:
    assert ev.recall_at_k(["a", "b", "c"], {"b"}, 1) == 0.0
    assert ev.recall_at_k(["a", "b", "c"], {"b"}, 2) == 1.0
    assert ev.recall_at_k(["a", "b", "c"], {"b", "z"}, 3) == 0.5
    assert ev.recall_at_k([], {"b"}, 5) == 0.0


def test_reciprocal_rank_is_one_over_the_first_right_one() -> None:
    assert ev.reciprocal_rank(["a", "b", "c"], {"c", "b"}) == 0.5
    assert ev.reciprocal_rank(["a"], {"c"}) == 0.0


def test_ndcg_is_one_for_a_perfect_ranking_and_less_lower_down() -> None:
    assert ev.ndcg_at_k(["b", "c", "a"], {"b", "c"}, 3) == pytest.approx(1.0)
    got = ev.ndcg_at_k(["a", "b"], {"b"}, 3)
    assert got == pytest.approx(1 / math.log2(3))
    assert ev.ndcg_at_k(["a"], {"b"}, 3) == 0.0


def test_aggregate_averages_over_cases() -> None:
    runs = [
        ev.Run(ev.Case("q1", frozenset({"a"})), [("a", 0.9), ("b", 0.5)]),
        ev.Run(ev.Case("q2", frozenset({"z"})), [("a", 0.7), ("b", 0.6)]),
    ]
    m = ev.aggregate(runs, ks=(1, 5))
    assert m["n"] == 2
    assert m["recall@1"] == 0.5 and m["recall@5"] == 0.5
    assert m["mrr"] == 0.5
    assert m["ndcg@5"] == pytest.approx(0.5)


def test_scores_of_right_and_wrong_hits() -> None:
    runs = [
        ev.Run(ev.Case("q1", frozenset({"a"})), [("b", 0.8), ("a", 0.7)]),
        ev.Run(ev.Case("q2", frozenset({"z"})), [("c", 0.65)]),
        ev.Run(ev.Case("q3", frozenset({"d"})), [("d", None)]),  # ranked by text only
    ]
    s = ev.score_split(runs)
    assert s["right"] == [0.7]  # the best-ranked right hit of each case that found one
    assert s["wrong_top"] == [0.8, 0.65]  # the top wrong hit of each case
    assert s["miss_top"] == [0.65]  # the top hit of each case that found nothing right


def test_percentiles_summarise_a_list() -> None:
    p = ev.percentiles([0.1, 0.2, 0.3, 0.4, 0.5])
    assert p["min"] == 0.1 and p["p50"] == 0.3 and p["max"] == 0.5
    assert ev.percentiles([]) == {}


def _store(tmp_path: Path) -> index.Index:
    ix = index.Index(tmp_path / "scout.db", FakeEmbedder(), min_similarity=0.0)
    ix.put("granola:m1", "meeting", "Vendor review", "We picked Acme for the fiber install.", ts=1)
    ix.put("granola:m1#1", "meeting", "Vendor review", "Pat: Acme can start in November.", ts=1)
    ix.put("mail:7", "mail", "Lease", "Please send the signed lease back by Friday.", ts=2)
    ix.put("task:1", "task", "Send the signed lease", "From mail", ts=3)
    ix.put("task:2", "task", "Book the fiber install with Acme", "From a meeting", ts=3)
    ix.put(
        "journal:2026-10-15T09:00:00",
        "turn",
        "when does Acme start",
        "when does Acme start\nScout: ...",
        ts=4,
    )
    ix.link("task:1", "mail:7", "found_in")
    ix.link("task:2", "granola:m1", "found_in")
    ix.link("task:2", "gone:1", "merged_from")  # not a golden pair: only found_in and about
    ix.link("journal:2026-10-15T09:00:00", "granola:m1", "about")
    ix.link("journal:2026-10-15T09:00:00", "task:2", "about")
    ix.link("task:9", "mail:404", "found_in")  # neither record exists: skipped
    while ix.embed_pending(16):
        pass
    return ix


def test_harvest_reads_query_and_expected_records_from_links(tmp_path: Path) -> None:
    ix = _store(tmp_path)
    cases = {c.query: c for c in ev.harvest(ix._conn)}
    assert set(cases) == {"Send the signed lease", "Book the fiber install with Acme", "when does Acme start"}
    lease = cases["Send the signed lease"]
    assert lease.want == {"mail:7"} and lease.skip == {"task:1"} and lease.tag == "task"
    turn = cases["when does Acme start"]
    assert turn.want == {"granola:m1", "task:2"} and turn.tag == "turn"


def test_a_run_ranks_records_not_their_chunks_and_never_the_query_itself(tmp_path: Path) -> None:
    ix = _store(tmp_path)
    case = ev.Case("Book the fiber install with Acme", frozenset({"granola:m1"}), frozenset({"task:2"}))
    run = ev.run_case(ix, case, k=5)
    ids = [id for id, _ in run.ranked]
    assert "task:2" not in ids  # the query's own record
    assert ids.count("granola:m1") == 1 and not any("#" in id for id in ids)
    assert ids[0] == "granola:m1"


def test_the_report_never_carries_query_or_record_text(tmp_path: Path) -> None:
    ix = _store(tmp_path)
    runs = [ev.run_case(ix, c, k=5) for c in ev.harvest(ix._conn)]
    text = ev.format_metrics("live", ev.aggregate(runs, ks=(1, 5)))
    for word in ("lease", "Acme", "fiber", "granola", "task:"):
        assert word not in text
    assert "recall@5" in text and "mrr" in text


class _Const:
    """Every text the same vector: the order then comes from full text alone."""

    model = "const@4"

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        return np.ones((len(texts), 4), np.float32) / 2.0


def test_runs_report_similarity_from_the_store(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "scout.db", _Const(), min_similarity=0.0)
    ix.put("note:1", "note", "Gate code", "The gate code is 4417.", ts=1)
    while ix.embed_pending(16):
        pass
    run = ev.run_case(ix, ev.Case("gate code", frozenset({"note:1"})), k=5)
    assert run.ranked[0][0] == "note:1" and run.ranked[0][1] == pytest.approx(1.0)
