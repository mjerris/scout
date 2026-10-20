"""A GitHub login joined to the rest of a person (github_people.py, people.py; docs/contacts.md,
People): commit emails from the tracked repos, the public profile's email, a Slack profile
field for GitHub, same-name suggestions the user confirms, and "Brian's GitHub is briankwest"
told to Scout (and forgotten). The API answers are recorded fixtures (tests/data/github_people),
served by a fake fetch; synthetic data only."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from scout import github, github_people, index, memory, people, slack, tools
from scout.memory_service import Service

NOW = time.time()
DATA = Path(__file__).parent / "data" / "github_people"

CTX = people.Ctx(
    me_slack="U0",
    me_emails=frozenset({"me@corp.example"}),
    me_github=frozenset({"melogin"}),
    me_names=frozenset({"michael jerris"}),
)


def fixture(name: str) -> Any:
    return json.loads((DATA / name).read_text())


class FakeGitHub:
    """The GET paths Scout asks, answered from recorded fixtures; every path asked is kept."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, path: str, etag: str = "") -> github.Response:
        self.asked.append(path)
        for prefix, data in self.answers.items():
            if path == prefix or path.startswith(prefix + "&") or path.startswith(prefix + "?"):
                return github.Response(200, data, "")
        return github.Response(404, None, "")


FIRST = "/repos/o/api/commits?per_page=100"


@pytest.fixture
def store() -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    s.people_ctx = lambda: CTX
    return s


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "github_people.json"


def theirs(s: index.Index, pid: str) -> set[str]:
    return {ln["id"] for ln in s.links(pid) if ln["dir"] == "in" and ln["rel"] in people.RELS}


def item(s: index.Index, n: int, author: str) -> str:
    rid = f"github:o/api#{n}"
    s.put(rid, "github", f"Item {n}", "x", author, NOW, {"author": author, "assignees": [], "repo": "o/api"})
    return rid


def tracked(path: Path) -> Path:
    """github.json with one tracked repo (the sync's own state)."""
    gh = path.parent / "github.json"
    gh.write_text(json.dumps({"login": "melogin", "repos": {"o/api": {"private": True, "checked": NOW}}}))
    return gh


# --- 1. commit emails ---------------------------------------------------------------------------


def test_commit_emails_map_logins_skipping_noreply_bots_automation_and_the_user(state: Path) -> None:
    st: dict[str, Any] = {}
    github_people.tally(st, fixture("commits_first.json"), CTX)
    got = github_people.email_map(st, CTX)
    # octo: two commits with octo@ (case folded); the stray lee@ (one commit, not their
    # most used) doesn't hijack a mapping.
    assert got == {"octo": ["octo@acme.example"]}
    seen = set(st["emails"])
    assert not any("noreply" in e for e in seen)  # noreply addresses, web-flow's included
    assert "deploy@acme.example" not in seen and "ci@acme.example" not in seen  # a Bot, automation
    assert "ghost@acme.example" not in seen  # no GitHub user on the commit
    assert "me@corp.example" not in seen  # the user


def test_an_email_two_logins_used_is_not_mapped(state: Path) -> None:
    st: dict[str, Any] = {}
    github_people.tally(st, fixture("commits_first.json"), CTX)
    assert set(st["emails"]["team@acme.example"]) == {"shared1", "shared2"}
    got = github_people.email_map(st, CTX)
    assert "shared1" not in got and "shared2" not in got


