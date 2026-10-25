"""The offline reference tests' archives: tiny ZIM files (tests/data/zims/, written once by
scripts/make-test-zims.py with libzim's own Creator, full-text and title indexes included)
and a fake kiwix-serve that answers kiwix-serve's HTTP API (the catalog, /search,
/suggest, /raw) from the same pages, so the unit tests need no binary and no network.
The real kiwix-serve runs on the same files in test_reference's integration test."""

from __future__ import annotations

import html
import json
import re
import shutil
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from loopback import LoopbackHTTPServer

DATA = Path(__file__).parent / "data" / "zims"

# Shaped like mwoffliner's Wikipedia pages: a title, an infobox, sections under h2/h3,
# reference marks, boilerplate sections, a script.
BEE_STING = """<html><head><title>Bee sting</title><script>alert("x")</script></head><body>
<h1>Bee sting</h1>
<table class="infobox"><tr><th>Specialty</th><td>Emergency medicine</td></tr></table>
<p>A <b>bee sting</b> is the wound and pain caused by the stinger of a female bee
puncturing skin.<sup class="reference">[1]</sup> Most stings cause only local pain and swelling.</p>
<h2>Signs and symptoms</h2>
<p>The sting site turns red and swells within minutes. The swelling may last up to 48 hours.</p>
<h2>Treatment<span class="mw-editsection">[edit]</span></h2>
<p>Remove the stinger by scraping it out sideways with a fingernail or a card. Wash the
sting site with soap and water and apply a cold pack for 10 minutes to reduce swelling.</p>
<h3>Severe reactions</h3>
<p>Call emergency services if the person has trouble breathing after a bee sting.</p>
<table><tr><th>Dose</th><th>Adult</th></tr><tr><td>Epinephrine</td><td>0.3 mg</td></tr></table>
<h2>See also</h2><ul><li>Wasp sting</li></ul>
<h2>References</h2><ol class="references"><li>A study of stings.</li></ol>
<div class="navbox">Insects navigation box</div>
</body></html>"""

EIFFEL = """<html><head><title>Eiffel Tower</title></head><body>
<h1>Eiffel Tower</h1>
<p>The <b>Eiffel Tower</b> is a wrought-iron lattice tower in Paris, France. It is
named after the engineer Gustave Eiffel, whose company built it from 1887 to 1889.</p>
<h2>Design</h2>
<p>The tower is 330 metres tall, about the height of an 81-storey building.</p>
<p><img src="_assets_/tower.png" alt="tower"></p>
<h2>Tourism</h2>
<p>It is one of the most visited paid monuments in the world. See <a href="Bee_sting">this</a>.</p>
</body></html>"""

MERCURY = """<html><head><title>Mercury (planet)</title></head><body>
<h1>Mercury (planet)</h1><p>Mercury is the smallest planet in the Solar System and the
closest to the Sun.</p></body></html>"""

PUMP = """<html><head><title>Replace an RV water pump</title></head><body>
<h1>Replace an RV water pump</h1><p>This guide shows how to replace a 12 volt RV water pump.</p>
<h2>Step 1</h2><p>Turn off the pump switch and open a faucet to relieve the pressure.</p>
<h2>Step 2</h2><p>Disconnect the two wires and loosen the hose clamps on the inlet and outlet.</p>
</body></html>"""

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


@dataclass(frozen=True)
class Book:
    file: str
    title: str
    pages: list[tuple[str, str, str | bytes, str]]  # (path, title, content, mimetype)
    redirects: list[tuple[str, str, str]] = field(default_factory=list)  # (path, title, target)
    date: str = "2026-06-01"
    language: str = "eng"
    fulltext: bool = True  # False: a title index only, as kiwix-serve says for a book without one

    @property
    def id(self) -> str:
        return self.file.removesuffix(".zim")


_WIKI_PAGES: list[tuple[str, str, str | bytes, str]] = [
    ("Bee_sting", "Bee sting", BEE_STING, "text/html"),
    ("Eiffel_Tower", "Eiffel Tower", EIFFEL, "text/html"),
    ("Mercury_(planet)", "Mercury (planet)", MERCURY, "text/html"),
    ("_assets_/tower.png", "", PNG, "image/png"),
]
BOOKS = [
    Book(
        "wikipedia_en_test_2026-06.zim", "Wikipedia", _WIKI_PAGES, [("Bee_stings", "Bee stings", "Bee_sting")]
    ),
    Book("wikipedia_en_test_2024-01.zim", "Old Wikipedia", _WIKI_PAGES[2:3]),
    Book(
        "ifixit_en_test_2025-12.zim",
        "iFixit",
        [("Replace_RV_pump", "Replace an RV water pump", PUMP, "text/html")],
    ),
]


def library(folder: Path) -> Path:
    """The test reference folder: a Wikipedia-like archive in two dated copies (the newer
    is used), an iFixit-like one, a file that isn't a ZIM, and a note that isn't .zim."""
    folder.mkdir(parents=True, exist_ok=True)
    for b in BOOKS:
        if not (folder / b.file).exists():
            shutil.copyfile(DATA / b.file, folder / b.file)
    (folder / "broken_en_2026-01.zim").write_bytes(b"not a zim file at all")
    (folder / "README.txt").write_text("not an archive")
    return folder


