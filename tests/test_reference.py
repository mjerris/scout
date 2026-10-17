"""The offline reference (reference.py, kiwix.py): Kiwix ZIM archives searched through
their own full-text index and read a section at a time, as tools for Claude, as a source
for local answers when the internet is down, and as pages on Scout's web UI. kiwix-serve
reads the archives; here a fake one answers its API from the same pages
(tests/zim_fixtures.py), and the real one, when installed, runs in one integration test."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_web import token_file  # noqa: F401 (the fixture)
from zim_fixtures import BOOKS, FakeKiwix
from zim_fixtures import library as build_library

from scout import (
    config,
    kiwix,
    links,
    mcp_server,
    phone_service,
    reference,
    refs,
    reminders_mac,
    shared_tools,
)
from scout.mac import ToolError

BASE = "https://mini.tail1234.ts.net"
HOME = Path(os.environ["SCOUT_HOME"])  # this test run's own data folder
FOLDER = HOME / "zims"
EXE = HOME / "bin" / "kiwix-serve-stand-in"  # the program only has to be there; the fake answers
BEE = "zim:wikipedia_en_test/Bee_sting"


@pytest.fixture(scope="module")
def fake() -> Iterator[FakeKiwix]:
    with FakeKiwix() as f:
        yield f


def make_lib(port: int, names: list[str] | None = None) -> reference.Library:
    build_library(FOLDER)
    EXE.parent.mkdir(parents=True, exist_ok=True)
    EXE.touch()
    return reference.Library(FOLDER, names or [], port, exe=EXE)


@pytest.fixture
def lib(fake: FakeKiwix, monkeypatch: pytest.MonkeyPatch) -> reference.Library:
    lib = make_lib(fake.port)
    monkeypatch.setattr(reference, "_library", lib)
    monkeypatch.setattr(links, "web_base", lambda refresh=False: BASE)
    return lib


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the archives ------------------------------------------------------------------------


def test_books_are_found_by_name_newest_copy_and_bad_files_skipped(lib: reference.Library) -> None:
    assert [f.name for f in lib.files()] == ["ifixit_en_test_2025-12.zim", "wikipedia_en_test_2026-06.zim"]
    books = {b.name: b for b in lib.books()}
    assert set(books) == {"wikipedia_en_test", "ifixit_en_test"}  # not broken_en, not README.txt
    wiki = books["wikipedia_en_test"]
    assert wiki.file.name == "wikipedia_en_test_2026-06.zim" and wiki.title == "Wikipedia"
    assert wiki.id == "wikipedia_en_test_2026-06"  # its name in kiwix-serve's URLs
    assert wiki.date == "2026-06-01" and wiki.articles == 4 and wiki.fulltext  # three pages and a redirect
    assert wiki.size == wiki.file.stat().st_size


def test_archives_setting_picks_which_books(fake: FakeKiwix) -> None:
    assert [b.name for b in make_lib(fake.port, ["ifixit_en_test"]).books()] == ["ifixit_en_test"]
    # A file name works too, and picks that exact copy.
    with FakeKiwix(BOOKS) as every:
        only = make_lib(every.port, ["wikipedia_en_test_2024-01.zim"]).books()
    assert [(b.name, b.title) for b in only] == [("wikipedia_en_test", "Old Wikipedia")]


def test_no_folder_means_no_books(tmp_path: Path) -> None:
    lib = reference.Library(tmp_path / "missing", exe=EXE)
    assert lib.books() == []
    with pytest.raises(ToolError, match="No offline reference"):
        lib.search("bee sting")


def test_without_kiwix_serve_the_tools_say_so(fake: FakeKiwix, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = reference.Library(build_library(FOLDER), port=fake.port, exe=HOME / "bin" / "nope")
    monkeypatch.setattr(reference, "_library", missing)
    with pytest.raises(ToolError, match="offline reference isn't installed: kiwix-serve"):
        run(reference.search_text({"query": "bee sting"}))
    assert "not readable: The offline reference isn't installed" in reference.status_line(missing)
    down = make_lib(free_port())  # installed, but nothing answers
    monkeypatch.setattr(reference, "_library", down)
    with pytest.raises(ToolError, match="isn't running right now"):
        run(reference.read_text({"ref": BEE}))
    assert run(refs.resolve(BEE)) is None
    assert (
        run(reference.wanted("what is a bee sting", config.ReferenceConfig(local_answers="always"))) is False
    )


# --- articles cut at their headings ----------------------------------------------------


def test_sections_cut_at_headings_without_boilerplate() -> None:
    from zim_fixtures import BEE_STING

    title, secs = reference.sections(BEE_STING)
    assert title == "Bee sting"
    assert [s.heading for s in secs] == ["", "Signs and symptoms", "Treatment", "Severe reactions"]
    intro = secs[0].text
    assert intro.startswith("A bee sting is the wound") and "[1]" not in intro
    assert "Emergency medicine" not in intro  # the infobox
    assert all("alert" not in s.text and "navigation box" not in s.text for s in secs)
    assert "Epinephrine | 0.3 mg" in secs[3].text  # table cells kept apart
    assert secs[2].text.startswith("Remove the stinger") and "[edit]" not in secs[2].heading


def test_unstructured_page_is_one_section() -> None:
    title, secs = reference.sections("<p>Just <i>some</i> text.</p><p>More.</p>")
    assert title == "" and [(s.heading, s.text) for s in secs] == [("", "Just some text.\nMore.")]


# --- search and read -------------------------------------------------------------------


def test_search_finds_the_article_with_ref_and_link(lib: reference.Library) -> None:
    hits = lib.search("bee sting treatment")
    assert hits[0].title == "Bee sting" and hits[0].ref == BEE
    assert hits[0].url == f"{BASE}/ref/wikipedia_en_test/Bee_sting"
    assert hits[0].snippet.startswith("A bee sting is the wound")


def test_search_drops_a_word_no_article_has(lib: reference.Library) -> None:
    hits = lib.search("how do I treat a bee sting zzyzx")
    assert hits and hits[0].ref == BEE


def test_search_one_book_and_a_path_needing_quoting(lib: reference.Library) -> None:
    assert [h.title for h in lib.search("water pump", book="ifixit_en_test")] == ["Replace an RV water pump"]
    assert lib.search("water pump", book="wikipedia_en_test") == []
    (mercury,) = lib.search("smallest planet")
    assert mercury.ref == "zim:wikipedia_en_test/Mercury_%28planet%29"
    assert refs.clean([mercury.ref]) == (mercury.ref,)  # a valid record id
    with pytest.raises(ToolError, match="no book"):
        lib.search("pump", book="nope")


def test_read_gives_intro_and_section_list_then_one_section(lib: reference.Library) -> None:
    text = run(reference.read_text({"ref": BEE}))
    assert text.startswith(
        f"ref: {BEE}\nBee sting (Wikipedia, 2026-06-01)\nlink: {BASE}/ref/wikipedia_en_test/Bee_sting"
    )
    assert "Sections: Signs and symptoms; Treatment; Severe reactions" in text
    assert "A bee sting is the wound" in text and "Remove the stinger" not in text
    one = run(reference.read_text({"ref": BEE, "section": "treatment"}))
    assert "== Treatment ==\nRemove the stinger" in one and "A bee sting is the wound" not in one
    best = run(reference.read_text({"ref": BEE, "query": "trouble breathing"}))
    assert "== Severe reactions ==" in best


def test_read_follows_a_redirect_and_rejects_unknown_refs(lib: reference.Library) -> None:
    assert run(reference.read_text({"ref": "zim:wikipedia_en_test/Bee_stings"})).startswith(f"ref: {BEE}\n")
    for bad in ("zim:wikipedia_en_test/Nope", "zim:nope/Bee_sting", "task:3", "zim:"):
        with pytest.raises(ToolError):
            run(reference.read_text({"ref": bad}))


def test_search_tool_lists_refs_that_refs_picks_up(lib: reference.Library) -> None:
    out = run(shared_tools.BY_NAME["reference_search"].run({"query": "bee sting"}))
    assert out.startswith("1. Bee sting (Wikipedia): A bee sting is the wound")
    assert f"(ref {BEE})" in out
    assert refs.from_tool("mcp__voice_app__reference_search", {}, out) == [BEE]
    read = run(shared_tools.BY_NAME["reference_read"].run({"ref": BEE}))
    assert refs.from_tool("reference_read", {"ref": BEE}, read) == [BEE]


def test_the_tools_reach_the_room_the_phone_and_other_sessions() -> None:
    for name in ("reference_search", "reference_read"):
        assert not shared_tools.BY_NAME[name].asks
        assert name in {t.name for t in phone_service.PHONE_TOOLS}
        assert callable(getattr(mcp_server, name))


# --- links ---------------------------------------------------------------------------------


def test_a_ref_resolves_and_its_link_goes_to_the_phone(
    lib: reference.Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    added: list[dict[str, Any]] = []

    async def add_data(a: dict[str, Any], helper: Any = None, mirror: bool = False) -> dict[str, Any]:
        added.append(a)
        return {"id": "R1", "title": a["title"]}

    monkeypatch.setattr(reminders_mac, "add_data", add_data)
    refs._sent.clear()
    r = run(refs.resolve(BEE))
    assert r["kind"] == "reference" and r["url"] == f"{BASE}/ref/wikipedia_en_test/Bee_sting"
    assert not r["private"]
    assert "link: " + r["url"] in run(refs.open_link({"ref": BEE}))
    assert run(refs.send_link({"ref": BEE})) == f"ref: {BEE}\nI put the link in your Scout reminders."
    assert added == [{"title": "Link: Bee sting (Wikipedia)", "list": "Scout", "url": r["url"]}]
    assert run(refs.resolve("zim:wikipedia_en_test/Nope")) is None


# --- offline -----------------------------------------------------------------------------


def test_online_is_checked_cheaply_and_kept_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    async def probe(host: str, port: int, timeout: float) -> bool:
        calls.append((host, port))
        return False

    monkeypatch.setattr(reference, "_probe", probe)
    monkeypatch.setattr(reference, "_online", (0.0, True))
    assert run(reference.online()) is False
    assert run(reference.online()) is False
    assert len(calls) == len(reference.PROBES)  # once per probe, then remembered


class FakeModel:
    ready = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int = 120) -> str:
        self.prompts.append(user)
        return self.reply


GOOD = (
    '{"found": true, "source": 1, "quote": "Remove the stinger by scraping it out sideways", '
    '"answer": "Remove the stinger by scraping it out sideways, wash the sting site, and apply a cold pack '
    'for 10 minutes."}'
)


def test_a_local_answer_comes_from_a_passage_with_its_ref(lib: reference.Library) -> None:
    model = FakeModel(GOOD)
    said = run(reference.answer("how do I treat a bee sting", model))
    assert said == (
        "From Wikipedia: Remove the stinger by scraping it out sideways, wash the sting site, "
        "and apply a cold pack for 10 minutes."
    )
    assert said.refs == (BEE,) and not said.private
    assert "[1] (Bee sting, Treatment)" in model.prompts[0] and "References" not in model.prompts[0]


@pytest.mark.parametrize(
    "reply",
    [
        '{"found": false}',
        '{"found": true, "source": 1, "quote": "rub it with butter", "answer": "Rub it with butter."}',
        GOOD.replace("10 minutes", "45 minutes"),  # a number the passage doesn't have
        GOOD.replace('"source": 1', '"source": 9'),
    ],
)
def test_an_ungrounded_local_answer_is_dropped(lib: reference.Library, reply: str) -> None:
    assert run(reference.answer("how do I treat a bee sting", FakeModel(reply))) is None


def test_local_answers_only_when_offline_and_for_lookups(
    lib: reference.Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    up = {"v": True}

    async def online() -> bool:
        return up["v"]

    monkeypatch.setattr(reference, "online", online)
    cfg = config.ReferenceConfig()
    assert run(reference.wanted("how do I treat a bee sting", cfg)) is False  # online: Claude
    up["v"] = False
    assert run(reference.wanted("how do I treat a bee sting", cfg)) is True
    assert run(reference.wanted("look up the Eiffel Tower", cfg)) is True
    assert run(reference.wanted("send Pat the bee sting article", cfg)) is False  # an action
    assert run(reference.wanted("turn it off", cfg)) is False  # not a question
    assert (
        run(reference.wanted("what is a bee sting", config.ReferenceConfig(local_answers="never"))) is False
    )
    up["v"] = True
    assert (
        run(reference.wanted("what is a bee sting", config.ReferenceConfig(local_answers="always"))) is True
    )


def test_the_room_answers_from_the_reference_when_offline(
    lib: reference.Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_routing import make

    async def offline() -> bool:
        return False

    monkeypatch.setattr(reference, "online", offline)

    class Tier1(FakeModel):
        async def decide(self, request: str, now: Any) -> dict[str, Any] | None:
            return None  # knowledge: tier 1 hands it to Claude

    async def go() -> tuple[bool, list[str], Any]:
        r: Any = make()
        r.a.tier1 = Tier1(GOOD)
        handled = await r.a._answer_locally("how do I treat a bee sting")
        return handled, r.spk.said, refs.log_of().last(channel=None)

    handled, said, last = run(go())
    assert handled and said and said[-1].startswith("From Wikipedia: Remove the stinger")
    assert last is not None and last.refs == (BEE,)  # "give me a link to that" works


# --- the web page --------------------------------------------------------------------------


@pytest.mark.usefixtures("token_file")
def test_articles_open_on_the_web_ui_token_gated_without_scripts(lib: reference.Library) -> None:
    import aiohttp
    from test_web import TOKEN, serve

    async def go() -> list[tuple[int, str, str, bytes]]:
        out = []
        async with serve() as srv, aiohttp.ClientSession() as s:
            for p in (
                "/ref/wikipedia_en_test/Bee_sting",  # no token
                f"/ref/wikipedia_en_test/Bee_sting?token={TOKEN}",
                f"/ref/wikipedia_en_test/_assets_/tower.png?token={TOKEN}",
                f"/ref/wikipedia_en_test/Bee_stings?token={TOKEN}",
                f"/ref/wikipedia_en_test/Nope?token={TOKEN}",
                f"/ref/wikipedia_en_test/Mercury_%28planet%29?token={TOKEN}",
            ):
                async with s.get(srv.url + p, allow_redirects=False) as r:
                    out.append((r.status, r.headers.get("Content-Type", ""),
                                r.headers.get("Content-Security-Policy", "") + "|" + r.headers.get("Location", ""),
                                await r.read()))  # fmt: skip
        return out

    no_token, page, image, redirect, missing, mercury = run(go())
    assert no_token[0] == 401
    assert page[0] == 200 and page[1].startswith("text/html") and b"Remove the stinger" in page[3]
    assert "script-src 'none'" in page[2] and "connect-src 'none'" in page[2]
    assert image[0] == 200 and image[1] == "image/png" and image[3].startswith(b"\x89PNG")
    assert redirect[0] == 302 and redirect[2].endswith("|Bee_sting")  # relative: links inside still work
    assert missing[0] == 404
    assert mercury[0] == 200 and b"smallest planet" in mercury[3]


# --- config and status ---------------------------------------------------------------------


def test_config_and_status_line(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        f'[reference]\ndir = "{FOLDER}"\narchives = ["wikipedia_en_test"]\nlocal_answers = "always"\n'
    )
    cfg = config.load(tmp_path / "config.toml").reference
    assert (cfg.dir, cfg.archives, cfg.local_answers) == (str(FOLDER), ["wikipedia_en_test"], "always")
    with FakeKiwix() as fake:
        line = reference.status_line(make_lib(fake.port, cfg.archives))
    assert line.startswith("reference: 1 archive (wikipedia_en_test 2026-06-01, ")
    assert line.endswith(f"through kiwix-serve on 127.0.0.1:{fake.port}")
    assert config.ReferenceConfig().port == 8769
    empty = reference.status_line(reference.Library(tmp_path / "none"))
    assert empty == f"reference: no archives in {tmp_path / 'none'} (docs/reference.md)"


def test_a_bad_local_answers_value_falls_back(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[reference]\nlocal_answers = "sometimes"\n')
    assert config.load(tmp_path / "config.toml").reference.local_answers == "offline"


# --- kiwix-serve, run by the app (kiwix.Server) ----------------------------------------------


def stand_in(folder: Path) -> Path:
    """A kiwix-serve program that is the fake (tests/fake_kiwix_serve.py)."""
    exe = folder / "kiwix-serve"
    script = Path(__file__).parent / "fake_kiwix_serve.py"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(0o755)
    return exe


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[reference.Library, kiwix.Server]:
    monkeypatch.setattr(reference, "SCAN_S", 0.0)  # the folder is looked at on every check
    monkeypatch.setattr(kiwix, "RETRY_S", (0.0,))
    port = free_port()
    exe = stand_in(tmp_path)
    lib = reference.Library(build_library(tmp_path / "zims"), port=port, exe=exe)
    return lib, kiwix.Server(lib.files, port, exe=exe, on_change=lib.changed)


def test_the_app_runs_kiwix_serve_and_restarts_it_when_the_archives_change(
    served: tuple[reference.Library, kiwix.Server],
) -> None:
    lib, srv = served

    async def go() -> None:
        await srv.check()
        assert srv.status() == {
            "kiwix_serve": "running", "version": "9.9.9", "address": f"127.0.0.1:{srv.port}",
            "pid": srv.pid, "archives": 2,
        }  # fmt: skip
        first = srv.pid
        assert first is not None
        hits = await asyncio.to_thread(lib.search, "water pump")
        assert [h.title for h in hits] == ["Replace an RV water pump"]
        await srv.check()
        assert srv.pid == first  # nothing changed: left alone
        (lib.folder / "ifixit_en_test_2025-12.zim").unlink()
        await srv.check()
        assert srv.pid != first and gone(first) and srv.status()["archives"] == 1
        assert [b.name for b in await asyncio.to_thread(lib.books)] == ["wikipedia_en_test"]
        last = srv.pid
        assert last is not None
        await srv.stop()
        assert srv.pid is None and srv.state == "stopped" and gone(last)

    run(go())


def test_kiwix_serve_that_dies_is_started_again(served: tuple[reference.Library, kiwix.Server]) -> None:
    import signal

    lib, srv = served

    async def go() -> None:
        await srv.check()
        first = srv.pid
        assert first is not None
        os.kill(first, signal.SIGKILL)
        for _ in range(100):
            if srv.pid is None:
                break
            await asyncio.sleep(0.02)
        await srv.check()
        assert srv.state == "running" and srv.pid not in (None, first)
        assert await asyncio.to_thread(lib.search, "bee sting")
        await srv.stop()

    run(go())


def test_kiwix_serve_that_fails_to_start_waits_before_trying_again(
    served: tuple[reference.Library, kiwix.Server], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, srv = served
    monkeypatch.setenv("FAKE_KIWIX_EXIT", "3")
    monkeypatch.setattr(kiwix, "RETRY_S", (60.0,))

    async def go() -> None:
        await srv.check()
        assert srv.state == "failed" and srv.pid is None
        monkeypatch.delenv("FAKE_KIWIX_EXIT")
        await srv.check()
        assert srv.state == "failed"  # not before the wait is over
        srv._retry_at = 0.0
        await srv.check()
        assert srv.state == "running"
        await srv.stop()

    run(go())


def test_no_program_or_no_archives_means_no_kiwix_serve(tmp_path: Path) -> None:
    folder = build_library(tmp_path / "zims")
    lib = reference.Library(folder, port=free_port(), exe=tmp_path / "missing")
    srv = kiwix.Server(lib.files, free_port(), exe=lib.exe)
    run(srv.check())
    assert srv.state == "not installed" and srv.pid is None
    empty = reference.Library(tmp_path / "empty", exe=stand_in(tmp_path))
    srv = kiwix.Server(empty.files, free_port(), exe=empty.exe)
    run(srv.check())
    assert srv.state == "no archives" and srv.pid is None


# --- the real kiwix-serve (only where it is installed) ---------------------------------------


def _real_kiwix() -> Path | None:
    for p in (
        os.environ.get("KIWIX_SERVE"),
        str(Path.home() / "Library" / "Application Support" / "Scout" / "bin" / "kiwix-serve"),
        shutil.which("kiwix-serve"),
    ):
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return Path(p)
    return None


REAL = _real_kiwix()


@pytest.mark.skipif(REAL is None, reason="kiwix-serve isn't installed (scripts/install-kiwix.sh)")
def test_the_real_kiwix_serve_reads_the_test_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REAL is not None
    port = free_port()
    lib = reference.Library(build_library(tmp_path / "zims"), port=port, exe=REAL)
    srv = kiwix.Server(lib.files, port, exe=REAL, on_change=lib.changed)
    monkeypatch.setattr(reference, "_library", lib)
    monkeypatch.setattr(links, "web_base", lambda refresh=False: BASE)

    async def go() -> None:
        await srv.check()
        try:
            assert srv.state == "running" and srv.version.startswith("3."), srv.status()
            books = {b.name: b for b in await asyncio.to_thread(lib.books)}
            assert set(books) == {"wikipedia_en_test", "ifixit_en_test"}
            assert books["wikipedia_en_test"].fulltext and books["wikipedia_en_test"].title == "Wikipedia"
            hits = await asyncio.to_thread(lib.search, "bee sting treatment")
            assert hits[0].ref == BEE and hits[0].snippet.startswith("A bee sting is the wound")
            assert (await asyncio.to_thread(lib.search, "how do I treat a bee sting zzyzx"))[0].ref == BEE
            (mercury,) = await asyncio.to_thread(lib.search, "smallest planet")
            assert mercury.ref == "zim:wikipedia_en_test/Mercury_%28planet%29"
            assert [h.title for h in await asyncio.to_thread(lib.search, "water pump", "ifixit_en_test")] == [
                "Replace an RV water pump"
            ]
            text = await reference.read_text({"ref": "zim:wikipedia_en_test/Bee_stings"})  # a redirect
            assert text.startswith(f"ref: {BEE}\n") and "Sections: Signs and symptoms; Treatment" in text
            png = await asyncio.to_thread(lib.content, "wikipedia_en_test", "_assets_/tower.png")
            assert png.mimetype == "image/png" and png.data.startswith(b"\x89PNG")
            assert (
                await asyncio.to_thread(lib.content, "wikipedia_en_test", "Bee_stings")
            ).redirect == "Bee_sting"
            with pytest.raises(KeyError):
                await asyncio.to_thread(lib.content, "wikipedia_en_test", "Nope")
        finally:
            pid = srv.pid
            await srv.stop()
        assert pid is not None and gone(pid)

    run(go())


def test_a_count_of_thousands_is_read(fake: FakeKiwix, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake, "scale", 2000)  # kiwix-serve says "2,000"
    assert kiwix.Client(fake.port).search("wikipedia_en_test_2026-06", ["sting"], 1)[0] == 2000


def test_suggestions_are_unescaped(monkeypatch: pytest.MonkeyPatch) -> None:
    # As kiwix-serve 3.8.2 answers /suggest for a title with "&" in it.
    body = (
        b'[{"value" : "Float like a butterfly &amp; sting like a bee", "label" : "x", "kind" : "path",'
        b' "path" : "Float_like_a_butterfly_&amp;_sting_like_a_bee"}, {"value": "f ", "kind": "pattern"}]'
    )
    client = kiwix.Client(free_port())
    monkeypatch.setattr(client, "_get", lambda path, query=None: (200, {}, body))
    assert client.suggest("wikipedia_en_top_nopic_2026-09", "float like a butterfly", 3) == [
        ("Float_like_a_butterfly_&_sting_like_a_bee", "Float like a butterfly & sting like a bee")
    ]
