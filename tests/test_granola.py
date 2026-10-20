"""Granola notes into the local store (granola.py), against a fake of the public API in
the shapes docs.granola.ai documents. Synthetic notes only; no network."""

import asyncio
from collections.abc import Iterable
from typing import Any

import pytest

from scout import granola, index


def note(nid: str, updated: str, lines: int = 3, title: str = "Budget review") -> dict[str, Any]:
    return {
        "id": nid, "object": "note", "title": title,
        "owner": {"name": "Me", "email": "me@example.com"},
        "created_at": "2026-10-01T15:00:00Z", "updated_at": updated, "deleted_at": None,
        "web_url": f"https://notes.granola.ai/d/{nid}",
        "calendar_event": {"event_title": title, "invitees": [], "organiser": None, "calendar_event_id": None,
                           "scheduled_start_time": "2026-10-01T15:00:00Z", "scheduled_end_time": None},
        "attendees": [{"name": "Pat Doe", "email": "pat@example.com"}, {"name": None, "email": "sam@example.com"}],
        "folder_membership": [], "summary_text": "Agreed to cut travel by 10%.",
        "summary_markdown": "## Decisions\n- Cut travel by 10%", "private_notes_text": None,
        "transcript": [
            {"speaker": {"source": "microphone" if i % 2 == 0 else "speaker", **({"name": "Pat Doe"} if i % 2 else {})},
             "text": f"line {i} about the travel budget", "start_time": "2026-10-01T15:00:00Z",
             "end_time": "2026-10-01T15:00:05Z"}
            for i in range(lines)
        ],
    }  # fmt: skip


class FakeGranola:
    def __init__(self, notes: list[dict[str, Any]], unfinished: Iterable[str] = (), big: Iterable[str] = ()) -> None:  # fmt: skip
        self.notes = {n["id"]: n for n in notes}
        self.unfinished, self.big = set(unfinished), set(big)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, query: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, query))
        if path == "/v1/notes":
            after = query.get("updated_after")
            listed = sorted((n for n in self.notes.values() if after is None or n["updated_at"] > after),
                            key=lambda n: n["updated_at"])  # fmt: skip
            start = int(query.get("cursor") or 0)
            page = listed[start : start + query["page_size"]]
            more = start + len(page) < len(listed)
            summary = ("id", "object", "title", "owner", "created_at", "updated_at", "deleted_at")
            return {"notes": [{k: n[k] for k in summary} for n in page], "hasMore": more,
                    "cursor": str(start + len(page)) if more else None}  # fmt: skip
        nid = path.split("/")[3]
        if nid in self.unfinished:
            raise granola.GranolaError(f"{path}: HTTP 404")
        n = dict(self.notes[nid])
        if path.endswith("/transcript"):
            start = int(query.get("cursor") or 0)
            part = n["transcript"][start : start + query["page_size"]]
            more = start + len(part) < len(n["transcript"])
            return {"transcript": part, "hasMore": more, "cursor": str(start + len(part)) if more else None}
        if query.get("include") == "transcript":
            if nid in self.big:
                raise granola.GranolaError(f"{path}: HTTP 413")
            return n
        n["transcript"] = None
        return n


