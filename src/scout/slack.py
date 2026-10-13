"""Slack messages in Scout's local store, so "what did Sam say about the launch in Slack"
is searched on this Mac like mail (docs/slack.md).

Slack's Web API with a user token (xoxp-..., `[slack] token`): it reads what the user can
see. users.conversations lists the conversations the user is in (channels, private
channels, DMs and group DMs; archived ones left out), conversations.history gives each
one's messages since its own resume point, conversations.replies a thread's replies, and
users.info a sender's name (cached). Rate limits: conversations.history and .replies are
Tier 3 (about 50 a minute), so requests are spaced GAP_S apart, and a 429's Retry-After
is honored (at most MAX_WAIT_S).

Each message is stored as kind "slack" (id slack:<channel>:<ts>); a thread (root and
replies) is one item, chunked at about CHUNK_CHARS (slack:<channel>:<ts>#<n> after the
first part). Every pass re-reads the last LOOKBACK_S of each conversation, so an edited
message or a new reply to a recent thread is picked up; deleted messages stay until
they age out. Slack is the record; this is a searchable copy, and private like mail.

State (state/slack.json): per conversation, the newest message ts stored and when it
was last checked; the user names already looked up. A quiet conversation (nothing new
for QUIET_DAYS) is checked every QUIET_EVERY_S rather than every pass.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from . import index
from .config import DATA
from .granola import retry_after

log = logging.getLogger(__name__)

API = "https://slack.com/api"
KIND = "slack"
CHUNK_CHARS = 1000  # what embeds well (index.EMBED_CHARS)
PAGE = 200  # messages or conversations per page
GAP_S = 1.2  # between requests: Tier 3 allows about 50 a minute
TIMEOUT_S = 30.0
STATE = DATA / "state" / "slack.json"
LOOKBACK_S = 2 * 86400.0  # re-read this much of each conversation: edits, new thread replies
MAX_PAGES = 10  # history pages per conversation per pass (2,000 messages)
MAX_THREADS = 25  # threads (re)fetched per conversation per pass
MAX_REPLY_PAGES = 3  # reply pages per thread
QUIET_DAYS = 7.0  # a conversation with nothing newer than this is quiet...
QUIET_EVERY_S = 2 * 3600.0  # ...and checked only this often
# Messages that are only Slack noise: joins, leaves, topic and name changes.
SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "group_join", "group_leave", "channel_topic", "channel_purpose",
    "channel_name", "group_topic", "group_purpose", "group_name", "channel_archive", "channel_unarchive",
    "group_archive", "group_unarchive", "pinned_item", "unpinned_item", "bot_add", "bot_remove",
    "reminder_add", "joiner_notification_for_inviter", "message_deleted", "tombstone",
})  # fmt: skip
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Fetch = Callable[[str, dict[str, Any]], dict[str, Any]]  # (method, params) -> JSON


class SlackError(Exception):
    pass


def fetcher(token: str, base: str = API, gap_s: float = GAP_S) -> Fetch:
    """GETs to Slack's Web API with the token, spaced out, retrying a 429 after its wait.
    A response with ok false raises SlackError with Slack's error code."""
    last = [0.0]

    def get(method: str, params: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(  # noqa: S310 (a fixed https API)
            f"{base}/{method}" + (f"?{q}" if q else ""),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Scout"},
        )
        for attempt in range(4):
            wait = last[0] + gap_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            try:
                with _opener.open(req, timeout=TIMEOUT_S) as r:
                    out: dict[str, Any] = json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(retry_after(exc.headers.get("Retry-After")))
                    continue
                raise SlackError(f"{method}: HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                raise SlackError(f"{method}: {exc}") from None
            if not out.get("ok"):
                raise SlackError(f"{method}: {out.get('error') or 'not ok'}")
            return out
        raise SlackError(f"{method}: still rate limited")

    return get


def _pages(
    get: Fetch, method: str, params: dict[str, Any], key: str, max_pages: int
) -> Iterator[dict[str, Any]]:
    cursor = None
    for _ in range(max_pages):
        page = get(method, {**params, "cursor": cursor})
        yield from page.get(key) or []
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return
    log.info("slack: %s stopped after %d pages", method, max_pages)


def conversations(get: Fetch) -> list[dict[str, Any]]:
    """The conversations the user is a member of, archived ones left out."""
    params = {"types": "public_channel,private_channel,im,mpim", "exclude_archived": "true", "limit": PAGE}
    return [
        c for c in _pages(get, "users.conversations", params, "channels", 100) if not c.get("is_archived")
    ]


def muted(get: Fetch) -> set[str]:
    """Channels the user has muted, from their preferences when Slack gives them (best
    effort: users.prefs.get isn't a documented method; nothing is muted when it fails)."""
    try:
        prefs = get("users.prefs.get", {}).get("prefs") or {}
    except SlackError as exc:
        log.debug("slack: no muted channels (%s)", exc)
        return set()
    out = {c for c in str(prefs.get("muted_channels") or "").split(",") if c}
    try:
        notif = json.loads(prefs.get("all_notifications_prefs") or "{}")
        out |= {c for c, p in (notif.get("channels") or {}).items() if isinstance(p, dict) and p.get("muted")}
    except (TypeError, ValueError, AttributeError):
        pass
    return out


class Names:
    """User ids to display names, looked up once (users.info) and kept in the state."""

    def __init__(self, get: Fetch, cache: dict[str, str]) -> None:
        self.get, self.cache = get, cache

    def __call__(self, uid: str | None) -> str:
        if not uid:
            return "someone"
        if uid not in self.cache:
            try:
                u = self.get("users.info", {"user": uid}).get("user") or {}
                p = u.get("profile") or {}
                self.cache[uid] = str(
                    p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid
                )
            except SlackError as exc:
                log.info("slack: no name for %s (%s)", uid, exc)
                return uid
        return self.cache[uid]


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL = re.compile(r"<#[CG][A-Z0-9]+\|([^>]*)>")
_LINK = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")


def plain(text: str, names: Callable[[str | None], str]) -> str:
    """Slack's markup as plain text: <@U1> becomes @Pat, <#C1|general> #general, a link its label."""
    text = _MENTION.sub(lambda m: "@" + names(m.group(1)), text)
    text = _CHANNEL.sub(lambda m: "#" + m.group(1), text)
    text = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = _SPECIAL.sub(lambda m: "@" + m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def title_of(conv: dict[str, Any], names: Callable[[str | None], str]) -> str:
    if conv.get("is_im"):
        return f"DM with {names(conv.get('user'))}"
    name = str(conv.get("name") or conv.get("id") or "")
    if conv.get("is_mpim"):
        people = re.sub(r"^mpdm-|-\d+$", "", name).split("--")
        return "Group DM with " + ", ".join(p for p in people if p)
    return f"#{name}"


def is_bot(m: dict[str, Any]) -> bool:
    return bool(m.get("bot_id") or m.get("subtype") == "bot_message" or m.get("bot_profile"))


def sender(m: dict[str, Any], names: Callable[[str | None], str]) -> str:
    if m.get("user"):
        return names(str(m["user"]))
    return str(m.get("username") or (m.get("bot_profile") or {}).get("name") or "a bot")


def line(m: dict[str, Any], names: Callable[[str | None], str]) -> str:
    text = " ".join(plain(str(m.get("text") or ""), names).split())
    files = [str(f.get("title") or f.get("name") or "") for f in m.get("files") or [] if isinstance(f, dict)]
    files = [f"[file: {f}]" for f in files if f]
    return f"{sender(m, names)}: " + " ".join([text, *files]).strip()


def permalink(team_url: str, channel: str, ts: str, thread_ts: str | None = None) -> str | None:
    if not team_url:
        return None
    url = f"{team_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


def documents(
    channel: str,
    title: str,
    msgs: Sequence[dict[str, Any]],
    names: Callable[[str | None], str],
    team_url: str = "",
) -> list[dict[str, Any]]:
    """A message, or a thread (root first, then replies), as index items: one, or the
    thread in chunks of about CHUNK_CHARS."""
    root = msgs[0]
    ts = str(root["ts"])
    thread = len(msgs) > 1 or bool(root.get("reply_count"))
    people: list[str] = []
    for m in msgs:
        s = sender(m, names)
        if s not in people:
            people.append(s)
    meta: dict[str, Any] = {
        "source": "slack",
        "channel": channel,
        "conversation": title,
        "ts": ts,
        "thread_ts": ts if thread else None,
        "last_ts": max(str(m["ts"]) for m in msgs),
        "latest_reply": root.get("latest_reply"),
        "bot": all(is_bot(m) for m in msgs),
        "permalink": permalink(team_url, channel, ts),
    }
    base = {"title": title, "who": ", ".join(people)[:300], "ts": float(ts)}
    lines = [line(m, names) for m in msgs]
    chunks: list[list[str]] = [[]]
    size = 0
    for said in lines:
        if chunks[-1] and size + len(said) > CHUNK_CHARS:
            chunks.append([])
            size = 0
        chunks[-1].append(said)
        size += len(said) + 1
    docs = []
    for n, chunk in enumerate(chunks, 1):
        doc_id = f"slack:{channel}:{ts}" + (f"#{n}" if n > 1 else "")
        part = {"part": n} if len(chunks) > 1 else {}
        docs.append({**base, "id": doc_id, "body": "\n".join(chunk), "meta": {**meta, **part}})
    return docs


def store_docs(ix: index.Store, docs: Sequence[dict[str, Any]]) -> int:
    """Put a message's or thread's items, and drop parts a shorter thread no longer has.
    Each changed item notes when it was stored (stored_at)."""
    now = time.time()
    changed = 0
    for d in docs:
        old = ix.get(d["id"])
        if old is not None and old.body == d["body"] and old.title == d["title"] and old.who == d["who"]:
            continue  # unchanged: keep its stored_at, so the farm doesn't read it again
        changed += ix.put(kind=KIND, **{**d, "meta": {**d["meta"], "stored_at": now}})
    head = docs[0]["id"]
    n = len(docs)
    while ix.get(f"{head}#{n + 1}") is not None:
        n += 1
        ix.delete(f"{head}#{n}")
        changed += 1
    return changed


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _fts(ts: str | None) -> float:
    try:
        return float(ts or 0)
    except ValueError:
        return 0.0


def sync_conversation(
    ix: index.Store,
    get: Fetch,
    conv: dict[str, Any],
    names: Names,
    resume: str | None,
    oldest: float,
    team_url: str = "",
) -> tuple[int, int, str | None]:
    """One conversation's messages since `oldest` into the store, with their threads.
    Returns (messages seen, items changed, the newest message ts)."""
    cid = str(conv["id"])
    title = title_of(conv, names)
    newest = resume
    seen = changed = threads = 0
    params = {"channel": cid, "oldest": f"{oldest:.6f}", "limit": PAGE}
    for m in _pages(get, "conversations.history", params, "messages", MAX_PAGES):
        ts = str(m.get("ts") or "")
        if not ts:
            continue
        if newest is None or _fts(ts) > _fts(newest):
            newest = ts
        if m.get("subtype") in SKIP_SUBTYPES or (m.get("thread_ts") and m["thread_ts"] != ts):
            continue  # noise, or a reply also sent to the channel: it's stored with its thread
        seen += 1
        msgs = [m]
        if m.get("reply_count"):
            old = ix.get(f"slack:{cid}:{ts}")
            if old is not None and old.meta.get("latest_reply") == m.get("latest_reply"):
                continue  # the thread hasn't changed since it was stored
            if threads >= MAX_THREADS:
                continue  # next pass
            threads += 1
            replies = list(
                _pages(get, "conversations.replies", {"channel": cid, "ts": ts, "limit": PAGE}, "messages",
                       MAX_REPLY_PAGES)
            )  # fmt: skip
            msgs = [
                m,
                *(r for r in replies if str(r.get("ts")) != ts and r.get("subtype") not in SKIP_SUBTYPES),
            ]
        changed += store_docs(ix, documents(cid, title, msgs, names, team_url))
    return seen, changed, newest


def sync_pass(
    ix: index.Store,
    get: Fetch,
    backfill_days: float = 14.0,
    skip: Sequence[str] = (),
    state_path: Path | None = None,
    now: float | None = None,
) -> tuple[int, int]:
    """Bring new Slack messages into the store, each conversation from its own resume point
    (the first time, from `backfill_days` back). Returns (messages, items changed)."""
    path = state_path or STATE
    now = time.time() if now is None else now
    state = load_state(path)
    chans: dict[str, dict[str, Any]] = state.setdefault("channels", {})
    names = Names(get, state.setdefault("users", {}))
    skipped = {s.lower().lstrip("#") for s in skip}
    try:
        team_url = str(get("auth.test", {}).get("url") or "")
    except SlackError as exc:
        if "invalid_auth" in str(exc) or "not_authed" in str(exc) or "token_revoked" in str(exc):
            raise
        team_url = ""
    quiet = muted(get)
    total = changed = 0
    try:
        for conv in conversations(get):
            cid = str(conv.get("id") or "")
            name = str(conv.get("name") or "").lower()
            if not cid or cid in quiet or (name and name in skipped):
                continue
            if conv.get("is_im") and names(conv.get("user")).lower() in skipped:
                continue
            st = chans.setdefault(cid, {})
            resume = st.get("ts")
            last = _fts(resume)
            if (
                last
                and now - last > QUIET_DAYS * 86400
                and now - float(st.get("checked") or 0) < QUIET_EVERY_S
            ):
                continue
            start = now - backfill_days * 86400
            oldest = max(start, last - LOOKBACK_S) if last else start
            try:
                n, c, newest = sync_conversation(ix, get, conv, names, resume, oldest, team_url)
            except SlackError as exc:
                if "ratelimited" in str(exc) or "rate limited" in str(exc):
                    raise
                log.info("slack: %s skipped this pass (%s)", cid, exc)
                continue
            total += n
            changed += c
            if newest:
                st["ts"] = newest
            st["checked"] = now
            save_state(path, state)
    finally:
        save_state(path, state)
    return total, changed
