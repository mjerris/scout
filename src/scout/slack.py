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

State (state/slack.json, State): per conversation, the newest message ts stored and
when it was last checked; the user names already looked up. A quiet conversation
(nothing new for QUIET_DAYS) is checked every QUIET_EVERY_S rather than every pass.

Live events (slack_events.py, Socket Mode) come through handle_event: each new, edited
or deleted message, or thread reply, is stored with the same code as a pass (documents,
store_docs, thread_messages; the same skip, muted and archived rules), and moves its
conversation's resume point up. With events flowing, a pass reads back LIVE_LOOKBACK_S
instead of LOOKBACK_S. A deleted message heard as an event leaves the copy (a pass never
sees deletions: then it stays until it ages out).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
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
LIVE_LOOKBACK_S = 600.0  # ...or this much, while live events bring the edits and replies
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


def fetcher(token: str, base: str = API, gap_s: float = GAP_S, post: bool = False) -> Fetch:
    """Calls to Slack's Web API with the token, spaced out (across threads: the poller and
    the event handler share one), retrying a 429 after its wait. GETs, or with `post` form
    POSTs (chat.postMessage). A response with ok false raises SlackError with Slack's
    error code."""
    last = [0.0]
    lock = threading.Lock()

    def get(method: str, params: dict[str, Any]) -> dict[str, Any]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(  # noqa: S310 (a fixed https API)
            f"{base}/{method}" + (f"?{q}" if q and not post else ""),
            data=q.encode() if post else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Scout"},
        )
        for attempt in range(4):
            with lock:
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
    """User ids to display names, looked up once (users.info) and kept in the state; with
    `people`, each one's display, real and user name too (for [slack] announce)."""

    def __init__(
        self,
        get: Fetch,
        cache: dict[str, str],
        people: dict[str, dict[str, str]] | None = None,
        lock: threading.RLock | None = None,
    ) -> None:
        self.get, self.cache = get, cache
        self.people = {} if people is None else people
        self.lock = lock or threading.RLock()

    def _look_up(self, uid: str) -> bool:
        try:
            u = self.get("users.info", {"user": uid}).get("user") or {}
        except SlackError as exc:
            log.info("slack: no name for %s (%s)", uid, exc)
            return False
        p = u.get("profile") or {}
        with self.lock:
            self.cache[uid] = str(
                p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or uid
            )
            self.people[uid] = {
                "display": str(p.get("display_name") or ""),
                "real": str(p.get("real_name") or u.get("real_name") or ""),
                "name": str(u.get("name") or ""),
            }
        return True

    def __call__(self, uid: str | None) -> str:
        if not uid:
            return "someone"
        if uid not in self.cache and not self._look_up(uid):
            return uid
        return self.cache[uid]

    def person(self, uid: str) -> dict[str, str]:
        """Display, real and user name (looked up if only the display name is kept)."""
        if uid not in self.people:
            self._look_up(uid)
        return self.people.get(uid) or {"display": self.cache.get(uid, ""), "real": "", "name": ""}


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


def drop_item(ix: index.Store, doc_id: str) -> int:
    """Take a message's or thread's item, and its parts, out of the store."""
    n = 0
    while ix.get(doc_id if n == 0 else f"{doc_id}#{n + 1}") is not None:
        ix.delete(doc_id if n == 0 else f"{doc_id}#{n + 1}")
        n += 1
    return n


class State:
    """state/slack.json, shared in scout-memory by the poller and the event handler (they
    run in different threads): per conversation, the newest message stored ("ts", the
    resume point) and when the poller last checked it; names; the conversations and muted
    channels the last pass saw (so an event needn't ask again); messages announced."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE
        self.lock = threading.RLock()
        self.data = load_state(self.path)
        for key in ("channels", "users", "people", "convs", "announced"):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}

    @property
    def channels(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = self.data["channels"]
        return out

    def names(self, get: Fetch) -> Names:
        return Names(get, self.data["users"], self.data["people"], self.lock)

    def advance(self, cid: str, ts: str | None, checked: float | None = None) -> None:
        """Move a conversation's resume point forward to `ts` (never back)."""
        with self.lock:
            st = self.channels.setdefault(cid, {})
            if ts and _fts(ts) > _fts(st.get("ts")):
                st["ts"] = ts
            if checked is not None:
                st["checked"] = checked

    def save(self) -> None:
        with self.lock:
            save_state(self.path, self.data)


