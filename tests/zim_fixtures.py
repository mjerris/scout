"""Tiny synthetic ZIM archives for the offline reference tests, written with libzim's
own Creator (full-text and title indexes included), so search runs against a real
Xapian index exactly as it would on a Kiwix download. Nothing is downloaded."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from libzim.writer import Creator, Hint, Item, StringProvider

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

FRONT = Hint["FRONT_ARTICLE"]  # (the stub types Hint.FRONT_ARTICLE oddly for mypy)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class _Page(Item):
    def __init__(self, path: str, title: str, body: str | bytes, mimetype: str = "text/html") -> None:
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


def build(
    path: Path,
    title: str,
    pages: list[tuple[str, str, str | bytes, str]],
    redirects: Sequence[tuple[str, str, str]] = (),
) -> Path:
    """A ZIM at `path` with `pages` (path, title, content, mimetype) and `redirects`
    (path, title, target path)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(True, "eng") as c:
        c.set_mainpath(pages[0][0])
        for key, value in {
            "Title": title, "Name": path.stem, "Language": "eng", "Creator": "test",
            "Publisher": "test", "Date": "2026-06-01", "Description": f"{title} (test)",
        }.items():  # fmt: skip
            c.add_metadata(key, value)
        for p, t, body, mime in pages:
            c.add_item(_Page(p, t, body, mime))
        for p, t, target in redirects:
            c.add_redirection(p, t, target, {FRONT: True})
    return path


def library(folder: Path) -> Path:
    """The test reference folder: a Wikipedia-like archive in two dated copies (the newer
    is used), an iFixit-like one, a file that isn't a ZIM, and a note that isn't .zim."""
    if (folder / "ifixit_en_test_2025-12.zim").exists():
        return folder
    pages: list[tuple[str, str, str | bytes, str]] = [
        ("Bee_sting", "Bee sting", BEE_STING, "text/html"),
        ("Eiffel_Tower", "Eiffel Tower", EIFFEL, "text/html"),
        ("Mercury_(planet)", "Mercury (planet)", MERCURY, "text/html"),
        ("_assets_/tower.png", "", PNG, "image/png"),
    ]
    build(
        folder / "wikipedia_en_test_2026-06.zim",
        "Wikipedia",
        pages,
        [("Bee_stings", "Bee stings", "Bee_sting")],
    )
    build(folder / "wikipedia_en_test_2024-01.zim", "Old Wikipedia", pages[2:3])
    build(
        folder / "ifixit_en_test_2025-12.zim",
        "iFixit",
        [("Replace_RV_pump", "Replace an RV water pump", PUMP, "text/html")],
    )
    (folder / "broken_en_2026-01.zim").write_bytes(b"not a zim file at all")
    (folder / "README.txt").write_text("not an archive")
    return folder
