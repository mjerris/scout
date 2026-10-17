"""What the offline reference costs through kiwix-serve (reference.py, kiwix.py): its start
time and memory, and a search plus reading the top five articles, on synthetic archives
written with libzim (full-text index included). Nothing is downloaded.

libzim only writes the archives here; it isn't one of Scout's dependencies (GPL-3.0-or-later):

    uv run --with libzim==3.13.1 python scripts/bench_reference.py 20000 200000

kiwix-serve is Scout's (scripts/install-kiwix.sh), or the one in $KIWIX_SERVE. Each archive
is built once under .tmp/bench-reference/ and served on a free port while measured.
Measured on the Mac mini (Apple M6, 24 GB), 2026-10-16, kiwix-serve 3.8.2, warm runs:

    articles  file    ready   kiwix-serve RSS after start / 30 searches  search+read5 p50 / max
    20,000    79 MB   40 ms   17 / 69 MB                                  16 / 27 ms
    200,000   789 MB  27 ms   13 / 68 MB                                  17 / 28 ms

In Scout's process with libzim (before 2026-10-16) the same searches took 8 / 12 and
10.5 / 16 ms: the difference is six HTTP requests on 127.0.0.1 per search.
"""

from __future__ import annotations

import os
import random
import socket
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
        c.add_metadata("Tags", "_ftindex:yes")
        for i in range(n):
            title = f"{rng.choice(words)} {rng.choice(words)} {i}"
            sections = "".join(f"<h2>Section {j}</h2>{para()}{para()}" for j in range(4))
            body = f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{para()}{sections}</body></html>"
            c.add_item(Page(f"A{i}", title, body))


def rss_mb(pid: int) -> float:
    out = subprocess.run(  # noqa: S603
        ["/bin/ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return int(out.stdout.strip() or 0) / 1024


def measure(path: Path, exe: Path) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from scout import kiwix

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    client = kiwix.Client(port)
    t = time.perf_counter()
    proc = subprocess.Popen(  # noqa: S603
        [str(exe), "--address", "127.0.0.1", "--port", str(port), str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        while True:
            try:
                (entry,) = client.catalog()
                break
            except kiwix.Unavailable:
                time.sleep(0.01)
        started, after_start = time.perf_counter() - t, rss_mb(proc.pid)
        rng, words = random.Random(7), vocab()  # noqa: S311
        times = []
        for _ in range(30):
            t = time.perf_counter()
            for p, _ in client.search(entry.id, [rng.choice(words)], 5)[1]:
                client.raw(entry.id, p)
            times.append(time.perf_counter() - t)
        times.sort()
        print(
            f"{entry.articles:>9,} articles  {path.stat().st_size / 1e6:6.0f} MB  ready in {started * 1000:.0f} ms  "
            f"kiwix-serve RSS {after_start:.0f} MB after start, {rss_mb(proc.pid):.0f} MB after 30 searches  "
            f"search+read5 p50 {times[15] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms"
        )
    finally:
        proc.terminate()
        proc.wait(5)


def main() -> None:
    home = Path(os.environ.get("SCOUT_HOME") or Path.home() / "Library" / "Application Support" / "Scout")
    exe = Path(os.environ.get("KIWIX_SERVE") or home / "bin" / "kiwix-serve")
    if not exe.is_file():
        sys.exit(f"no kiwix-serve at {exe} (scripts/install-kiwix.sh, or set KIWIX_SERVE)")
    for n in [int(x) for x in sys.argv[1:]] or [20000]:
        path = OUT / f"bench_{n}.zim"
        if not path.exists():
            t = time.perf_counter()
            build(path, n)
            print(f"built {path.name} in {time.perf_counter() - t:.0f} s")
        measure(path, exe)


if __name__ == "__main__":
    main()
