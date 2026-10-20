"""A GitHub login joined to the rest of the person it is (docs/contacts.md, People): by the
email addresses their commits carry and the one their public profile shows. Run by
scout-memory's GitHub sync (github.loop), every PASS_EVERY_S; read-only, GET only.

Commit emails: for each repo the sync tracks (state/github.json's repos), the commit list
(`/repos/{o}/{r}/commits`), newest first, MAX_PAGES pages of 100 the first time, then only
what came since the newest commit seen (its date, `since`; the commits at that date are
remembered so none is counted twice). The history before the first pass's pages is read
back a page at a time with what's left of a pass's requests (`until` the oldest date seen,
the commits at that date remembered; a page all at one date moves on by page number), its
place kept page by page, so a rate limit or a restart resumes there and nothing is counted
twice; `old.done` once a short page reaches the first commit. Each commit gives (author login, commit author email)
and (committer login, committer email). Skipped: noreply addresses
(`*@users.noreply.github.com`, `noreply@github.com`), bots (`[bot]`, type Bot, web-flow,
github-actions...), automation addresses (ci@, build@, deploy@..., and people.automated's),
commits with no GitHub user, and the user (their logins and addresses; a login that commits
with one of the user's addresses is taken for the user's too). An address is mapped to a
login only when it was seen with that one login, and then only when it's that login's most
used address or seen on MIN_COMMITS commits, so one stray commit doesn't give a login
someone else's address.

The profile: `/users/{login}` for the logins the store has people for and the ones commits
showed, PROFILE_TTL_S between reads (with its ETag). Its email, when shown, is a key like a
commit's; its name is not (a name never makes or joins a person). The names (the profile's,
else the commit author name a login used most) are kept in the store for its same-name
suggestions (Index.people_suggestions).

What it finds goes to Index.put_people (never creating anyone, never the user), which merges
the records those identities join into one. State: state/github_people.json (addresses and
counts; never logged). The log line is counts only."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import github, people
from .config import DATA

log = logging.getLogger(__name__)

STATE = DATA / "state" / "github_people.json"
PASS_EVERY_S = 6 * 3600.0
PROFILE_TTL_S = 7 * 86400.0
MAX_REQUESTS = 60  # a pass, commits and profiles together (304s included)
MAX_PAGES = 5  # of commits, per repo per pass
PER_PAGE = 100
MIN_COMMITS = 2  # an address that isn't a login's most used needs this many commits
# 2: each repo keeps its place in the older history ("old"). The counts from before were
# read without one, so they're read again once (not added to: that would count twice).
VERSION = 2

_NOREPLY = re.compile(r"(?:@users\.noreply\.github\.com|^noreply@github\.com|@noreply\.github\.com)$", re.I)
_AUTO_LOGIN = re.compile(
    r"\[bot\]$|-bot$|^bot-|^(?:web-flow|github-actions|actions-user|ghost|ci|jenkins\S*|renovate\S*|"
    r"dependabot\S*|semantic-release-bot|copilot)$",
    re.I,
)
_AUTO_EMAIL = re.compile(
    r"^(?:ci|build(?:s|er)?|deploy(?:s|er)?|jenkins|bots?|automation|releases?|releaser|robot|git|actions|"
    r"travis|circleci|buildkite)@",
    re.I,
)


def usable_email(e: str) -> bool:
    """A person's address: not a noreply, a machine's or an automation's."""
    return (
        bool(e)
        and "@" in e
        and not _NOREPLY.search(e)
        and not _AUTO_EMAIL.match(e)
        and not people.automated(e)
    )


def _login(user: Any) -> str:
    """A commit's GitHub user, unless it's a bot or automation."""
    if not isinstance(user, dict) or str(user.get("type") or "User") == "Bot":
        return ""
    login = str(user.get("login") or "").lower()
    return "" if not login or _AUTO_LOGIN.search(login) else login


