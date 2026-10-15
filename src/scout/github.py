"""GitHub issues, pull requests and commits as records of Scout's store (docs/links.md,
"GitHub"), so "what's the status of PR 117" is answered here and a task about it opens it.

What becomes a record: the items the cross-reference pass (xref.py, index.link_xrefs)
found named in Slack, mail, meetings, notes and tasks; each is a link target
`github:owner/repo#123` (or `github:owner/repo@<sha>`) until this sync fetches it. Each
record is kind `github`: its title, state (open, closed, merged; a commit is
"committed"), author and assignees, updated time and html_url (its link out), with the
first part of its description to search.

GitHub's REST API, read-only (GET only): /repos/{o}/{r}/issues/{n} answers for an issue
and a pull request alike (a PR's merged_at is in its pull_request part), /commits/{sha}
for a commit, /repos/{o}/{r} once a day per repo for whether it's private, /user once
for who the user is. Rate-friendly: every refresh sends the item's ETag (If-None-Match),
and a 304 costs nothing against the limit; open items are checked every
`open_every_min`, closed ones every `closed_every_days`, commits never again; at most
MAX_PER_PASS requests a pass; a 403/429 for the rate limit stops the pass until the
reset GitHub gives.

Auth: `[github] token`, else the token the Mac's `gh` CLI is logged in with (`gh auth
token`, read once when the sync starts and again after a 401). scout-memory runs under
launchd with a plain PATH, so `gh` is looked for where Homebrew puts it as well. The token
goes only to api.github.com and is never logged.

Privacy: a record from a private repo is private (meta `private`), like mail: its title
and text stay on the Mac outside open privacy mode. Public ones can be shown.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from . import index, syncstate, xref
from .config import DATA
from .mac import ToolError

log = logging.getLogger(__name__)

API = "https://api.github.com"
KIND = "github"
STATE = DATA / "state" / "github.json"
TIMEOUT_S = 20.0
MAX_PER_PASS = 40  # requests a pass (304s included)
BODY_CHARS = 1500  # of an issue's or PR's description, kept to search
REPO_EVERY_S = 86400.0  # whether a repo is private, asked again this often
MISSING_AGAIN_S = 86400.0  # an item GitHub didn't give (gone, no access), tried again after this
GH_PATHS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    def __init__(self, reset: float) -> None:
        super().__init__("rate limited")
        self.reset = reset


class Response(NamedTuple):
    status: int  # 200, 304 (unchanged), 404...
    data: Any
    etag: str


Fetch = Callable[[str, str], Response]  # (path, etag) -> response


def token(configured: str = "") -> str:
    """The token to use: the configured one, else `gh auth token` ("" when neither)."""
    if configured.strip():
        return configured.strip()
    exe = shutil.which("gh") or next((p for p in GH_PATHS if Path(p).exists()), None)
    if not exe:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 (a fixed program with fixed arguments)
            [exe, "auth", "token"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("github: gh not usable (%s)", type(exc).__name__)
        return ""
    if out.returncode != 0:
        log.info("github: gh has no token (exit %d)", out.returncode)
        return ""
    return out.stdout.strip()


def fetcher(tok: str | Callable[[], str], base: str = API) -> Fetch:
    """GETs to GitHub's REST API with the token (re-read once after a 401)."""
    get_token: Callable[[], str] = tok if callable(tok) else (lambda: tok)
    cur = [get_token()]

    def get(path: str, etag: str = "") -> Response:
        for attempt in range(2):
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "Scout",
                       "X-GitHub-Api-Version": "2022-11-28"}  # fmt: skip
            if cur[0]:
                headers["Authorization"] = f"Bearer {cur[0]}"
            if etag:
                headers["If-None-Match"] = etag
            req = urllib.request.Request(f"{base}{path}", headers=headers)  # noqa: S310 (a fixed https API)
            try:
                with _opener.open(req, timeout=TIMEOUT_S) as r:
                    return Response(
                        r.status, json.loads(r.read() or b"null"), str(r.headers.get("ETag") or "")
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return Response(304, None, str(exc.headers.get("ETag") or etag))
                if exc.code == 401 and attempt == 0:
                    cur[0] = get_token()
                    continue
                if exc.code in (403, 429) and (
                    exc.headers.get("X-RateLimit-Remaining") == "0" or exc.headers.get("Retry-After")
                ):
                    reset = float(exc.headers.get("X-RateLimit-Reset") or 0) or (
                        time.time() + float(exc.headers.get("Retry-After") or 60)
                    )
                    raise RateLimited(reset) from None
                if exc.code in (404, 410, 451, 403):
                    return Response(exc.code, None, "")
                raise GitHubError(f"HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                raise GitHubError(type(exc).__name__) from None
        raise GitHubError("HTTP 401")

    return get


def _ts(iso: Any) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def _login(u: Any) -> str:
    return str((u or {}).get("login") or "") if isinstance(u, dict) else ""


def item_doc(
    ref: xref.GitHubRef, data: dict[str, Any], private: bool, etag: str, me: str = ""
) -> dict[str, Any]:
    """An issue or PR (the issues endpoint's answer) as a store item."""
    pr = data.get("pull_request") if isinstance(data.get("pull_request"), dict) else None
    kind = "pr" if pr is not None else "issue"
    merged_at = (pr or {}).get("merged_at")
    state = "merged" if merged_at else str(data.get("state") or "open")
    author = _login(data.get("user"))
    assignees = [a for a in (_login(x) for x in data.get("assignees") or []) if a]
    title = " ".join(str(data.get("title") or "").split())[:300]
    word = "Pull request" if kind == "pr" else "Issue"
    repo = ref.repo
    head = f"{word} {ref.number} in {repo}: {title}\nState: {state}."
    head += f" Author: {author}." if author else ""
    head += f" Assigned: {', '.join(assignees)}." if assignees else ""
    body = (head + "\n\n" + str(data.get("body") or "")[:BODY_CHARS]).strip()
    updated = _ts(data.get("updated_at")) or time.time()
    return {
        "id": ref.id, "title": title, "body": body, "who": ", ".join(dict.fromkeys(p for p in [author, *assignees] if p)),
        "ts": updated,
        "meta": {
            "repo": repo, "number": ref.number, "type": kind, "state": state, "author": author,
            "assignees": assignees, "url": str(data.get("html_url") or ref.url),
            "updated_at": data.get("updated_at"), "closed_at": data.get("closed_at"), "merged_at": merged_at,
            "draft": bool(data.get("draft")), "private": private, "etag": etag, "checked": time.time(),
            "by_me": bool(me) and author.lower() == me.lower(),
            "mine": bool(me) and me.lower() in (a.lower() for a in assignees),
        },
    }  # fmt: skip


def commit_doc(
    ref: xref.GitHubRef, data: dict[str, Any], private: bool, etag: str, me: str = ""
) -> dict[str, Any]:
    c: dict[str, Any] = data["commit"] if isinstance(data.get("commit"), dict) else {}
    message = str(c.get("message") or "")
    title = " ".join(message.split("\n", 1)[0].split())[:300]
    author = _login(data.get("author")) or str((c.get("author") or {}).get("name") or "")
    when = _ts((c.get("committer") or c.get("author") or {}).get("date")) or time.time()
    sha = str(data.get("sha") or ref.sha or "")
    body = f"Commit {sha[:12]} in {ref.repo}: {title}\nAuthor: {author}.\n\n{message[:BODY_CHARS]}".strip()
    return {
        "id": ref.id, "title": title, "body": body, "who": author, "ts": when,
        "meta": {
            "repo": ref.repo, "sha": sha, "type": "commit", "state": "committed", "author": author,
            "assignees": [], "url": str(data.get("html_url") or ref.url), "private": private, "etag": etag,
            "checked": time.time(), "by_me": bool(me) and author.lower() == me.lower(),
        },
    }  # fmt: skip


def load_state(path: Path = STATE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def due(ix: index.Index, cfg: Any, state: dict[str, Any], now: float) -> list[str]:
    """What to fetch this pass, in order: items named but not stored yet (newest mention
    first; one GitHub didn't give waits a day), then stored ones due a refresh (open
    every open_every_min, closed or merged every closed_every_days, commits never),
    least recently checked first. Only repos the allow-list lets through."""
    allow = xref.Allow(cfg.orgs, cfg.repos)
    missing: dict[str, float] = state.get("missing") or {}
    new = []
    for id in ix.link_targets("github:"):
        ref = xref.parse_id(id)
        if ref is None or not allow(ref.repo) or ix.get(id) is not None:
            continue
        if now - float(missing.get(id) or 0) < MISSING_AGAIN_S:
            continue
        new.append(id)
    old = []
    for h in ix.recent([KIND], limit=5000):
        st = str(h.meta.get("state") or "")
        if st == "committed":
            continue
        every = cfg.open_every_min * 60 if st == "open" else cfg.closed_every_days * 86400
        checked = float(h.meta.get("checked") or 0)
        if now - checked >= every:
            old.append((checked, h.id))
    return new + [id for _, id in sorted(old)]


def _private(get: Fetch, repo: str, state: dict[str, Any], now: float, asked: list[int]) -> bool | None:
    """Whether a repo is private (asked once a day); None when GitHub won't say."""
    repos: dict[str, Any] = state.setdefault("repos", {})
    known = repos.get(repo)
    if isinstance(known, dict) and now - float(known.get("checked") or 0) < REPO_EVERY_S:
        return bool(known.get("private"))
    asked[0] += 1
    r = get(f"/repos/{repo}", "")
    if r.status != 200 or not isinstance(r.data, dict):
        return None if not isinstance(known, dict) else bool(known.get("private"))
    repos[repo] = {"private": bool(r.data.get("private")), "checked": now}
    return bool(r.data.get("private"))


def sync_pass(
    ix: index.Index, get: Fetch, cfg: Any, state_path: Path = STATE, now: float | None = None
) -> tuple[int, int]:
    """Fetch what's due (due()), at most MAX_PER_PASS requests. Returns (asked, changed)."""
    now = time.time() if now is None else now
    state = load_state(state_path)
    asked, changed = [0], 0
    try:
        if "login" not in state:
            r = get("/user", "")
            asked[0] += 1
            state["login"] = _login(r.data) if r.status == 200 else ""
        me = str(state.get("login") or "")
        for id in due(ix, cfg, state, now):
            if asked[0] >= MAX_PER_PASS:
                break
            ref = xref.parse_id(id)
            if ref is None:
                continue
            private = _private(get, ref.repo, state, now, asked)
            if private is None:  # no such repo, or no access: try again tomorrow
                state.setdefault("missing", {})[id] = now
                continue
            old = ix.get(id)
            etag = str(old.meta.get("etag") or "") if old is not None else ""
            path = (
                f"/repos/{ref.repo}/commits/{ref.sha}"
                if ref.sha
                else f"/repos/{ref.repo}/issues/{ref.number}"
            )
            r = get(path, etag)
            asked[0] += 1
            if r.status == 304 and old is not None:
                ix.put(id, KIND, old.title, old.body, old.who, old.ts,
                       {**old.meta, "checked": now, "private": private})  # fmt: skip
                continue
            if r.status != 200 or not isinstance(r.data, dict):
                state.setdefault("missing", {})[id] = now
                if old is not None:  # gone from GitHub: checked, kept as it was
                    ix.put(id, KIND, old.title, old.body, old.who, old.ts, {**old.meta, "checked": now})
                continue
            make = commit_doc if ref.sha else item_doc
            doc = make(ref, r.data, private, r.etag, me)
            doc["meta"]["checked"] = now
            changed += ix.put(kind=KIND, **doc)
            state.get("missing", {}).pop(id, None)
    except RateLimited as exc:
        state["limited_until"] = exc.reset
        log.info("github: rate limited until %s", dt.datetime.fromtimestamp(exc.reset).strftime("%H:%M"))
    finally:
        save_state(state, state_path)
    return asked[0], changed


async def loop(ix: index.Index, cfg: Any, state: Path = STATE) -> None:
    """Every cfg.every_min minutes, for scout-memory's life: fetch new GitHub items and
    refresh the ones due. No token: nothing is fetched (said once in the log)."""
    tok = await asyncio.to_thread(token, cfg.token)
    if not tok:
        log.warning("github: no token ([github] token, or gh auth login); GitHub items aren't fetched")
        return
    get = fetcher(lambda: tok or token(cfg.token))
    run = asyncio.get_running_loop()
    while True:
        try:
            until = float(load_state(state).get("limited_until") or 0)
            if time.time() >= until:
                with syncstate.track("github", max(1.0, cfg.every_min) * 60) as p:
                    asked, changed = await run.run_in_executor(None, sync_pass, ix, get, cfg, state)
                    p.updated = changed
                    p.backlog = len(due(ix, cfg, load_state(state), time.time()))
                if changed:
                    log.info("github: %d items changed (%d requests)", changed, asked)
        except GitHubError as exc:
            log.warning("github: sync failed: %s", exc)
        except Exception:
            log.exception("github: sync failed")
        await asyncio.sleep(max(1.0, cfg.every_min) * 60)


# --- saying it (tier 0, the pulse, the github_items tool) ---------------------------------

DONE_STATES = ("closed", "merged")


def _day(ts: float | None) -> str:
    if not ts:
        return ""
    d = dt.datetime.fromtimestamp(ts)
    return f"{d:%B} {d.day}"


def name(h: index.Hit, repo: bool = True) -> str:
    """ "PR 117 in api", "issue 9 in lib", "commit abc1234 in lib" (spoken)."""
    m = h.meta
    where = f" in {str(m.get('repo') or '').split('/')[-1]}" if repo and m.get("repo") else ""
    if m.get("type") == "commit":
        return f"commit {str(m.get('sha') or '')[:7]}{where}"
    return f"{'PR' if m.get('type') == 'pr' else 'issue'} {m.get('number')}{where}"


def state_words(h: index.Hit) -> str:
    """ "was merged on October 14", "is open, by Luca, assigned to Sam, updated October 13"."""
    m = h.meta
    state = str(m.get("state") or "")
    if state == "merged":
        return "was merged" + (f" on {_day(_ts(m.get('merged_at')))}" if m.get("merged_at") else "")
    if state == "closed":
        how = "closed without merging" if m.get("type") == "pr" else "closed"
        return f"was {how}" + (f" on {_day(_ts(m.get('closed_at')))}" if m.get("closed_at") else "")
    if state == "committed":
        return f"is a commit by {m.get('author') or 'someone'}, from {_day(h.ts)}"
    parts = ["is open" + (" as a draft" if m.get("draft") else "")]
    if m.get("author"):
        parts.append("by you" if m.get("by_me") else f"by {m['author']}")
    who = [str(a) for a in m.get("assignees") or []]
    if who:
        parts.append(
            "assigned to " + ", ".join("you" if m.get("mine") and i == 0 else a for i, a in enumerate(who))
        )
    parts.append(f"updated {_day(h.ts)}")
    return ", ".join(parts)


def say_status(h: index.Hit) -> str:
    """ "PR 117 in api, Fix the jitter buffer, was merged on October 14." """
    title = " ".join(h.title.split()).rstrip(".")
    return f"{name(h)}, {title}, {state_words(h)}." if title else f"{name(h)} {state_words(h)}."


def matching(hits: list[index.Hit], number: int, what: str = "") -> list[index.Hit]:
    """The records numbered `number` (of the kind said: "PR", "issue"), newest first."""
    want = (
        "pr" if what.lower().replace(" ", "") in ("pr", "pullrequest") else "issue" if what == "issue" else ""
    )
    out = [h for h in hits if h.meta.get("number") == number and (not want or h.meta.get("type") == want)]
    return sorted(out, key=lambda h: -h.ts)


async def items_text(a: dict[str, Any]) -> str:
    """The github_items tool: GitHub items Scout has seen named, newest first, filtered by
    `query` (a number, a repo, or words of the title), `state` (open, closed, merged,
    all) and `waiting` (only the ones a waiting-on task is about). In strict privacy mode
    a private repo's item is shown without its title."""
    from . import privacy, tasks

    store = index.default()
    hits: list[index.Hit] = await asyncio.to_thread(store.recent, [KIND], 5000)
    query = str(a.get("query") or "").strip().lower().lstrip("#")
    state = str(a.get("state") or "all").lower()
    if state not in ("all", "open", "closed", "merged", "committed"):
        raise ToolError("state must be open, closed, merged or all")
    if a.get("waiting"):
        waiting = {tasks.github_ref(t) for t in await asyncio.to_thread(tasks.all_tasks, store)
                   if t.status == "open" and not t.mine}  # fmt: skip
        hits = [h for h in hits if h.id in waiting]
    if state != "all":
        hits = [h for h in hits if h.meta.get("state") == state]
    if query:
        words = query.split()
        hits = [h for h in hits if query == str(h.meta.get("number")) or query in str(h.meta.get("repo") or "")
                or all(w in f"{h.title} {h.meta.get('repo')}".lower() for w in words)]  # fmt: skip
    limit = max(1, min(int(a.get("limit") or 10), 30))
    strict = privacy.current().mode == "strict"
    out = []
    for h in hits[:limit]:
        m = h.meta
        title = "(a private repo's item)" if strict and m.get("private") else " ".join(h.title.split())
        people = ", ".join(x for x in [f"author {m['author']}" if m.get("author") else "",
                                        "assignees " + ", ".join(m.get("assignees") or []) if m.get("assignees") else ""] if x)  # fmt: skip
        out.append(f"{name(h)} ({m.get('repo')}): {title}. {m.get('state')}, updated {_day(h.ts)}"
                   + (f"; {people}" if people else "") + f". {m.get('url')} (ref {h.id})")  # fmt: skip
    return (
        "\n".join(out)
        or "No GitHub items match (Scout knows only the ones named in Slack, mail, meetings, notes or tasks)."
    )
