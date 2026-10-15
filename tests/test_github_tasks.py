"""Follow-up tasks about GitHub items: the farm links a task to the item it's about (and
its link is the item's page), the item closing or merging marks your task done or is
said once by the pulse for a waiting-on one, tier 0 answers "status of PR 117" and
"what GitHub issues am I waiting on", and resolve / where_from / the github_items tool
know the records. Synthetic data only."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from typing import Any

import pytest

from scout import (
    config,
    github,
    index,
    local_intents,
    privacy,
    pulse,
    refs,
    reminders_mac,
    shared_tools,
    taskfarm,
    tasks,
)

NOW = time.time()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index) and s.journal_root is not None
    s.web_base = lambda: "https://mini.example.ts.net"

    async def no_reminders(helper: Any = None) -> bool:
        return False

    monkeypatch.setattr(reminders_mac, "available", no_reminders)
    monkeypatch.setattr(tasks, "_people", lambda: [])
    cfg = config.Config()
    cfg.tasks.me = ["Mike Jerris"]
    for mod in (config, tasks, taskfarm):
        monkeypatch.setattr(mod, "load", lambda: cfg)
    return s


def gh_item(s: index.Index, repo: str, n: int, title: str, state: str = "open", kind: str = "pr",
            private: bool = False, **meta: Any) -> str:  # fmt: skip
    id = f"github:{repo}#{n}"
    path = "pull" if kind == "pr" else "issues"
    s.put(id, "github", title, f"{title}", meta.get("author", "luca"), NOW - 3600,
          {"repo": repo, "number": n, "type": kind, "state": state, "author": "luca", "assignees": [],
           "url": f"https://github.com/{repo}/{path}/{n}", "private": private,
           "merged_at": "2026-10-14T12:00:00Z" if state == "merged" else None, **meta})  # fmt: skip
    return id


def slack_msg(s: index.Index, ch: str, ts: float, text: str, who: str = "Luca") -> None:
    t = f"{ts:.6f}"
    s.put(f"slack:{ch}:{t}", "slack", "#voice", f"{who}: {text}", who, ts,
          {"channel": ch, "conversation": "#voice", "ts": t, "last_ts": t,
           "permalink": f"https://team.slack.com/archives/{ch}/p{t.replace('.', '')}"})  # fmt: skip


def farm(s: index.Index, answer: dict[str, Any]) -> None:
    async def local(system: str, text: str) -> str:
        return json.dumps(answer)

    asyncio.run(taskfarm.farm_pass(s, None, local, now=NOW, state_path=s.path.parent / "farm.json"))


def test_a_task_about_a_pr_links_to_it_and_opens_it(store: index.Index) -> None:
    gh_item(store, "acme/api", 117, "Fix the jitter buffer")
    slack_msg(store, "C1", NOW - 600, "can you review https://github.com/acme/api/pull/117 today?")
    farm(store, {"tasks": [{"title": "Review PR 117", "owner": "me", "evidence": "can you review"}]})
    [h] = store.recent(["task"])
    assert h.meta["github"] == "acme/api#117" and h.meta["link"] == "https://github.com/acme/api/pull/117"
    ls = {(x["id"], x["rel"]) for x in store.links(h.id)}
    assert ("github:acme/api#117", "mentions") in ls and ("slack:C1:" + f"{NOW - 600:.6f}", "found_in") in ls
    r = store.resolve(h.id)
    assert r is not None and r["url"] == "https://github.com/acme/api/pull/117"
    assert refs.best_url(r) == "https://github.com/acme/api/pull/117"  # not the Slack thread
    back = store.resolve("github:acme/api#117")
    assert back is not None and any(x["id"] == h.id and x["dir"] == "in" for x in back["related"])


def test_a_task_not_about_the_item_gets_no_github_line(store: index.Index) -> None:
    slack_msg(
        store, "C1", NOW - 600, "see https://github.com/acme/api/pull/117; also can you send the budget?"
    )
    farm(
        store, {"tasks": [{"title": "Send the budget", "owner": "me", "evidence": "can you send the budget"}]}
    )
    [h] = store.recent(["task"])
    assert "github" not in h.meta
    assert str(h.meta.get("link") or "").startswith("https://team.slack.com/")


def test_your_task_is_done_when_its_pr_merges(store: index.Index) -> None:
    gh_item(store, "acme/api", 117, "Fix the jitter buffer")
    n = store.task_add("Review PR 117", {"owner": "me", "github": "acme/api#117", "from": "slack"})
    other = store.task_add("Send the deck", {"owner": "me", "github": "acme/api#117", "from": "slack"})
    assert asyncio.run(tasks.github_followups(store)) == 0  # still open
    gh_item(store, "acme/api", 117, "Fix the jitter buffer", "merged")
    assert asyncio.run(tasks.github_followups(store)) == 1
    t = tasks.of(store.get(f"task:{n}"))  # type: ignore[arg-type]
    assert t.status == "done" and "PR 117 in api was merged" in t.context
    assert tasks.of(store.get(f"task:{other}")).status == "open"  # type: ignore[arg-type]  # its title isn't the PR


def test_the_pulse_says_a_merged_waiting_on_item_once(store: index.Index) -> None:
    gh_item(store, "acme/api", 117, "Fix the jitter buffer", "merged")
    n = store.task_add("Luca merges PR 117", {"owner": "Luca", "github": "acme/api#117", "from": "slack",
                                              "due": "2026-01-01"})  # fmt: skip

    async def none(*a: Any) -> dict[str, Any]:
        return {"events": [], "reminders": []}

    async def ts() -> list[tasks.Task]:
        return tasks.all_tasks(store)

    async def rec(id: str) -> Any:
        return store.get(id)

    async def note(n: int, f: dict[str, Any]) -> Any:
        return store.task_update(n, f)

    p = pulse.Pulse(10, 15, none, none, ts, lambda: [], rec, note)
    now = dt.datetime.now().astimezone()
    said = asyncio.run(p.check(now))
    merged = [s for s in said if "merged" in s]
    assert merged == [
        "PR 117 in api, Fix the jitter buffer, was merged on October 14. You were waiting on Luca for it."
    ]
    assert f"task:{n}" in refs.refs_of(merged[0])
    assert not any("maybe follow up" in s for s in said)  # no nagging about a merged one
    assert tasks.of(store.get(f"task:{n}")).fields["github_said"] == "merged"  # type: ignore[arg-type]
    p2 = pulse.Pulse(10, 15, none, none, ts, lambda: [], rec, note)  # a restart: still said once
    assert not any("merged" in s for s in asyncio.run(p2.check(now)))


def ctx(store: index.Index) -> local_intents.Context:
    async def items() -> list[Any]:
        return store.recent(["github"], 5000)

    async def ts() -> list[tasks.Task]:
        return tasks.all_tasks(store)

    c = local_intents.Context(None)
    c.github_items, c.task_list = items, ts
    return c


@pytest.mark.parametrize("text", ["What's the status of PR 117?", "is PR 117 merged", "has pull request #117 been merged yet",
                                  "what happened with PR 117"])  # fmt: skip
def test_tier0_says_a_prs_status(store: index.Index, text: str) -> None:
    gh_item(store, "acme/api", 117, "Fix the jitter buffer", "merged")
    c = ctx(store)
    got = asyncio.run(local_intents.answer(text, c))
    assert got == "PR 117 in api, Fix the jitter buffer, was merged on October 14."
    assert c.refs == ["github:acme/api#117"]


def test_tier0_unknown_item_goes_to_claude_and_private_is_marked(store: index.Index) -> None:
    gh_item(store, "acme/api", 9, "Crash", "open", kind="issue", private=True, assignees=["sam"])
    c = ctx(store)
    assert asyncio.run(local_intents.answer("what's the status of PR 404", c)) is None
    got = asyncio.run(local_intents.answer("what's the status of issue 9", c))
    assert got is not None and got.startswith("issue 9 in api, Crash, is open, by luca, assigned to sam")
    assert c.private is True


def test_tier0_github_waiting(store: index.Index) -> None:
    c = ctx(store)
    assert asyncio.run(local_intents.answer("what GitHub issues am I waiting on", c)) is None  # none stored
    gh_item(store, "acme/api", 117, "Fix the jitter buffer")
    gh_item(store, "acme/web", 52, "Add retries", by_me=True)
    gh_item(store, "acme/web", 53, "Done one", "merged", by_me=True)
    store.task_add("Luca merges PR 117", {"owner": "Luca", "github": "acme/api#117"})
    got = asyncio.run(local_intents.answer("What PRs am I waiting on?", c))
    assert got == ("On GitHub you're waiting on two: Luca, on PR 117 in api, Fix the jitter buffer; "
                   "your PR 52 in web, Add retries, still open.")  # fmt: skip
    assert "github:acme/api#117" in c.refs and "github:acme/web#52" in c.refs


def test_where_did_that_come_from_for_a_github_record(store: index.Index) -> None:
    gh_item(store, "acme/api", 117, "Fix the jitter buffer", "merged")
    slack_msg(store, "C1", NOW - 600, "review https://github.com/acme/api/pull/117 please", "Pat Doe")
    store.link_xrefs()
    r = store.resolve("github:acme/api#117")
    assert r is not None and refs.best_url(r) == "https://github.com/acme/api/pull/117"
    said = asyncio.run(refs.where_from(r, store))
    d = dt.datetime.fromtimestamp(NOW - 600)
    assert said == (f"It's PR 117 in api, Fix the jitter buffer. It was merged on October 14. "
                    f"It came up in a Slack message from Pat Doe on {d:%B} {d.day}.")  # fmt: skip


def test_the_github_items_tool_hides_private_titles_in_strict_mode(
    store: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh_item(store, "acme/api", 117, "Secret plan", private=True)
    gh_item(store, "pub/lib", 9, "Public crash", kind="issue")
    out = asyncio.run(github.items_text({"query": "117"}))
    assert "Secret plan" in out and "(ref github:acme/api#117)" in out
    monkeypatch.setattr(privacy, "current", lambda: privacy.Policy("strict"))
    out = asyncio.run(github.items_text({}))
    assert "Secret plan" not in out and "Public crash" in out
    assert set(refs.from_tool("github_items", {}, out)) == {"github:acme/api#117", "github:pub/lib#9"}


def test_local_search_in_strict_mode_leaves_out_private_repos(
    store: index.Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh_item(store, "acme/api", 117, "Secret jitter plan", private=True)
    gh_item(store, "pub/lib", 9, "Public jitter crash", kind="issue")
    monkeypatch.setattr(privacy, "current", lambda: privacy.Policy("strict"))

    async def search() -> str:
        return await shared_tools.BY_NAME["local_search"].run({"query": "jitter"})

    out = asyncio.run(search())
    assert "Public jitter crash" in out and "Secret" not in out