def _ts(iso: Any) -> float:
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def tally(st: dict[str, Any], commits: list[Any], ctx: people.Ctx) -> int:
    """Count each commit's (login, address) pairs into the state: st["emails"][address][login]
    = [commits, newest commit time], and the author names each login used. Returns the
    commits counted."""
    emails: dict[str, dict[str, list[float]]] = st.setdefault("emails", {})
    names: dict[str, dict[str, int]] = st.setdefault("names", {})
    mine: list[str] = st.setdefault("me_logins", [])
    n = 0
    for c in commits:
        if not isinstance(c, dict) or not isinstance(c.get("commit"), dict):
            continue
        n += 1
        pairs: dict[tuple[str, str], str] = {}
        for side in ("author", "committer"):
            ident = c["commit"].get(side) or {}
            login, email = _login(c.get(side)), people.norm_email(str(ident.get("email") or ""))
            if not login or not usable_email(email) or ctx.is_me(f"github:{login}"):
                continue
            if ctx.is_me(f"email:{email}"):  # the user's address: that login is the user's
                if login not in mine:
                    mine.append(login)
                continue
            pairs.setdefault((login, email), str(ident.get("name") or ""))
        when = _ts((c["commit"].get("author") or {}).get("date"))
        for (login, email), name in pairs.items():
            rec = emails.setdefault(email, {}).setdefault(login, [0, 0.0])
            rec[0], rec[1] = int(rec[0]) + 1, max(float(rec[1]), when)
            name = " ".join(name.split())
            if len(name.split()) > 1:
                names.setdefault(login, {})[name] = names.get(login, {}).get(name, 0) + 1
    return n


def email_map(st: dict[str, Any], ctx: people.Ctx) -> dict[str, list[str]]:
    """{login: its addresses} from the counts: an address seen with exactly one login,
    that login's most used (by commits, then the newest) or seen on MIN_COMMITS commits."""
    mine = set(st.get("me_logins") or [])
    by: dict[str, list[tuple[int, float, str]]] = {}
    for email, logins in (st.get("emails") or {}).items():
        if len(logins) != 1:
            continue  # two logins used it: whose is it?
        login, (n, last) = next(iter(logins.items()))
        if login in mine or ctx.is_me(f"github:{login}") or ctx.is_me(f"email:{email}"):
            continue
        by.setdefault(login, []).append((int(n), float(last), email))
    out: dict[str, list[str]] = {}
    for login, es in by.items():
        es.sort(reverse=True)
        out[login] = [e for i, (n, _, e) in enumerate(es) if i == 0 or n >= MIN_COMMITS]
    return dict(sorted(out.items()))


def gh_names(st: dict[str, Any]) -> dict[str, str]:
    """{login: the full name they go by on GitHub}: the profile's, else the commit author
    name they used most."""
    out: dict[str, str] = {}
    for login, ns in (st.get("names") or {}).items():
        if ns:
            out[login] = max(ns.items(), key=lambda kv: kv[1])[0]
    for login, u in (st.get("users") or {}).items():
        name = " ".join(str((u or {}).get("name") or "").split())
        if len(name.split()) > 1:
            out[login] = name
    return dict(sorted(out.items()))


def entries(st: dict[str, Any], ctx: people.Ctx) -> list[dict[str, Any]]:
    """What the pass knows, as Index.put_people's people: each login with its addresses."""
    found = email_map(st, ctx)
    mine = set(st.get("me_logins") or [])
    for login, u in (st.get("users") or {}).items():
        e = people.norm_email(str((u or {}).get("email") or ""))
        if not usable_email(e) or login in mine or ctx.is_me(f"github:{login}"):
            continue
        if ctx.is_me(f"email:{e}"):
            found.pop(login, None)  # the user's own account under another login
            mine.add(login)
            continue
        if e not in found.setdefault(login, []):
            found[login].append(e)
    return [{"contact": "", "github": [login], "emails": es} for login, es in sorted(found.items())
            if es and login not in mine]  # fmt: skip