@pytest.fixture
def ix(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> index.Index:
    monkeypatch.setattr(granola, "PENDING", tmp_path / "pending.json")
    monkeypatch.setattr(granola, "RESYNC", tmp_path / "resync.json")
    return index.Index(tmp_path / "m.db", None)


def test_a_note_becomes_a_summary_and_transcript_chunks() -> None:
    docs = granola.documents(note("not_aaaaaaaaaaaaaa", "2026-10-01T16:00:00Z", lines=60))
    assert docs[0]["id"] == "granola:not_aaaaaaaaaaaaaa" and docs[0]["title"] == "Budget review"
    assert docs[0]["body"].startswith("## Decisions") and docs[0]["who"] == "Pat Doe, sam@example.com"
    assert docs[0]["ts"] == 1_790_866_800.0  # 2026-10-01 15:00 UTC
    chunks = docs[1:]
    assert len(chunks) >= 2 and all(len(c["body"]) <= granola.CHUNK_CHARS + 60 for c in chunks)
    assert chunks[0]["body"].splitlines()[:2] == [
        "Me: line 0 about the travel budget",
        "Pat Doe: line 1 about the travel budget",
    ]
    assert chunks[0]["id"] == "granola:not_aaaaaaaaaaaaaa#1" and chunks[0]["meta"]["part"] == 1


def test_a_full_sync_then_only_changes(ix: index.Index) -> None:
    api = FakeGranola([note(f"not_{i:014d}", f"2026-10-0{1 + i % 9}T10:00:{i:02d}Z") for i in range(35)])
    notes, _, newest = granola.sync_pass(ix, api, None)
    assert notes == 35 and newest == max(n["updated_at"] for n in api.notes.values())
    assert [h.kind for h in ix.search("travel budget", semantic=False)][:1] == ["meeting"]
    api.calls.clear()
    assert granola.sync_pass(ix, api, newest)[:2] == (0, 0)
    assert [p for p, _ in api.calls] == ["/v1/notes"]  # nothing changed: one list call
    edited = note("not_00000000000003", "2026-10-20T09:00:00Z", lines=1, title="Budget review (final)")
    api.notes[edited["id"]] = edited
    notes, _, newest2 = granola.sync_pass(ix, api, newest)
    assert notes == 1 and newest2 == "2026-10-20T09:00:00Z"
    final = ix.get("granola:not_00000000000003")
    assert final is not None and final.title == "Budget review (final)"
    assert ix.get("granola:not_00000000000003#2") is None  # the shorter transcript's leftovers are gone


def test_a_large_transcript_is_read_in_pages(ix: index.Index) -> None:
    n = note("not_bbbbbbbbbbbbbb", "2026-10-02T10:00:00Z", lines=250)
    api = FakeGranola([n], big={n["id"]})
    assert granola.sync_pass(ix, api, None)[0] == 1
    pages = [q for p, q in api.calls if p.endswith("/transcript")]
    assert len(pages) == 3 and all(q["page_size"] == 100 for q in pages)
    assert "line 249" in "".join(h.body for h in ix.recent(["meeting"], limit=100))


def test_an_unfinished_note_is_asked_for_again(ix: index.Index) -> None:
    done = note("not_cccccccccccccc", "2026-10-03T10:00:00Z")
    waiting = note("not_dddddddddddddd", "2026-10-02T12:00:00Z")
    api = FakeGranola([done, waiting], unfinished={waiting["id"]})
    notes, _, newest = granola.sync_pass(ix, api, None)
    assert notes == 1 and newest == done["updated_at"]  # the resume point moves on past it
    api.unfinished.clear()
    assert granola.sync_pass(ix, api, newest)[0] == 1  # asked for by id
    assert ix.get("granola:not_dddddddddddddd") is not None


def test_other_errors_stop_the_pass(ix: index.Index) -> None:
    def broken(path: str, query: dict[str, Any]) -> dict[str, Any]:
        raise granola.GranolaError("/v1/notes: HTTP 401")

    with pytest.raises(granola.GranolaError, match="401"):
        granola.sync_pass(ix, broken, None)


def test_speaker_names() -> None:
    assert granola.speaker({"speaker": {"source": "speaker", "name": "Pat"}}) == "Pat"
    assert granola.speaker({"speaker": {"source": "microphone"}}) == "Me"
    assert (
        granola.speaker(
            {"speaker": {"source": "speaker", "attribution": "them", "diarization_label": "Speaker B"}}
        )
        == "Speaker B"
    )
    assert granola.speaker({"speaker": {"source": "speaker"}}) == "Them"


def test_meetings_stay_titles_only_outside_open_mode(
    ix: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout import privacy, shared_tools

    granola.store_note(ix, note("not_eeeeeeeeeeeeee", "2026-10-04T10:00:00Z"))
    monkeypatch.setattr(index, "_index", ix)
    monkeypatch.setattr(privacy, "_current", privacy.Policy("balanced"))
    out = asyncio.run(shared_tools._local_search({"query": "travel budget", "kinds": ["meeting"]}))
    assert "Budget review" in out and "line 0" not in out
    monkeypatch.setattr(privacy, "_current", privacy.Policy("strict"))
    assert "Budget review" not in asyncio.run(shared_tools._local_search({"query": "travel budget"}))


def test_notes_stored_before_attendees_were_kept_are_read_again_once(
    ix: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scout import people

    ix.people_ctx = lambda: people.Ctx(me_emails=frozenset({"me@example.com"}))
    notes = [note(f"not_{i:014d}", f"2026-10-0{1 + i % 9}T10:00:00Z") for i in range(5)]
    notes[4]["attendees"] = []  # a note nobody's listed on
    for n in notes:  # as stored before: attendees by name only
        granola.store_note(
            ix, {**n, "attendees": [{"name": a["name"] or a["email"]} for a in n["attendees"]]}
        )
    for i in range(5):  # none has attendees in its meta
        h = ix.get(f"granola:not_{i:014d}")
        assert h is not None
        ix.put(
            h.id,
            "meeting",
            h.title,
            h.body,
            h.who,
            h.ts,
            {k: v for k, v in h.meta.items() if k != "attendees"},
        )
    assert sorted(ix.granola_without_attendees()) == sorted(n["id"] for n in notes)
    monkeypatch.setattr(granola, "RESYNC_PER_PASS", 3)
    api = FakeGranola(notes)
    newest = max(n["updated_at"] for n in notes)
    granola.sync_pass(ix, api, newest)  # nothing changed since: only the old notes are asked for
    asked = [p for p, _ in api.calls if p != "/v1/notes"]
    assert len(asked) == 3
    api.calls.clear()
    granola.sync_pass(ix, api, newest)
    assert len([p for p, _ in api.calls if p != "/v1/notes"]) == 2
    assert ix.granola_without_attendees() == ["not_00000000000004"]  # still nobody listed
    # Linked by address now, not by name.
    pat = [ln["id"] for ln in ix.links("granola:not_00000000000000") if ln["dir"] == "out"]
    assert "person:email:pat@example.com" in pat
    # Done: never asked again, even for a note that still lists nobody.
    api.calls.clear()
    granola.sync_pass(ix, api, newest)
    assert [p for p, _ in api.calls] == ["/v1/notes"]


def test_a_note_gone_from_granola_leaves_the_re_read(ix: index.Index) -> None:
    n = note("not_eeeeeeeeeeeeee", "2026-10-03T10:00:00Z")
    granola.store_note(ix, n)
    h = ix.get("granola:not_eeeeeeeeeeeeee")
    assert h is not None
    ix.put(
        h.id, "meeting", h.title, h.body, h.who, h.ts, {k: v for k, v in h.meta.items() if k != "attendees"}
    )
    api = FakeGranola([n], unfinished={n["id"]})
    granola.sync_pass(ix, api, n["updated_at"])
    api.calls.clear()
    granola.sync_pass(ix, api, n["updated_at"])  # asked again as an unfinished note, not as a re-read
    assert len([p for p, _ in api.calls if p != "/v1/notes"]) == 1
    assert granola._load_resync(granola.RESYNC) == []
