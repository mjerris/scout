"""Section-aware windows (index._doc_texts): a long item with markdown headings is cut at
its headings, each window carrying the item's title and its section's heading; short
items and long ones without headings are cut as before; a new window scheme re-embeds
the long items in the background. Synthetic text only."""

from pathlib import Path

from test_index import FakeEmbedder

from scout import index


def _summary(sections: dict[str, str], opening: str = "Weekly sync on the fiber rollout.") -> str:
    return "\n\n".join([opening, *(f"### {h}\n{t}" for h, t in sections.items())])


SECTIONS = {
    "Budget": "The budget is 258,000 dollars, with 16,000 set aside for contractors. " * 6,
    "Risks": "The biggest risk is the vendor slipping past the holidays. " * 7,
    "Action items": "Pat will send the cost estimate by Friday. " * 8,
}


def _hit(body: str, title: str = "Fiber rollout weekly sync", who: str = "Pat, Sam") -> index.Hit:
    return index.Hit("granola:x", "meeting", title, body, who, 0, {})


def test_short_items_and_long_ones_without_headings_are_cut_as_before() -> None:
    assert index._doc_texts(_hit("### Budget\nshort", who="")) == [
        "Fiber rollout weekly sync ### Budget\nshort"
    ]
    plain = "".join(f"{i:04d}" for i in range(1000))
    assert index._doc_texts(_hit(plain)) == index._fixed_windows(_hit(plain))


def test_long_markdown_is_cut_at_its_headings_with_title_and_heading() -> None:
    body = _summary(SECTIONS)
    assert len(body) > index.EMBED_CHARS
    texts = index._doc_texts(_hit(body))
    assert texts[0].startswith("Fiber rollout weekly sync Pat, Sam")
    for head, text in SECTIONS.items():
        mine = [t for t in texts if text.strip()[:40] in t]
        assert mine, head
        # The window holding a section's text says which meeting and which section.
        assert all(
            t.startswith(f"Fiber rollout weekly sync{index.SECTION_MARK}{head}\n") or t is texts[0]
            for t in mine
        )
    # No window mixes two sections' text.
    for t in texts[1:]:
        assert sum(text[:40] in t for text in SECTIONS.values()) == 1


def test_a_long_section_is_split_and_every_piece_keeps_its_heading() -> None:
    body = _summary(
        {"Notes": "word " * 600, "Risks": "The biggest risk is the permit."}
    )  # 3,000 chars of notes
    texts = index._doc_texts(_hit(body))
    notes = [t for t in texts if "word word" in t]
    assert len(notes) >= 3
    assert all(t.startswith(f"Fiber rollout weekly sync{index.SECTION_MARK}Notes\n") for t in notes[1:])
    assert any(
        t.startswith(f"Fiber rollout weekly sync{index.SECTION_MARK}Risks\n") and "permit" in t for t in texts
    )
    assert "".join(texts).count("word") >= 600  # all of it covered


def test_small_sections_share_a_window() -> None:
    small = {f"Topic {i}": f"Item {i} is fine." for i in range(40)}  # 40 tiny sections
    texts = index._doc_texts(_hit(_summary(small, "x" * 900)))
    assert len(texts) < 10  # packed, not one window each
    assert all(f"Item {i} is fine." in "".join(texts) for i in range(40))


def test_windows_stay_within_the_cap() -> None:
    many = {f"Part {i}": "text " * 220 for i in range(40)}
    assert len(index._doc_texts(_hit(_summary(many)))) == index.MAX_WINDOWS


def test_a_new_window_scheme_re_embeds_long_items_only(tmp_path: Path) -> None:
    db = tmp_path / "scout.db"
    ix = index.Index(db, FakeEmbedder())
    ix.put("long", "meeting", "Fiber rollout weekly sync", _summary(SECTIONS), ts=1)
    ix.put("short", "note", "Gate code", "The gate code is 4417.", ts=2)
    while ix.embed_pending(16):
        pass
    with ix._lock:  # as a store made under the earlier scheme
        ix._conn.execute("UPDATE settings SET value = '1' WHERE key = 'windows_version'")
    ix.close()
    ix = index.Index(db, FakeEmbedder())
    with ix._lock:
        assert [r[0] for r in ix._conn.execute("SELECT id FROM pending")] == ["long"]
    found = ix.search("contractors budget fiber rollout")  # the old vectors serve meanwhile
    assert found and found[0].id == "long"
    while ix.embed_pending(16):
        pass
    with ix._lock:
        assert ix._conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
        n = ix._conn.execute("SELECT count(*) FROM vecs WHERE id = 'long'").fetchone()[0]
        v = ix._conn.execute("SELECT value FROM settings WHERE key = 'windows_version'").fetchone()[0]
    assert n == len(index._doc_texts(ix.get("long"))) and v == str(index.WINDOWS_VERSION)  # type: ignore[arg-type]
    ix.close()
    ix = index.Index(db, FakeEmbedder())  # once only
    with ix._lock:
        assert ix._conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0


def test_a_new_store_needs_no_re_embedding(tmp_path: Path) -> None:
    ix = index.Index(tmp_path / "scout.db", FakeEmbedder())
    with ix._lock:
        v = ix._conn.execute("SELECT value FROM settings WHERE key = 'windows_version'").fetchone()
    assert v == (str(index.WINDOWS_VERSION),)
