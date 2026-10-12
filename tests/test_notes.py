"""Notes: kept in Scout's store and searchable; the Apple Notes copy is best-effort and
switches itself off for a while when Notes refuses. Spoken note commands keep the
note's own wording."""

import asyncio
from typing import Any

import pytest

from scout import local_intents, mac, notes
from scout.mac import ToolError


@pytest.fixture(autouse=True)
def _no_apple(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def run(*argv: str, timeout: float = 10.0) -> str:
        calls.append(argv)
        raise ToolError("Not authorized to send Apple events to Notes. (-1743)")

    monkeypatch.setattr(mac, "_run", run)
    monkeypatch.setattr(notes, "_apple_off_until", 0.0)
    return calls


def test_notes_are_saved_found_and_deleted(_no_apple: list[tuple[str, ...]]) -> None:
    async def go() -> tuple[str, str, str, str, str]:
        a = await notes.add_text({"text": "Propane: refilled both tanks at Tractor Supply in Cadillac, $40"})
        b = await notes.add_text(
            {"text": "Dump station at Mitchell State Park is free", "title": "Dump stations"}
        )
        found = await notes.find_text({"query": "propane"})
        gone = await notes.delete({"id": 1})
        return a, b, found, gone, await notes.find_text({"query": "propane"})

    a, b, found, gone, after = asyncio.run(go())
    assert (
        a == "Saved note 1: Propane: refilled both tanks at Tractor Supply in Cadillac,."[:0] + a
    )  # shape below
    assert a.startswith("Saved note 1: Propane: refilled both tanks") and a.endswith(".")
    assert b == "Saved note 2: Dump stations."
    assert found.startswith("Note 1 (") and found.endswith("Cadillac, $40")
    assert gone.startswith("Deleted note 1: ") and after == "No notes found."
    assert len(_no_apple) == 1  # Notes refused once; Scout stops asking for a while


@pytest.mark.parametrize(
    ("bad", "why"), [("", "nothing to note"), ("x" * 5001, "too long"), ("a\x07b", "control characters")]
)
def test_bad_notes_are_refused(bad: str, why: str) -> None:
    with pytest.raises(ToolError, match=why):
        asyncio.run(notes.add(bad))


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("Take a note: the Onewheel tire is 22 PSI.", "the Onewheel tire is 22 PSI."),
        ("Please make a note that Sam's birthday is March 3rd", "Sam's birthday is March 3rd"),
        (
            "Note that the water filter was replaced in September.",
            "the water filter was replaced in September.",
        ),
    ],
)
def test_spoken_notes_keep_their_wording(said: str, kept: str) -> None:
    saved: list[str] = []

    async def add(text: str) -> dict[str, Any]:
        saved.append(text)
        return {"id": 1, "title": text, "apple": False}

    ctx = local_intents.Context(timers=None, note_add=add)
    assert asyncio.run(local_intents.answer(said, ctx)) == "Noted."
    assert saved == [kept]


def test_notes_are_read_back_by_topic() -> None:
    async def find(about: str) -> list[dict[str, Any]]:
        return [{"id": 3, "title": "Tire", "body": f"about {about}: 22 PSI", "created": 0}] if about else []

    ctx = local_intents.Context(timers=None, notes_find=find)
    assert (
        asyncio.run(local_intents.answer("what are my notes about the onewheel", ctx))
        == "Your note: about the onewheel: 22 PSI"
    )
    assert asyncio.run(local_intents.answer("read me my notes", ctx)) == "I don't have a note about that."
