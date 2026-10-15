"""What reading ZIM archives in Scout's own process costs (reference.py): open time,
memory, and search plus reading the top five articles, on synthetic archives written
with libzim (full-text index included). Nothing is downloaded.

    uv run python scripts/bench_reference.py 20000 200000

Each archive is built once under .tmp/bench-reference/ and measured in a fresh process.
Measured on the Mac mini (Apple M6, 24 GB), 2026-10-15, warm runs (a first run after
building, with the file not yet cached, opened the 200,000 in 30 ms and searched in 24 ms):

    articles  file    open    RSS after open  RSS after 30 searches  search+read5 p50 / max
    20,000    79 MB   2.4 ms  23 MB           84 MB                  8 / 12 ms
    200,000   789 MB  2.7 ms  24 MB           88 MB                  10.5 / 16 ms

Python alone is 14 MB. RSS includes the archive's pages the searches touched: they are
mapped from the file, so macOS drops them first under memory pressure.
"""

from __future__ import annotations

import random
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".tmp" / "bench-reference"
WORDS = 30000


def vocab() -> list[str]:
    rng = random.Random(1)  # noqa: S311 (synthetic text)
    letters = "abcdefghijklmnopqrstuvwxyz"
    return ["".join(rng.choice(letters) for _ in range(rng.randint(3, 10))) for _ in range(WORDS)]


def build(path: Path, n: int) -> None:
    from libzim.writer import Creator, Hint, Item, StringProvider

    front = Hint["FRONT_ARTICLE"]
    rng, words = random.Random(2), vocab()  # noqa: S311

    class Page(Item):
        def __init__(self, p: str, t: str, h: str) -> None:
            super().__init__()
            self.p, self.t, self.h = p, t, h

        def get_path(self) -> str:
            return self.p

        def get_title(self) -> str:
            return self.t

        def get_mimetype(self) -> str:
            return "text/html"

        def get_contentprovider(self) -> StringProvider:
            return StringProvider(self.h)

        def get_hints(self) -> dict[Hint, int]:
            return {front: True}

    def para() -> str:
        return "<p>" + " ".join(rng.choice(words) for _ in range(80)) + "</p>"

    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(True, "eng") as c:
        c.set_mainpath("A0")
        for key in ("Title", "Name", "Language", "Creator", "Publisher", "Date", "Description"):
            c.add_metadata(key, "eng" if key == "Language" else "bench")
        for i in range(n):
            title = f"{rng.choice(words)} {rng.choice(words)} {i}"
            sections = "".join(f"<h2>Section {j}</h2>{para()}{para()}" for j in range(4))
            body = f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{para()}{sections}</body></html>"
            c.add_item(Page(f"A{i}", title, body))


def measure(path: Path) -> None:
    def rss() -> float:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6  # bytes on macOS

    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    t = time.perf_counter()
    a = Archive(path)
    opened, after_open = time.perf_counter() - t, rss()
    s, rng, words = Searcher(a), random.Random(7), vocab()  # noqa: S311
    times = []
    for _ in range(30):
        t = time.perf_counter()
        for p in s.search(Query().set_query(rng.choice(words))).getResults(0, 5):
            bytes(a.get_entry_by_path(p).get_item().content)
        times.append(time.perf_counter() - t)
    times.sort()
    print(
        f"{a.article_count:>9,} articles  {a.filesize / 1e6:6.0f} MB  open {opened * 1000:.1f} ms  "
        f"RSS {after_open:.0f} MB after open, {rss():.0f} MB after 30 searches  "
        f"search+read5 p50 {times[15] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms"
    )


def main() -> None:
    if sys.argv[1:2] == ["--measure"]:
        measure(Path(sys.argv[2]))
        return
    for n in [int(x) for x in sys.argv[1:]] or [20000]:
        path = OUT / f"bench_{n}.zim"
        if not path.exists():
            t = time.perf_counter()
            build(path, n)
            print(f"built {path.name} in {time.perf_counter() - t:.0f} s")
        subprocess.run([sys.executable, __file__, "--measure", str(path)], check=True)  # noqa: S603


if __name__ == "__main__":
    main()