def test_commits_are_fetched_incrementally_since_the_newest_seen(store: index.Index, state: Path) -> None:
    gh = tracked(state)
    fake = FakeGitHub({FIRST: fixture("commits_first.json")})
    got = github_people.run_pass(store, fake, state, gh, now=NOW)
    assert got["commits"] == 11 and fake.asked[0] == FIRST
    fake2 = FakeGitHub({FIRST + "&since=2026-10-10T12:00:00Z": fixture("commits_since.json")})
    again = github_people.run_pass(store, fake2, state, gh, now=NOW + 7 * 3600)
    assert fake2.asked[0] == FIRST + "&since=2026-10-10T12:00:00Z"
    assert again["commits"] == 1  # the boundary commit isn't counted twice
    st = json.loads(state.read_text())
    assert st["emails"]["octo@acme.example"]["octo"][0] == 2
    assert github_people.email_map(st, CTX)["sam"] == ["sam@corp.example"]
    # Not due again within six hours: no requests.
    fake3 = FakeGitHub({})
    assert github_people.run_pass(store, fake3, state, gh, now=NOW + 8 * 3600) == {}
    assert fake3.asked == []


def test_a_commit_email_merges_the_login_with_that_address(store: index.Index, state: Path) -> None:
    rid = item(store, 1, "octo")
    store.put("mail:1", "mail", "Hi", "x", "Octo Cat <octo@acme.example>", NOW)
    assert store.count("person") == 2
    got = github_people.run_pass(store, FakeGitHub({FIRST: fixture("commits_first.json")}), state,
                                 tracked(state), now=NOW)  # fmt: skip
    assert got["mapped"] >= 1 and got["merged"] == 1
    owners = store._owners(["github:octo", "email:octo@acme.example"])
    assert len(owners) == 1
    assert theirs(store, owners[0]) == {rid, "mail:1"}
    assert store._owners(["email:lee@acme.example"]) == []  # the stray address made nobody


# --- 2. the public profile ------------------------------------------------------------------------


def test_the_profile_email_merges_and_its_name_is_not_a_key(store: index.Index, state: Path) -> None:
    rid = item(store, 2, "briank")
    store.put("mail:2", "mail", "Hi", "x", "brian@west.example", NOW)
    fake = FakeGitHub({"/users/briank": fixture("user_briank.json")})
    github_people.run_pass(store, fake, state, tracked(state), now=NOW)
    assert "/users/briank" in fake.asked
    owners = store._owners(["github:briank"])
    assert owners == store._owners(["email:brian@west.example"]) and len(owners) == 1
    assert theirs(store, owners[0]) == {rid, "mail:2"}
    assert store._owners(["name:brian west"]) == []  # the profile's name finds nobody
    # Cached: not asked again before the refresh time.
    fake2 = FakeGitHub({"/users/briank": fixture("user_briank.json")})
    github_people.run_pass(store, fake2, state, tracked(state), now=NOW + 7 * 3600)
    assert "/users/briank" not in fake2.asked


# --- 3. Slack's GitHub profile field --------------------------------------------------------------


class FakeSlack:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.asked.append((method, params))
        key = f"{method}:{params.get('user')}" if params.get("user") else method
        got = self.answers.get(key)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise slack.SlackError(f"{method}: user_not_found")
        return dict(got)


USERS = [
    {"id": "U5", "profile": {"real_name": "Brian West", "display_name": "brian"}},
    {"id": "U0", "profile": {"real_name": "Michael Jerris", "display_name": "mike"}},
]


def test_a_slack_github_field_joins_the_slack_user_to_the_login(store: index.Index) -> None:
    rid = item(store, 3, "briank")
    slack.store_docs(store, slack.documents("C1", "#a", [{"ts": "9.0", "user": "U5", "text": "x"}],
                                            lambda u: "Brian West"))  # fmt: skip
    fake = FakeSlack({"team.profile.get": fixture("slack_team_profile.json"),
                      "users.profile.get:U5": fixture("slack_user_profile_U5.json"),
                      "users.profile.get:U0": fixture("slack_user_profile_U0.json")})  # fmt: skip
    st: dict[str, Any] = {}
    got = people.slack_github(fake, USERS, st, NOW)
    assert got == {"U5": "briank", "U0": "melogin"}
    assert st["slack_field"]["id"] == "Xf02"
    store.put_people(people.build({}, USERS, got), complete=False)
    owners = store._owners(["github:briank", "slack:U5"])
    assert owners == ["person:slack:U5"]
    assert theirs(store, "person:slack:U5") == {rid, "slack:C1:9.0"}
    assert store._owners(["github:melogin"]) == []  # the user is never a person
    # Read again later: the field and the profiles are cached.
    fake.asked.clear()
    assert people.slack_github(fake, USERS, st, NOW + 3600) == got and fake.asked == []