def left_out(
    conv: dict[str, Any], names: Callable[[str | None], str], skipped: set[str], quiet: set[str]
) -> bool:
    """Archived, muted, or named in [slack] skip (a channel, or the person a DM is with)."""
    cid = str(conv.get("id") or "")
    name = str(conv.get("name") or "").lower()
    if not cid or conv.get("is_archived") or cid in quiet or (name and name in skipped):
        return True
    return bool(conv.get("is_im") and names(conv.get("user")).lower() in skipped)


def skip_set(skip: Sequence[str]) -> set[str]:
    return {s.lower().lstrip("#") for s in skip}


def thread_messages(
    get: Fetch, cid: str, ts: str, root: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """A thread as stored: its root (from conversations.replies unless given), then the
    replies that aren't noise. Empty when Slack has no root for it."""
    replies = list(
        _pages(
            get,
            "conversations.replies",
            {"channel": cid, "ts": ts, "limit": PAGE},
            "messages",
            MAX_REPLY_PAGES,
        )
    )
    if root is None:
        root = next((r for r in replies if str(r.get("ts")) == ts), None)
        if root is None:
            return []
    return [root, *(r for r in replies if str(r.get("ts")) != ts and r.get("subtype") not in SKIP_SUBTYPES)]


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
            msgs = thread_messages(get, cid, ts, root=m)
        changed += store_docs(ix, documents(cid, title, msgs, names, team_url))
    return seen, changed, newest


def _conv_entry(c: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "is_im", "is_mpim", "user", "is_archived", "is_channel", "is_group", "is_private")
    return {k: c[k] for k in keys if k in c}


def sync_pass(
    ix: index.Store,
    get: Fetch,
    backfill_days: float = 14.0,
    skip: Sequence[str] = (),
    state_path: Path | None = None,
    now: float | None = None,
    state: State | None = None,
    live_since: float | None = None,
) -> tuple[int, int]:
    """Bring new Slack messages into the store, each conversation from its own resume point
    (the first time, from `backfill_days` back). Returns (messages, items changed).

    `live_since`: when live events (slack_events.py) have been coming in without a break
    since then, a conversation last checked after it is read back only LIVE_LOOKBACK_S
    from its resume point, not LOOKBACK_S: its edits and new replies came as events."""
    st_all = state or State(state_path)
    now = time.time() if now is None else now
    chans = st_all.channels
    names = st_all.names(get)
    skipped = skip_set(skip)
    try:
        team_url = str(get("auth.test", {}).get("url") or "")
    except SlackError as exc:
        if "invalid_auth" in str(exc) or "not_authed" in str(exc) or "token_revoked" in str(exc):
            raise
        team_url = ""
    quiet = muted(get)
    total = changed = 0
    try:
        convs = conversations(get)
        with st_all.lock:
            st_all.data["convs"] = {str(c["id"]): _conv_entry(c) for c in convs if c.get("id")}
            st_all.data["muted"] = sorted(quiet)
            if team_url:
                st_all.data["team_url"] = team_url
        for conv in convs:
            cid = str(conv.get("id") or "")
            if left_out(conv, names, skipped, quiet):
                continue
            with st_all.lock:
                st = dict(chans.get(cid) or {})
            resume = st.get("ts")
            last = _fts(resume)
            checked = float(st.get("checked") or 0)
            if last and now - last > QUIET_DAYS * 86400 and now - checked < QUIET_EVERY_S:
                continue
            start = now - backfill_days * 86400
            live = live_since is not None and checked >= live_since
            oldest = max(start, last - (LIVE_LOOKBACK_S if live else LOOKBACK_S)) if last else start
            try:
                n, c, newest = sync_conversation(ix, get, conv, names, resume, oldest, team_url)
            except SlackError as exc:
                if "ratelimited" in str(exc) or "rate limited" in str(exc):
                    raise
                log.info("slack: %s skipped this pass (%s)", cid, exc)
                continue
            total += n
            changed += c
            st_all.advance(cid, newest, checked=now)
            st_all.save()
    finally:
        st_all.save()
    return total, changed


# --- live events (slack_events.py receives them; this stores them like a pass would) -----


EVENT_SUBTYPES = frozenset({None, "file_share", "thread_broadcast", "me_message"})  # said by a person
ANNOUNCED_KEEP_S = 2 * 86400.0


@dataclass
class Live:
    """What handling an event needs besides the store. auth.test (the team's URL, the
    user's own id) and the muted channels are refreshed every REFRESH_S."""

    get: Fetch
    state: State
    skip: Sequence[str] = ()
    announce: Sequence[str] = ()
    announce_dms: str = "listed"  # "listed", "all" or "off" ([slack] announce_dms)
    announce_mentions: bool = False
    team_url: str = ""
    me: str = ""
    quiet: set[str] = field(default_factory=set)
    refreshed: float = 0.0

    REFRESH_S = 3600.0

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.refreshed and now - self.refreshed < self.REFRESH_S:
            return
        me = self.get("auth.test", {})
        self.team_url, self.me = str(me.get("url") or ""), str(me.get("user_id") or "")
        self.quiet = muted(self.get)
        self.refreshed = now

    def conversation(self, cid: str) -> dict[str, Any]:
        """The conversation as the last pass listed it, else conversations.info (kept)."""
        with self.state.lock:
            conv = self.state.data["convs"].get(cid)
        if conv is None:
            conv = _conv_entry(self.get("conversations.info", {"channel": cid}).get("channel") or {"id": cid})
            with self.state.lock:
                self.state.data["convs"][cid] = conv
        return dict(conv)


def _store_message(ix: index.Store, live: Live, cid: str, title: str, m: dict[str, Any], names: Names) -> str:
    """A message as the poller stores it: on its own, or (a reply, or a root that has
    replies) its whole thread re-read. Returns the item's id."""
    ts = str(m["ts"])
    thread = str(m.get("thread_ts") or "")
    if thread and (thread != ts or m.get("reply_count")):
        msgs = thread_messages(live.get, cid, thread)
        if msgs and msgs[0].get("subtype") not in SKIP_SUBTYPES:
            store_docs(ix, documents(cid, title, msgs, names, live.team_url))
        return f"slack:{cid}:{thread}"
    store_docs(ix, documents(cid, title, [m], names, live.team_url))
    return f"slack:{cid}:{ts}"


def handle_event(
    ix: index.Store, live: Live, ev: dict[str, Any], now: float | None = None
) -> list[dict[str, Any]]:
    """One message event (new, edited, deleted, a thread reply) into the store, the way a
    pass stores it, and the conversation's resume point moved up. Returns the alerts it
    makes: a new DM or group DM from someone in [slack] announce (each message once)."""
    if ev.get("type") != "message" or not ev.get("channel"):
        return []
    live.refresh(now)
    cid = str(ev["channel"])
    sub = ev.get("subtype")
    conv = live.conversation(cid)
    names = live.state.names(live.get)
    if left_out(conv, names, skip_set(live.skip), live.quiet):
        return []
    title = title_of(conv, names)
    if sub == "message_changed":
        m = ev.get("message") or {}
        if ev.get("hidden"):  # Slack's own bookkeeping (a parent's reply count): the reply came too
            return []
        if isinstance(m, dict) and m.get("ts") and m.get("subtype") not in SKIP_SUBTYPES:
            _store_message(ix, live, cid, title, m, names)
        return []
    if sub == "message_deleted":
        gone, prev = str(ev.get("deleted_ts") or ""), ev.get("previous_message") or {}
        thread = str(prev.get("thread_ts") or "") if isinstance(prev, dict) else ""
        if thread and thread != gone:  # a reply: its thread without it
            msgs = thread_messages(live.get, cid, thread)
            if msgs and msgs[0].get("subtype") not in SKIP_SUBTYPES:
                store_docs(ix, documents(cid, title, msgs, names, live.team_url))
        elif gone:
            drop_item(ix, f"slack:{cid}:{gone}")
        return []
    ts = str(ev.get("ts") or "")
    if not ts or sub in SKIP_SUBTYPES or sub == "message_replied":
        return []
    doc_id = _store_message(ix, live, cid, title, ev, names)
    live.state.advance(cid, ts)
    alert = _alert(live, conv, ev, doc_id, names, now)
    live.state.save()
    return [alert] if alert else []


def announced_as(entries: Sequence[str], uid: str, person: dict[str, str]) -> str | None:
    """The name to say when this sender is in [slack] announce: an entry matches the user
    id, a display, real or user name, or (one word) a first name: "Luca" matches "Luca
    Pradovera". Said as the entry is written; an id entry is said as the first name."""
    known = [n.lower() for n in (person.get("display"), person.get("real"), person.get("name")) if n]
    for raw in entries:
        e = raw.strip().lstrip("@")
        if not e:
            continue
        if e.upper() == uid:
            return _first_name(person)
        el = e.lower()
        if el in known or (" " not in el and any(n.split()[0] == el for n in known)):
            return e
    return None


def announcing(cfg: Any) -> bool:
    """Whether [slack] has anything to say out loud: live events, and DMs or mentions on."""
    said = (
        cfg.announce_mentions or cfg.announce_dms == "all" or (cfg.announce_dms == "listed" and cfg.announce)
    )
    return bool(cfg.token and cfg.app_token and said)


def _first_name(person: dict[str, str]) -> str:
    name = person.get("real") or person.get("display") or person.get("name") or "Someone"
    return name.split()[0]


def _alert(
    live: Live, conv: dict[str, Any], m: dict[str, Any], doc_id: str, names: Names, now: float | None
) -> dict[str, Any] | None:
    uid = str(m.get("user") or "")
    if not uid or uid == live.me or is_bot(m) or m.get("subtype") not in EVENT_SUBTYPES:
        return None
    dm = bool(conv.get("is_im") or conv.get("is_mpim"))
    who: str | None = None
    mention = ""
    if dm and live.announce_dms == "all":
        who = announced_as(live.announce, uid, names.person(uid)) or _first_name(names.person(uid))
    elif dm and live.announce_dms == "listed":
        who = announced_as(live.announce, uid, names.person(uid))
    elif not dm and live.announce_mentions and live.me and f"<@{live.me}>" in str(m.get("text") or ""):
        who = announced_as(live.announce, uid, names.person(uid)) or _first_name(names.person(uid))
        mention = f"#{conv.get('name')}" if conv.get("name") else "a channel"
    if who is None:
        return None
    now = time.time() if now is None else now
    key = f"{conv.get('id')}:{m['ts']}"
    with live.state.lock:
        seen: dict[str, float] = live.state.data["announced"]
        for k in [k for k, at in seen.items() if now - float(at) > ANNOUNCED_KEEP_S]:
            del seen[k]
        if key in seen:
            return None
        seen[key] = now
    alert = {"id": doc_id, "who": who, "channel": str(conv.get("id")), "ts": str(m["ts"]),
             "group": bool(conv.get("is_mpim"))}  # fmt: skip
    if mention:
        alert["mention"] = mention
    return alert