def _commits(get: github.Fetch, repo: str, rs: dict[str, Any], budget: int) -> tuple[list[Any], int]:
    """The repo's commits since the newest seen (all of the first MAX_PAGES pages the
    first time), and the requests it took. The first time, it also sets where the older
    history starts (rs["old"]), or that there's none."""
    first = not rs.get("since")
    path = f"/repos/{repo}/commits?per_page={PER_PAGE}" + (f"&since={rs['since']}" if rs.get("since") else "")
    out: list[Any] = []
    asked = 0
    ended = False
    for page in range(1, MAX_PAGES + 1):
        if asked >= budget:
            break
        r = get(path + (f"&page={page}" if page > 1 else ""), "")
        asked += 1
        if r.status != 200 or not isinstance(r.data, list):
            break
        out += r.data
        if len(r.data) < PER_PAGE:
            ended = True
            break
    if first and out:
        oldest = _oldest(out)
        rs["old"] = (
            {"done": True} if ended or oldest is None else {"until": oldest[0], "edge": oldest[1], "page": 1}
        )
    return out, asked


def _dated(commits: list[Any]) -> list[tuple[str, str]]:
    """(committer date, sha) of each commit that has a date."""
    out = [(str(((c.get("commit") or {}).get("committer") or {}).get("date") or ""), str(c.get("sha") or ""))
           for c in commits if isinstance(c, dict)]  # fmt: skip
    return [(d, sha) for d, sha in out if d]


def _oldest(commits: list[Any]) -> tuple[str, list[str]] | None:
    """The oldest committer date in these commits, with the commits at it."""
    dated = _dated(commits)
    if not dated:
        return None
    at = min(dated, key=lambda x: _ts(x[0]))[0]
    return at, [sha for d, sha in dated if _ts(d) == _ts(at)]


def _older(get: github.Fetch, repo: str, rs: dict[str, Any], st: dict[str, Any], ctx: people.Ctx) -> int:
    """One page of the repo's history before what's been read (rs["old"]): counted, and the
    place moved past it. Returns the commits counted."""
    old = rs["old"]
    page = int(old.get("page") or 1)
    r = get(
        f"/repos/{repo}/commits?per_page={PER_PAGE}&until={old['until']}"
        + (f"&page={page}" if page > 1 else ""),
        "",
    )
    if r.status != 200 or not isinstance(r.data, list):
        rs["old"] = {"done": True}  # gone, or no longer readable
        return 0
    edge = set(old.get("edge") or [])
    n = tally(st, [c for c in r.data if isinstance(c, dict) and c.get("sha") not in edge], ctx)
    oldest = _oldest(r.data)
    if len(r.data) < PER_PAGE or oldest is None:
        rs["old"] = {"done": True}
    elif _ts(oldest[0]) < _ts(old["until"]):
        rs["old"] = {"until": oldest[0], "edge": oldest[1], "page": 1}
    else:  # a whole page at the one date: the next page of it
        rs["old"] = {
            "until": old["until"],
            "edge": list(dict.fromkeys([*edge, *oldest[1]])),
            "page": page + 1,
        }
    return n


def _advance(rs: dict[str, Any], commits: list[Any]) -> None:
    """The newest commit date seen (the next `since`), with the commits at that date."""
    dated = _dated(commits)
    if not dated:
        return
    newest = max(dated, key=lambda x: _ts(x[0]))[0]
    if rs.get("since") and _ts(rs["since"]) > _ts(newest):
        return
    edge = [s for d, s in dated if _ts(d) == _ts(newest)]
    if rs.get("since") and _ts(rs["since"]) == _ts(newest):
        edge = list(dict.fromkeys([*(rs.get("edge") or []), *edge]))
    rs.update(since=newest, edge=edge)