def test_no_slack_field_or_no_scope_is_recorded_and_skipped() -> None:
    st: dict[str, Any] = {}
    none = FakeSlack({"team.profile.get": fixture("slack_team_profile_none.json")})
    assert people.slack_github(none, USERS, st, NOW) == {}
    assert st["slack_field"]["id"] == "" and st["slack_field"]["why"] == "none"
    assert [m for m, _ in none.asked] == ["team.profile.get"]
    st2: dict[str, Any] = {}
    scope = FakeSlack({"team.profile.get": slack.SlackError("team.profile.get: missing_scope")})
    assert people.slack_github(scope, USERS, st2, NOW) == {}
    assert st2["slack_field"]["why"] == "missing_scope"
    assert people.slack_github(scope, USERS, st2, NOW + 3600) == {} and len(scope.asked) == 1  # a day later


# --- 4. same name: a suggestion, merged only once the user says yes ------------------------------


def service(tmp_path: Path, s: index.Index) -> Service:
    return Service(s, memory.Memory(tmp_path / "facts.md"), "secret")


def test_same_name_is_a_suggestion_until_accepted_and_a_rejection_is_kept(
    store: index.Index, state: Path, tmp_path: Path
) -> None:
    rid = item(store, 4, "briank")
    slack.store_docs(store, slack.documents("C1", "#a", [{"ts": "9.0", "user": "U5", "text": "x"}],
                                            lambda u: "Brian West"))  # fmt: skip
    store.put_people(people.build({}, USERS[:1]), complete=False)  # Slack's real name: Brian West
    github_people.run_pass(store, FakeGitHub({"/users/briank": fixture("user_briank_noemail.json")}), state,
                           tracked(state), now=NOW)  # fmt: skip
    assert store.count("person") == 2  # not merged by the name
    want = [{"login": "briank", "name": "Brian West", "person": "person:slack:U5", "title": "Brian West",
             "ask": "Is Brian West on GitHub the same as briank?"}]  # fmt: skip
    assert store.people_suggestions("is brian around today") == want
    assert store.people_suggestions("what's the weather") == []
    svc = service(tmp_path, store)
    # No: remembered, and not suggested again.
    assert "remember" in str(memory.answer("remember Brian West's GitHub is not briank", svc.memory)).lower()
    assert store.people_suggestions("brian") == [] and store.count("person") == 2
    github_people.run_pass(store, FakeGitHub({}), state, tracked(state), now=NOW + 7 * 3600)
    assert store.people_suggestions("brian") == []
    # Yes (the same words, so it replaces the no): merged.
    remember = {n: f for n, _, _, f in tools.memory_tools(svc.memory)}["remember"]

    async def keep() -> str:
        return await remember({"fact": "Brian West's GitHub is briank"})

    assert "Remembered" in asyncio.run(keep())
    assert store._owners(["github:briank"]) == ["person:slack:U5"]
    assert theirs(store, "person:slack:U5") == {rid, "slack:C1:9.0"}
    assert store.people_suggestions("brian") == []


# --- 5. told to Scout, and forgotten ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "want"),
    [
        ("Brian's GitHub is briankwest", ("brian", "briankwest", False)),
        ("brian's github username is @BrianKWest", ("brian", "briankwest", False)),
        ("Brian West is brian k west on GitHub", ("brian west", "briankwest", False)),
        ("Brian's GitHub is https://github.com/briankwest/", ("brian", "briankwest", False)),
        ("Brian's GitHub is not briankwest", ("brian", "briankwest", True)),
        ("what's Brian's GitHub", None),
        ("my GitHub is melogin", None),
        ("Brian's GitHub is a mess", None),
    ],
)
def test_told_github_reads_the_fact(said: str, want: tuple[str, str, bool] | None) -> None:
    got = people.told_github(said)
    assert (None if got is None else (got.name, got.login, got.negative)) == want