# --- a fake kiwix-serve --------------------------------------------------------------------


def _text(body: str | bytes) -> list[str]:
    plain = re.sub(r"<[^>]+>", " ", body if isinstance(body, str) else "")
    return re.findall(r"[a-z0-9]+", html.unescape(plain).lower())


class FakeKiwix:
    """kiwix-serve's API over the BOOKS, on 127.0.0.1 at `port`: a book matches a search
    when its title and text have every word (as Xapian's AND does), and a title matches a
    suggestion when each word starts one of its words. `books` are the ones it serves."""

    def __init__(self, books: list[Book] | None = None, port: int = 0) -> None:
        self.books = {b.id: b for b in (books if books is not None else [BOOKS[0], BOOKS[2]])}
        self.requests: list[str] = []
        self.scale = 1  # counts are this many times the hits, as a big archive's are
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                fake.requests.append(self.path)
                status, headers, body = fake.answer(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._http = LoopbackHTTPServer(("127.0.0.1", port), Handler)
        self.port = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)

    def __enter__(self) -> FakeKiwix:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.shutdown()
        self._http.server_close()

    def answer(self, url: str) -> tuple[int, dict[str, str], bytes]:
        parts = urllib.parse.urlsplit(url)
        many = urllib.parse.parse_qs(parts.query)
        q = {k: v[0] for k, v in many.items()}
        path = urllib.parse.unquote(parts.path)
        if path == "/catalog/v2/entries":
            return (
                200,
                {"Content-Type": "application/atom+xml;profile=opds-catalog;kind=acquisition"},
                self._catalog(),
            )
        if path == "/search":
            return self._search(many.get("books.name") or [q.get("content", "")], q.get("pattern", ""),
                                int(q.get("pageLength") or 25))  # fmt: skip
        if path == "/suggest":
            return self._suggest(q.get("content", ""), q.get("term", ""), int(q.get("count") or 10))
        m = re.fullmatch(r"/raw/([^/]+)/content/(.+)", path)
        if m and m[1] in self.books:
            book = self.books[m[1]]
            for p, _, target in book.redirects:
                if p == m[2]:
                    return 302, {"Location": f"/content/{book.id}/{urllib.parse.quote(target)}"}, b""
            for p, _, body, mime in book.pages:
                if p == m[2]:
                    return 200, {"Content-Type": mime}, body if isinstance(body, bytes) else body.encode()
        return 404, {"Content-Type": "text/html; charset=utf-8"}, b"<h1>Not Found</h1>"

    def _catalog(self) -> bytes:
        entries = "".join(
            f"""<entry><id>urn:uuid:{n}</id><title>{html.escape(b.title)}</title>
<updated>{b.date}T00:00:00Z</updated><name>{b.id}</name>
<language>{b.language}</language><tags>_ftindex:{"yes" if b.fulltext else "no"};_pictures:no</tags>
<articleCount>{sum(m == "text/html" for *_, m in b.pages) + len(b.redirects)}</articleCount>
<link type="text/html" href="/content/{b.id}" /></entry>"""
            for n, b in enumerate(self.books.values())
        )
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
<title>All Entries</title>{entries}</feed>""".encode()

    def _search(self, ids: list[str], pattern: str, limit: int) -> tuple[int, dict[str, str], bytes]:
        """Like kiwix-serve: several books are searched together (in the order given here;
        the real one merges them by score), but only when they share a language."""
        if any(i not in self.books for i in ids):
            return 400, {"Content-Type": "text/html"}, b"No such book"
        if len({self.books[i].language for i in ids}) > 1:
            return 400, {"Content-Type": "text/html"}, b"Two or more books in different languages"
        words = re.findall(r"[a-z0-9]+", pattern.lower())
        hits = [
            (i, p, t) for i in ids for p, t, body, mime in self.books[i].pages
            if mime == "text/html" and words and set(words) <= set(_text(t) + _text(body))
        ]  # fmt: skip
        items = "".join(
            f"<item><title>{html.escape(t)}</title><link>/content/{i}/{urllib.parse.quote(p)}</link>"
            f"<description>...{' '.join(f'<b>{w}</b>' for w in words)} in {html.escape(t)}...</description>"
            f"<book><title>{html.escape(self.books[i].title)}</title></book></item>"
            for i, p, t in hits[:limit]
        )
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><channel>
<title>Search: {html.escape(pattern)}</title><link>/search</link>
<opensearch:totalResults>{len(hits) * self.scale:,}</opensearch:totalResults>{items}</channel></rss>"""
        return 200, {"Content-Type": "application/rss+xml; charset=utf-8"}, body.encode()

    def _suggest(self, book: str, term: str, count: int) -> tuple[int, dict[str, str], bytes]:
        if book not in self.books:
            return 404, {"Content-Type": "text/html"}, b"No such book"
        words = re.findall(r"[a-z0-9]+", term.lower())
        out = [
            {
                "value": html.escape(t, quote=False),
                "label": t,
                "kind": "path",
                "path": html.escape(p, quote=False),
            }
            for p, t, _, mime in self.books[book].pages
            if mime == "text/html" and words and all(any(tw.startswith(w) for tw in _text(t)) for w in words)
        ][:count]
        out.append({"value": term + " ", "label": f"containing '{term}'...", "kind": "pattern"})
        return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(out).encode()
