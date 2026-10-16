"""GitHub items as records (github.py): fetched from a fake GitHub API on a local port
(the HTTP boundary), with ETags, private repos, missing items and the rate limit; the
token from config or gh. Synthetic data only."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scout import config, github, index, links, recordpage

NOW = time.time()


class FakeGitHub:
    """api.github.com, the parts Scout asks for: /user, /repos/o/r, /repos/o/r/issues/n,
    /repos/o/r/commits/sha. Records every request (path, auth, If-None-Match)."""

    def __init__(self) -> None:
        self.repos: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}  # path -> JSON
        self.seen: list[dict[str, str]] = []
        self.limited = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.seen.append({"path": self.path, "auth": self.headers.get("Authorization") or "",
                                  "etag": self.headers.get("If-None-Match") or ""})  # fmt: skip
                if fake.limited:
                    self._send(403, {"message": "API rate limit exceeded"},
                               {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(NOW + 600))})  # fmt: skip
                    return
                if self.path == "/user":
                    self._send(200, {"login": "mjx"})
                    return
                if self.path.count("/") == 3 and self.path[len("/repos/") :] in fake.repos:
                    self._send(200, fake.repos[self.path[len("/repos/") :]])
                    return
                data = fake.items.get(self.path)
                if data is None:
                    self._send(404, {"message": "Not Found"})
                    return
                tag = f'"{abs(hash(json.dumps(data, sort_keys=True)))}"'
                if self.headers.get("If-None-Match") == tag:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.end_headers()
                    return
                self._send(200, data, {"ETag": tag})

            def _send(self, code: int, data: Any, headers: dict[str, str] | None = None) -> None:
                body = json.dumps(data).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: Any) -> None:
                return None

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(5)


def pr(n: int, title: str, state: str = "open", merged: bool = False, user: str = "luca",
       assignees: tuple[str, ...] = (), repo: str = "acme/api") -> dict[str, Any]:  # fmt: skip
    return {
        "number": n, "title": title, "state": state, "user": {"login": user},
        "assignees": [{"login": a} for a in assignees], "body": f"Body of {title}.",
        "html_url": f"https://github.com/{repo}/pull/{n}", "updated_at": "2026-10-14T12:00:00Z",
        "closed_at": "2026-10-14T12:00:00Z" if state == "closed" else None,
        "pull_request": {"merged_at": "2026-10-14T12:00:00Z" if merged else None},
    }  # fmt: skip


def issue(n: int, title: str, state: str = "open", repo: str = "acme/api") -> dict[str, Any]:
    return {"number": n, "title": title, "state": state, "user": {"login": "pat"}, "assignees": [],
            "body": "", "html_url": f"https://github.com/{repo}/issues/{n}", "updated_at": "2026-10-13T09:00:00Z"}  # fmt: skip


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    f = FakeGitHub()
    f.repos = {"acme/api": {"private": True}, "pub/lib": {"private": False}}
    yield f
    f.close()


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    return s


def cfg() -> config.GithubConfig:
    return config.GithubConfig(enabled=True)


def test_named_items_become_records_with_state_people_and_privacy(
    store: index.Index, gh: FakeGitHub, tmp_path: Path
) -> None:
    gh.items = {
        "/repos/acme/api/issues/117": pr(117, "Fix the jitter buffer", "closed", merged=True, assignees=("mjx",)),
        "/repos/pub/lib/issues/9": issue(9, "Crash on start"),
        "/repos/pub/lib/commits/abcdef123456": {
            "sha": "abcdef1234567890", "html_url": "https://github.com/pub/lib/commit/abcdef1234567890",
            "author": {"login": "sam"}, "commit": {"message": "Retry the upload\n\nmore", "author": {"date": "2026-10-12T08:00:00Z"}},
        },
    }  # fmt: skip
    store.link("slack:C1:1.000000", "github:acme/api#117", "mentions")
    store.link("note:1", "github:pub/lib#9", "mentions")
    store.link("note:1", "github:pub/lib@abcdef123456", "mentions")
    store.link("note:1", "github:gone/repo#1", "mentions")
    get = github.fetcher("tok-123", gh.base)
    _, changed = github.sync_pass(store, get, cfg(), tmp_path / "gh.json", NOW)
    assert changed == 3
    p = store.get("github:acme/api#117")
    assert p is not None and p.kind == "github" and p.title == "Fix the jitter buffer"
    assert p.meta["state"] == "merged" and p.meta["type"] == "pr" and p.meta["private"] is True
    assert p.meta["author"] == "luca" and p.meta["mine"] is True and "luca" in p.who
    i = store.get("github:pub/lib#9")
    assert i is not None and i.meta["state"] == "open" and i.meta["private"] is False
    c = store.get("github:pub/lib@abcdef123456")
    assert c is not None and c.meta["state"] == "committed" and c.title == "Retry the upload"
    assert store.get("github:gone/repo#1") is None  # no such repo: tried again tomorrow
    assert all(r["auth"] == "Bearer tok-123" for r in gh.seen)
    # resolve: its link is the item's page; private when its repo is
    r = store.resolve("github:acme/api#117")
    assert r is not None and r["url"] == "https://github.com/acme/api/pull/117" and r["private"] is True
    # The Slack message that named it isn't stored here: left out; its people are linked.
    assert all(x["kind"] == "person" for x in r["related"])
    assert ("person:github:luca", "from") in {(x["id"], x["rel"]) for x in r["related"]}
    pub = store.resolve("github:pub/lib#9")
    assert pub is not None and pub["private"] is False


def test_refreshes_send_the_etag_and_a_304_costs_no_change(
    store: index.Index, gh: FakeGitHub, tmp_path: Path
) -> None:
    gh.items = {"/repos/pub/lib/issues/9": issue(9, "Crash on start")}
    store.link("note:1", "github:pub/lib#9", "mentions")
    get = github.fetcher("t", gh.base)
    state = tmp_path / "gh.json"
    github.sync_pass(store, get, cfg(), state, NOW)
    gh.seen.clear()
    assert github.sync_pass(store, get, cfg(), state, NOW + 60) == (0, 0)  # open: not due for 30 minutes
    _, changed = github.sync_pass(store, get, cfg(), state, NOW + 31 * 60)
    assert changed == 0 and gh.seen[-1]["etag"]  # If-None-Match sent, 304 back
    gh.items["/repos/pub/lib/issues/9"] = issue(9, "Crash on start", "closed")
    github.sync_pass(store, get, cfg(), state, NOW + 62 * 60)
    h = store.get("github:pub/lib#9")
    assert h is not None and h.meta["state"] == "closed"
    gh.seen.clear()
    assert github.sync_pass(store, get, cfg(), state, NOW + 3 * 86400) == (0, 0)  # closed: weekly
    github.sync_pass(store, get, cfg(), state, NOW + 8 * 86400)
    assert [r["path"] for r in gh.seen] == ["/repos/pub/lib", "/repos/pub/lib/issues/9"]


def test_the_rate_limit_stops_the_pass(store: index.Index, gh: FakeGitHub, tmp_path: Path) -> None:
    gh.items = {"/repos/pub/lib/issues/9": issue(9, "Crash")}
    store.link("note:1", "github:pub/lib#9", "mentions")
    gh.limited = True
    state = tmp_path / "gh.json"
    github.sync_pass(store, github.fetcher("t", gh.base), cfg(), state, NOW)
    assert github.load_state(state)["limited_until"] == int(NOW + 600)
    assert store.get("github:pub/lib#9") is None


def test_the_allow_list_and_a_pass_limit_bound_what_is_asked(
    store: index.Index, gh: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(1, 6):
        gh.items[f"/repos/pub/lib/issues/{n}"] = issue(n, f"Issue {n}", repo="pub/lib")
        store.link("note:1", f"github:pub/lib#{n}", "mentions")
    store.link("note:1", "github:other/x#1", "mentions")
    c = cfg()
    c.orgs = ["pub"]
    monkeypatch.setattr(github, "MAX_PER_PASS", 4)
    asked, _ = github.sync_pass(store, github.fetcher("t", gh.base), c, tmp_path / "gh.json", NOW)
    assert asked == 4  # /user, the repo, two items
    assert not any("/other/" in r["path"] for r in gh.seen)
    assert store.count("github") == 2


def test_the_token_is_the_configured_one_else_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert github.token("  conf-tok ") == "conf-tok"
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\n[ \"$1 $2\" = 'auth token' ] && echo gh-tok\n")
    fake.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake))
    assert github.token("") == "gh-tok"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(github, "GH_PATHS", (str(tmp_path / "missing"),))
    assert github.token("") == ""


def test_a_401_reads_the_token_again(store: index.Index, tmp_path: Path) -> None:
    seen: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization") or "")
            ok = self.headers.get("Authorization") == "Bearer new"
            body = json.dumps({"login": "mjx"} if ok else {"message": "Bad credentials"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        tokens = iter(["old", "new"])
        get = github.fetcher(lambda: next(tokens), f"http://127.0.0.1:{srv.server_address[1]}")
        assert get("/user", "").data == {"login": "mjx"}
        assert seen == ["Bearer old", "Bearer new"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_the_record_page_shows_a_github_item_and_its_mentions(store: index.Index) -> None:
    store.put("github:pub/lib#9", "github", "Crash on start", "Issue 9 in pub/lib: Crash on start", "pat", NOW,
              {"repo": "pub/lib", "number": 9, "type": "issue", "state": "open",
               "url": "https://github.com/pub/lib/issues/9", "private": False})  # fmt: skip
    store.put("slack:C1:5.000000", "slack", "#launch", "Pat: see pub/lib#9", "Pat", NOW, {"channel": "C1"})
    store.link("slack:C1:5.000000", "github:pub/lib#9", "mentions")
    r = store.resolve("github:pub/lib#9")
    assert r is not None
    page = recordpage.render(r, store.get("github:pub/lib#9"))
    assert "GitHub" in page and "https://github.com/pub/lib/issues/9" in page
    assert "Mentioned by" in page and "open" in page
    assert links.GITHUB_KIND == "github"