def test_x_s_github_is_y_adds_the_key_and_forget_removes_it(store: index.Index, tmp_path: Path) -> None:
    rid = item(store, 5, "briankwest")
    slack.store_docs(store, slack.documents("C1", "#a", [{"ts": "9.0", "user": "U5", "text": "x"}],
                                            lambda u: "Brian West"))  # fmt: skip
    store.put_people(people.build({}, USERS[:1]), complete=False)
    svc = service(tmp_path, store)
    said = memory.answer("Brian's GitHub is briankwest", svc.memory)  # tier 0, no "remember" needed
    assert said == "Okay, I'll remember that Brian's GitHub is briankwest."
    assert store._owners(["github:briankwest"]) == ["person:slack:U5"]
    assert theirs(store, "person:slack:U5") == {rid, "slack:C1:9.0"}
    assert store.get("person:github:briankwest") is None
    assert memory.told("Brian's GitHub is briankwest") == "Brian's GitHub is briankwest"
    # Forget: the key goes, and the GitHub item is its login's own again.
    assert str(memory.answer("forget Brian's GitHub", svc.memory)).startswith("Okay, I forgot")
    p = store.get("person:slack:U5")
    assert p is not None and "briankwest" not in p.meta["github"]
    assert store._owners(["github:briankwest"]) == ["person:github:briankwest"]
    assert theirs(store, "person:github:briankwest") == {rid}
    assert theirs(store, "person:slack:U5") == {"slack:C1:9.0"}


def test_a_told_name_two_people_share_waits(store: index.Index, tmp_path: Path) -> None:
    item(store, 6, "briankwest")
    two = [
        {"id": "U5", "profile": {"real_name": "Brian West"}},
        {"id": "U6", "profile": {"real_name": "Brian Lee"}},
    ]
    for n, u in enumerate(two):
        slack.store_docs(store, slack.documents("C1", "#a", [{"ts": f"9.{n}", "user": u["id"], "text": "x"}],
                                                lambda x: str(x)))  # fmt: skip
    store.put_people(people.build({}, two), complete=False)
    svc = service(tmp_path, store)
    memory.answer("Brian's GitHub is briankwest", svc.memory)
    assert store._owners(["github:briankwest"]) == ["person:github:briankwest"]  # which Brian? none yet


# --- the user ---------------------------------------------------------------------------------------


def test_the_user_is_never_joined(store: index.Index, state: Path, tmp_path: Path) -> None:
    item(store, 7, "melogin")  # the user's own item links nobody
    store.put("mail:3", "mail", "Hi", "x", "Me <me@corp.example>", NOW)
    assert store.count("person") == 0
    github_people.run_pass(store, FakeGitHub({FIRST: fixture("commits_first.json")}), state, tracked(state),
                           now=NOW)  # fmt: skip
    # A contact card holding the user's address isn't made a person either.
    store.put_people([{"contact": "c0", "name": "Michael Jerris", "emails": ["me@corp.example"]}])
    store.put_people(
        [{"contact": "", "github": ["melogin"], "emails": ["mike@home.example"]}], complete=False
    )
    assert store.count("person") == 0
    svc = service(tmp_path, store)
    memory.answer("Michael Jerris's GitHub is melogin", svc.memory)
    assert store.count("person") == 0


def test_the_pass_logs_counts_only(store: index.Index, state: Path, caplog: pytest.LogCaptureFixture) -> None:
    item(store, 8, "octo")
    with caplog.at_level("INFO", logger="scout.github_people"):
        github_people.run_pass(store, FakeGitHub({FIRST: fixture("commits_first.json")}), state, tracked(state),
                               now=NOW)  # fmt: skip
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("people: github ") for m in said)
    assert not any("@" in m or "octo" in m for m in said)


