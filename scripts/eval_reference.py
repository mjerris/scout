"""How well the offline reference finds things: known-item and topic queries over the real
library (docs/reference.md), scored by MRR, recall@5 and latency. It needs kiwix-serve
running on the archives (the app starts it) and reads only public archives: nothing here
is Scout's data.

    uv run python scripts/eval_reference.py sample         # draw new known-item queries
    nice -n 10 uv run python scripts/eval_reference.py run # score reference.Library.search

The queries are in scripts/reference_eval.json:

  known   an article's own title (random Wikipedia articles from the most-read and the full
          archive, Stack Exchange questions, LibreTexts pages; drawn by `sample` from
          kiwix-serve's /random). A result is right when its title is the target's, in any
          archive (the same article in Wikipedia top and Wikipedia all counts). Section
          numbers ("1.2:") are not part of a LibreTexts title.
  topics  questions a person would ask, each with the articles that answer it, chosen by
          reading them. A result is right when it matches one `accept` entry: its title
          matches every pattern in `title` (case-insensitive, searched for) and, when
          given, its archive name matches `book`. Nothing a method returned is accepted
          only because the method returned it.

Reported per group: MRR over the top 5 (0 when nothing right is there), recall@5 (the share
of queries with a right answer in the top 5), and p50/p95 latency of one search call, over
the last of --passes passes (the earlier ones warm the archives' pages in the OS cache).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0, os.environ.get("SCOUT_SRC") or str(ROOT / "src")
)  # SCOUT_SRC: another checkout's src, to score it

from scout import reference  # noqa: E402

DATA = Path(__file__).with_name("reference_eval.json")
SAMPLES = (  # (group, archive prefix, how many)
    ("wikipedia-top", "wikipedia_en_top_nopic", 50),
    ("wikipedia-all", "wikipedia_en_all_maxi", 50),
    ("stackexchange", "SE", 25),
    ("libretexts", "libretexts.org_en_", 25),
)
_NUMBERING = re.compile(r"^\s*[\d.]+:?\s+")
_LISTING = re.compile(
    r"^(?:list of|lists of|index|outline of|user|portal|category|template|help|talk|file)\b[ :]|tagged/|_page=|"
    r"^questions\b|_|^[\d\W]+$|^(?:back matter|front matter|review questions|detailed licensing|how to use this book|"
    r"important concepts|summary|exercises|introduction|glossary|readings? ?\w*|problems|key terms|references)$",
    re.I,
)  # not articles a person would look for by title


_LISTING_HIT = re.compile(r"^highest voted\b|^questions\b|^(?:category|list of|index of)\b|_page=", re.I)


def norm(title: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", title.lower()))


def random_title(lib: reference.Library, book: reference.Book, rng: random.Random) -> str | None:
    """A random article's title from kiwix-serve's /random (unseeded: `sample` keeps the draw)."""
    try:
        with urllib.request.urlopen(f"{lib.client.base}/random?content={book.id}", timeout=20) as r:
            path = urllib.parse.unquote(r.geturl().split(f"/content/{book.id}/", 1)[1])
        html = lib.client.raw(book.id, path).data.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001  (a bad draw is skipped)
        print("skipped a draw:", exc, file=sys.stderr)
        return None
    title = _NUMBERING.sub("", reference._summary(html, path)[0])
    return title if len(title) >= 3 and not _LISTING.search(title) else None


def sample() -> None:
    lib = reference.library()
    books = lib.books()
    rng = random.Random(0)  # noqa: S311
    known: list[dict[str, str]] = []
    for group, prefix, want in SAMPLES:
        pool = [
            b
            for b in books
            if (
                b.name.endswith(".stackexchange.com_en_all") or b.name.startswith("mathoverflow")
                if prefix == "SE"
                else b.name.startswith(prefix)
            )
        ]
        seen: set[str] = set()
        tries = 0
        while len([k for k in known if k["group"] == group]) < want and tries < want * 20:
            tries += 1
            b = rng.choice(pool)
            title = random_title(lib, b, rng)
            words = len(norm(title or "").split())
            if (
                not title
                or norm(title) in seen
                or words < (3 if group == "stackexchange" else 2 if group == "libretexts" else 1)
            ):
                continue
            seen.add(norm(title))
            known.append({"group": group, "query": title, "title": title, "book": b.name})
    old = json.loads(DATA.read_text()) if DATA.exists() else {}
    DATA.write_text(
        json.dumps({"known": known, "topics": old.get("topics", [])}, indent=1, ensure_ascii=False) + "\n"
    )
    print(f"wrote {len(known)} known-item queries to {DATA}")


def right_known(item: dict[str, str], hit: reference.Result) -> bool:
    return norm(_NUMBERING.sub("", hit.title)) == norm(item["title"])


def right_topic(item: dict, hit: reference.Result) -> bool:
    if _LISTING_HIT.search(hit.title) or "tagged/" in hit.path:
        return False  # a tag page or a listing is never the answer
    for ok in item["accept"]:
        if all(
            re.search(p, hit.title, re.I)
            for p in ([ok["title"]] if isinstance(ok["title"], str) else ok["title"])
        ) and re.search(ok.get("book", ""), hit.book):
            return True
    return False


def run(passes: int, only: str, show: bool) -> None:
    data = json.loads(DATA.read_text())
    items = [("known:" + k["group"], k, right_known) for k in data["known"]] + [
        ("topics", t, right_topic) for t in data["topics"]
    ]
    items = [i for i in items if only in i[0]]
    lib = reference.library()
    if not lib.books():
        raise SystemExit(lib.problem or reference.NO_BOOKS)
    groups: dict[str, list[tuple[float, int | None]]] = {}
    load = os.getloadavg()
    print(f"load before: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
    for n in range(passes):
        last = n == passes - 1
        for group, item, right in items:
            t0 = time.perf_counter()
            hits = lib.search(item["query"], limit=5)
            ms = (time.perf_counter() - t0) * 1000
            rank = next((i for i, h in enumerate(hits, 1) if right(item, h)), None)
            if last:
                groups.setdefault(group, []).append((ms, rank))
                groups.setdefault("all", []).append((ms, rank))
                if show and (rank is None or rank > 1):
                    print(
                        f"  [{group}] {item['query'][:70]!r}: rank {rank}; got "
                        + " | ".join(f"{h.book[:12]}:{h.title[:34]}" for h in hits[:3])
                    )
    load = os.getloadavg()
    print(f"load after: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
    print(f"{'group':26s} {'n':>4s} {'MRR@5':>6s} {'rec@5':>6s} {'p50 ms':>7s} {'p95 ms':>7s}")
    for group, rows in sorted(groups.items(), key=lambda kv: (kv[0] == "all", kv[0])):
        ms = sorted(r[0] for r in rows)
        p95 = ms[min(len(ms) - 1, int(0.95 * len(ms)))]
        mrr = statistics.fmean(1 / r[1] if r[1] else 0 for r in rows)
        rec = statistics.fmean(1 if r[1] else 0 for r in rows)
        print(f"{group:26s} {len(rows):4d} {mrr:6.3f} {rec:6.3f} {statistics.median(ms):7.0f} {p95:7.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sample", "run"])
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--only", default="", help="only groups containing this text (known, topics...)")
    ap.add_argument("--show", action="store_true", help="print the queries not answered at rank 1")
    ap.add_argument("--out", help="write the report to this file instead of the terminal")
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a number in scout.reference first (W_TITLE=0, POOL=40...), to measure what it does",
    )
    a = ap.parse_args()
    for pair in a.set:
        name, _, value = pair.partition("=")
        setattr(reference, name, type(getattr(reference, name))(float(value)))
    if a.out:
        sys.stdout = open(a.out, "w", buffering=1)  # noqa: SIM115  (closed with the process)
    sample() if a.command == "sample" else run(a.passes, a.only, a.show)


if __name__ == "__main__":
    main()
