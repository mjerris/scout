"""Write the offline reference tests' ZIM archives (tests/data/zims/) from the pages in
tests/zim_fixtures.py, with libzim's own Creator (full-text and title indexes included),
so the real kiwix-serve searches a real Xapian index in the integration test.

libzim is not one of Scout's dependencies (it is GPL-3.0-or-later; kiwix-serve reads the
archives in its own process). Run this only to change the test archives:

    uv run --no-project --with libzim==3.13.1 python scripts/make-test-zims.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from zim_fixtures import BOOKS, DATA  # noqa: E402

FRONT = Hint["FRONT_ARTICLE"]


class Page(Item):
    def __init__(self, path: str, title: str, body: str | bytes, mimetype: str) -> None:
        super().__init__()
        self.path, self.title, self.body, self.mimetype = path, title, body, mimetype

    def get_path(self) -> str:
        return self.path

    def get_title(self) -> str:
        return self.title

    def get_mimetype(self) -> str:
        return self.mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self.body)

    def get_hints(self) -> dict[Hint, int]:
        return {FRONT: self.mimetype == "text/html"}


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for b in BOOKS:
        out = DATA / b.file
        out.unlink(missing_ok=True)
        with Creator(out).config_indexing(True, "eng") as c:
            c.set_mainpath(b.pages[0][0])
            for key, value in {
                "Title": b.title, "Name": b.id.rsplit("_", 1)[0], "Language": "eng", "Creator": "test",
                "Publisher": "test", "Date": b.date, "Description": f"{b.title} (test)",
                "Tags": "_ftindex:yes;_pictures:no",
            }.items():  # fmt: skip
                c.add_metadata(key, value)
            for p, t, body, mime in b.pages:
                c.add_item(Page(p, t, body, mime))
            for p, t, target in b.redirects:
                c.add_redirection(p, t, target, {FRONT: True})
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