def test_a_told_login_is_a_fact_told_in_its_turn() -> None:
    from scout import links

    assert links.says_remember("Brian's GitHub is briankwest", "")
    assert not links.says_remember("what's Brian's GitHub", "")


def test_claudes_context_asks_about_a_same_name_person_the_request_names(
    store: index.Index, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_routing import make

    item(store, 9, "briank")
    slack.store_docs(store, slack.documents("C1", "#a", [{"ts": "9.0", "user": "U5", "text": "x"}],
                                            lambda u: "Brian West"))  # fmt: skip
    github_people.run_pass(store, FakeGitHub({"/users/briank": fixture("user_briank_noemail.json")}), state,
                           tracked(state), now=NOW)  # fmt: skip

    async def go(text: str) -> str:
        r: Any = make()
        r.a.cfg.contacts.enabled = True
        return str(await r.a._with_context(text))

    got = asyncio.run(go("what did Brian West say about the release"))
    assert "  - Is Brian West on GitHub the same as briank?" in got
    assert "Is Brian" not in asyncio.run(go("what's the weather"))


# --- 1b. the history before the first pass's pages ------------------------------------------------


class History:
    """A repo's commit list as GitHub serves it: newest first, `since` and `until` (both
    inclusive), pages of `per_page`. Commits 640 to 790 share one date (a rebase), more
    than a page. `limit_at`: the request (by count) answered with a rate limit."""

    def __init__(self, n: int, limit_at: int = 0, slip_in: bool = False) -> None:
        import datetime as dt

        base = dt.datetime(2026, 10, 10, 12, tzinfo=dt.UTC)
        self.commits: list[dict[str, Any]] = []
        for i in range(n):
            at = base - dt.timedelta(minutes=640 if 640 <= i <= 790 else i)
            login = f"dev{i % 2}"
            ident = {"name": f"Dev {i % 2}", "email": f"{login}@acme.example",
                     "date": at.strftime("%Y-%m-%dT%H:%M:%SZ")}  # fmt: skip
            self.commits.append({"sha": f"c{i}", "commit": {"author": ident, "committer": ident},
                                 "author": {"login": login, "type": "User"},
                                 "committer": {"login": login, "type": "User"}})  # fmt: skip
        self.asked: list[str] = []
        self.limit_at = limit_at
        self.slip_in = slip_in  # a commit at the shared date pushed while it's being read
        self.empty = 0  # pages asked for that had nothing

    def __call__(self, path: str, etag: str = "") -> github.Response:
        from urllib.parse import parse_qs, urlsplit

        self.asked.append(path)
        if self.limit_at and len(self.asked) == self.limit_at:
            raise github.RateLimited(NOW + 1800)
        if not path.startswith("/repos/o/api/commits"):
            return github.Response(404, None, "")
        q = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        cs = self.commits
        if "since" in q:
            cs = [
                c
                for c in cs
                if github_people._ts(c["commit"]["committer"]["date"]) >= github_people._ts(q["since"])
            ]
        if "until" in q:
            cs = [
                c
                for c in cs
                if github_people._ts(c["commit"]["committer"]["date"]) <= github_people._ts(q["until"])
            ]
        per, page = int(q["per_page"]), int(q.get("page", 1))
        if self.slip_in and "until" in q and page == 2:
            self.slip_in = False
            late = json.loads(json.dumps(self.commits[640]))
            late["sha"] = "late"
            self.commits.insert(640, late)
            return self(path, etag)
        got = cs[(page - 1) * per : page * per]
        self.empty += not got
        return github.Response(200, got, "")


def counted(state: Path) -> int:
    st = json.loads(state.read_text())
    return sum(int(n) for logins in st["emails"].values() for n, _ in logins.values())


def test_older_history_is_read_a_page_at_a_time_after_the_first_pass(
    store: index.Index, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_people, "MAX_REQUESTS", 8)
    gh, fake = tracked(state), History(1250)
    got = github_people.run_pass(store, fake, state, gh, now=NOW)
    # The newest five pages, the two logins' profiles, then one page further back (from
    # the oldest commit's date: the commit at it isn't counted again).
    assert got["commits"] == 599 and len(fake.asked) == 8
    assert "&until=" in fake.asked[-1] and counted(state) == 599
    t = NOW
    while "done" not in json.loads(state.read_text())["repos"]["o/api"]["old"]:
        t += 7 * 3600
        github_people.run_pass(store, fake, state, gh, now=t)
    # Every commit counted once: the ones at a shared date across pages too. The last
    # page was short, which ended it (no page asked for past the first commit).
    assert counted(state) == 1250 and fake.empty == 0
    # Done: later passes ask only for what's new.
    fake.asked.clear()
    github_people.run_pass(store, fake, state, gh, now=t + 7 * 3600)
    assert all("until=" not in p for p in fake.asked) and counted(state) == 1250


def test_a_rate_limit_keeps_the_place_in_the_older_history(
    store: index.Index, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_people, "MAX_REQUESTS", 8)
    gh = tracked(state)
    github_people.run_pass(store, History(1250), state, gh, now=NOW)
    fake = History(1250, limit_at=4)  # what's new, two older pages, then the limit
    github_people.run_pass(store, fake, state, gh, now=NOW + 7 * 3600)
    st = json.loads(state.read_text())
    assert st["limited_until"] == NOW + 1800 and 599 < counted(state) < 1250
    assert "until" in st["repos"]["o/api"]["old"]  # the place after the second page
    assert github_people.run_pass(store, fake, state, gh, now=NOW + 7.1 * 3600) == {}  # waits for the reset
    t = NOW + 7 * 3600
    while "done" not in json.loads(state.read_text())["repos"]["o/api"]["old"]:
        t += 7 * 3600
        github_people.run_pass(store, History(1250), state, gh, now=t)
    assert counted(state) == 1250


def test_counts_from_before_the_history_was_kept_are_read_again_once(store: index.Index, state: Path) -> None:
    gh = tracked(state)
    # A pass from before the place in the older history was kept: 500 counted, no place.
    old = History(1250)
    st: dict[str, Any] = {}
    github_people.tally(st, old.commits[:500], CTX)
    st["repos"] = {"o/api": {"since": old.commits[0]["commit"]["committer"]["date"], "edge": ["c0"]}}
    st["users"] = {"dev0": {"name": "", "email": "", "etag": "", "checked": NOW}}
    st["at"] = NOW - 7 * 3600
    state.write_text(json.dumps(st))
    t = NOW
    github_people.run_pass(store, old, state, gh, now=t)
    while "done" not in json.loads(state.read_text())["repos"]["o/api"]["old"]:
        t += 7 * 3600
        github_people.run_pass(store, old, state, gh, now=t)
    st = json.loads(state.read_text())
    assert counted(state) == 1250 and st["users"]["dev0"]["checked"] == NOW  # profiles kept


def test_a_commit_pushed_into_a_shared_date_while_it_is_read_is_not_counted_twice(
    store: index.Index, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_people, "MAX_REQUESTS", 8)
    gh, fake = tracked(state), History(1250, slip_in=True)
    t = NOW
    github_people.run_pass(store, fake, state, gh, now=t)
    while "done" not in json.loads(state.read_text())["repos"]["o/api"]["old"]:
        t += 7 * 3600
        github_people.run_pass(store, fake, state, gh, now=t)
    assert not fake.slip_in  # it was pushed: the next page starts one commit earlier
    # The first page's last commit, seen again, isn't counted again. (The pushed one lands
    # among the commits already read, so it isn't read: an old-dated push is the one gap.)
    assert counted(state) == 1250
