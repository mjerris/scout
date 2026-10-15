"""The record page on Scout's web UI (GET /r/<id>, recordpage.py): token-gated like the
rest of the page, escaped, with an Open button and the linked records. Synthetic records,
a stub assistant, a server on a free 127.0.0.1 port."""

import asyncio
import datetime as dt
import time
from typing import Any

import aiohttp
import pytest

from scout import granola, index, recordpage

from test_web import TOKEN, serve, token_file  # noqa: F401 (the fixture)

BASE = "https://mini.tail1234.ts.net"
pytestmark = pytest.mark.usefixtures("token_file")


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    s.web_base = lambda: BASE
    granola.store_note(s, {
        "id": "not_a", "title": "Budget <review>", "web_url": "https://notes.granola.ai/d/not_a",
        "summary_text": "We agreed on the numbers.", "attendees": [{"name": "Pat Doe"}],
        "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(time.time() - 86400, dt.UTC).isoformat()},
    })  # fmt: skip
    return s


def get(path: str, **kw: Any) -> tuple[int, str, dict[str, str]]:
    async def main() -> tuple[int, str, dict[str, str]]:
        async with serve() as srv, aiohttp.ClientSession() as s, s.get(srv.url + path, **kw) as r:
            return r.status, await r.text(), dict(r.headers)

    return asyncio.run(main())


def test_a_task_page_shows_its_fields_source_and_open_button(store: index.Index) -> None:
    n = store.task_add("Send Pat the <b>budget</b>", {"source": "meeting:not_a", "from": "meeting", "owner": "Pat"},
                       'From the meeting: "send it" (meeting:not_a)')  # fmt: skip
    status, page, headers = get(f"/r/task%3A{n}?token={TOKEN}")
    assert status == 200
    assert "Send Pat the &lt;b&gt;budget&lt;/b&gt;" in page and "<b>budget</b>" not in page
    assert 'class="open" href="https://notes.granola.ai/d/not_a"' in page
    assert "<th>owner</th><td>Pat</td>" in page and "<th>source</th><td>meeting:not_a</td>" in page
    assert "Found in" in page and 'href="/r/granola%3Anot_a"' in page and "Budget &lt;review&gt;" in page
    assert "script" not in page.lower()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"


def test_a_source_page_links_back_to_the_task(store: index.Index) -> None:
    n = store.task_add("Send it", {"source": "meeting:not_a"}, "")
    status, page, _ = get(f"/r/granola:not_a?token={TOKEN}")
    assert status == 200 and "A task found here" in page and f'href="/r/task%3A{n}"' in page
    assert 'class="open" href="https://notes.granola.ai/d/not_a"' in page


def test_record_pages_need_the_token(store: index.Index) -> None:
    n = store.task_add("Send it", {}, "")
    status, page, _ = get(f"/r/task%3A{n}")
    assert status == 401 and "Send it" not in page
    status, _, _ = get(f"/r/task%3A{n}", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_an_unknown_record_is_not_found(store: index.Index) -> None:
    status, page, _ = get(f"/r/task%3A999?token={TOKEN}")
    assert status == 404 and "no record task:999" in page


def test_a_down_memory_service_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def resolve(self, id: str) -> None:
            raise RuntimeError("down")

    monkeypatch.setattr(index, "_index", Down())
    status, page, _ = get(f"/r/task%3A1?token={TOKEN}")
    assert status == 503 and "memory service" in page


def test_only_safe_links_are_put_on_the_page() -> None:
    rec = {"id": "x", "kind": "task", "title": "t", "url": "javascript:alert(1)", "page": None,
           "related": [{"id": "y", "rel": "about", "dir": "in", "kind": "note", "title": "n", "url": "data:x"}]}  # fmt: skip
    page = recordpage.render(rec, None)
    assert "javascript:" not in page and "data:x" not in page and 'href="/r/y"' in page
    assert recordpage.safe_url("message://%3Ca@b%3E") and recordpage.safe_url("https://x.example/a")