def _profiles(get: github.Fetch, logins: list[str], st: dict[str, Any], now: float, budget: int) -> int:
    """Each login's public profile (name, email), read again after PROFILE_TTL_S."""
    users: dict[str, Any] = st.setdefault("users", {})
    due = sorted((float((users.get(lg) or {}).get("checked") or 0), lg) for lg in logins
                 if now - float((users.get(lg) or {}).get("checked") or 0) >= PROFILE_TTL_S)  # fmt: skip
    asked = 0
    for _, login in due[: max(0, budget)]:
        old = users.get(login) or {}
        r = get(f"/users/{login}", str(old.get("etag") or ""))
        asked += 1
        if r.status == 304:
            users[login] = {**old, "checked": now}
        elif r.status == 200 and isinstance(r.data, dict):
            bot = str(r.data.get("type") or "User") == "Bot"
            users[login] = {"name": "" if bot else str(r.data.get("name") or ""),
                            "email": "" if bot else str(r.data.get("email") or ""), "etag": r.etag, "checked": now}  # fmt: skip
        else:
            users[login] = {"name": "", "email": "", "etag": "", "checked": now, "gone": True}
    return asked


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(st: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(path)


def run_pass(
    ix: Any,
    get: github.Fetch,
    state: Path = STATE,
    github_state: Path = github.STATE,
    now: float | None = None,
    force: bool = False,
) -> dict[str, int]:
    """One pass when it's due (PASS_EVERY_S since the last; `force`: now): new commits and
    due profiles read, what they show handed to the store. Counts ({} when not due)."""
    now = time.time() if now is None else now
    st = load(state)
    if not force and now - float(st.get("at") or 0) < PASS_EVERY_S:
        return {}
    if now < float(st.get("limited_until") or 0):
        return {}
    if st.get("v") != VERSION:
        for k in ("emails", "names", "repos"):
            st.pop(k, None)
        st["v"] = VERSION
    ctx: people.Ctx = ix.people_ctx()
    repos = sorted(
        r
        for r, v in (load(github_state).get("repos") or {}).items()
        if isinstance(v, dict) and "private" in v
    )
    asked = counted = 0
    try:
        for repo in repos:
            if asked >= MAX_REQUESTS:
                break
            rs = st.setdefault("repos", {}).setdefault(repo, {})
            commits, n = _commits(get, repo, rs, MAX_REQUESTS - asked)
            asked += n
            edge = set(rs.get("edge") or [])
            counted += tally(
                st, [c for c in commits if isinstance(c, dict) and c.get("sha") not in edge], ctx
            )
            _advance(rs, commits)
        seen = set(ix.people_logins()) | {lg for ls in (st.get("emails") or {}).values() for lg in ls}
        seen = {
            lg for lg in seen if not ctx.is_me(f"github:{lg}") and lg not in set(st.get("me_logins") or [])
        }
        asked += _profiles(get, sorted(seen), st, now, MAX_REQUESTS - asked)
        # What's left goes to the older history, a page of each repo in turn.
        while asked < MAX_REQUESTS:
            behind = [r for r in repos if "until" in (st["repos"].get(r) or {}).get("old", {})]
            if not behind:
                break
            for repo in behind[: MAX_REQUESTS - asked]:
                counted += _older(get, repo, st["repos"][repo], st, ctx)
                asked += 1
    except github.RateLimited as exc:
        st["limited_until"] = exc.reset
        log.info(
            "people: github rate limited until %s", dt.datetime.fromtimestamp(exc.reset).strftime("%H:%M")
        )
    except github.GitHubError as exc:
        log.info("people: github pass stopped (%s)", exc)
    st["at"] = now
    _save(st, state)
    found = entries(st, ctx)
    ix.put_github_names(gh_names(st))
    got = ix.put_people(found, complete=False)
    logins = {lg for ls in (st.get("emails") or {}).values() for lg in ls} | set(st.get("users") or {})
    out = {"commits": counted, "logins": len(logins), "mapped": len(found), "merged": int(got.get("merged", 0)),
           "suggestions": len(ix.people_suggestions()), "requests": asked}  # fmt: skip
    log.info("people: github %d commits read, %d logins seen, %d mapped to an email, %d merged, %d suggestions "
             "(%d requests)", out["commits"], out["logins"], out["mapped"], out["merged"], out["suggestions"],
             asked)  # fmt: skip
    return out
