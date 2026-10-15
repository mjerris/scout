"""Cross-references between records (xref.py, index.Index.link_xrefs): GitHub refs,
pasted links, meetings named by title, meetings matched by meaning. Synthetic records."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest

from scout import config, granola, index, links, xref

DAY = 86400.0
T0 = dt.datetime(2026, 10, 14, 10, 0).timestamp()  # a Tuesday, 10:00


# --- plain rules ---------------------------------------------------------------------------


def test_github_urls_and_short_refs_are_found_once_in_order() -> None:
    text = (
        "see https://github.com/SignalWire/voice-sdk/pull/117 and signalwire/voice-sdk#117 again; "
        "issue https://github.com/acme/api/issues/9, commit https://github.com/acme/api/commit/0123456789abcdef0123"
    )
    refs = xref.github_refs(text)
    assert [r.id for r in refs] == [
        "github:signalwire/voice-sdk#117", "github:acme/api#9", "github:acme/api@0123456789ab",
    ]  # fmt: skip
    assert refs[0].kind == "pr" and refs[0].url == "https://github.com/signalwire/voice-sdk/pull/117"
    assert refs[2].kind == "commit" and refs[2].sha == "0123456789abcdef0123"


def test_a_bare_number_needs_one_repo_and_github_words() -> None:
    one = "Can you review #118? It's the follow-up to https://github.com/acme/api/pull/117"
    assert [r.id for r in xref.github_refs(one)] == ["github:acme/api#118", "github:acme/api#117"]
    two = "review #118 (https://github.com/acme/api/pull/1, https://github.com/acme/web/pull/2)"
    assert "github:acme/api#118" not in {r.id for r in xref.github_refs(two)}
    no_words = "Option #2 is better, per https://github.com/acme/api"
    assert xref.github_refs(no_words) == []
    assert xref.github_refs("PR 42 is up") == []  # no repo named: nothing to resolve it to


def test_the_allow_list_keeps_only_its_orgs_and_repos() -> None:
    text = "acme/api#1 other/thing#2 solo/repo#3"
    allow = xref.Allow(orgs=["acme"], repos=["solo/repo"])
    assert [r.id for r in xref.github_refs(text, allow)] == ["github:acme/api#1", "github:solo/repo#3"]
    assert len(xref.github_refs(text, xref.Allow())) == 3


def test_ids_parse_back_to_refs() -> None:
    r = xref.parse_id("github:acme/api#12")
    assert r is not None and r.number == 12 and r.url == "https://github.com/acme/api/issues/12"
    c = xref.parse_id("github:acme/api@abcdef1234")
    assert c is not None and c.sha == "abcdef1234"
    assert xref.parse_id("github:nope") is None


def test_a_found_task_is_about_the_item_it_names_or_the_only_one_with_github_words() -> None:
    items = xref.github_refs(
        "PR https://github.com/acme/api/pull/117 and https://github.com/acme/api/issues/90"
    )
    sub = xref.task_subject("Review PR 117", "I'll review #117 today", items)
    assert sub is not None and sub.id == "github:acme/api#117"
    assert xref.task_subject("Send Pat the budget", "can you send the budget", items) is None
    one = xref.github_refs("https://github.com/acme/api/pull/117")
    got = xref.task_subject("Look at the PR", "can you look at the PR", one)
    assert got is not None and got.id == "github:acme/api#117"
    assert xref.task_subject("Send the deck", "send me the deck", one) is None  # not about it
    assert xref.task_subject("Review #117 and #90", "review #117 and #90", items) is None  # two things


def test_pasted_links_are_read() -> None:
    text = (
        "notes: https://notes.granola.ai/d/abc123?x=1. thread "
        "<https://team.slack.com/archives/C0123ABC/p1760450000123456?thread_ts=1760449000.000100&cid=C0123ABC> "
        "and https://team.slack.com/archives/C0999/p1760450000000200 mail message://%3Cabc%40mail.example%3E"
    )
    assert xref.granola_urls(text) == ["https://notes.granola.ai/d/abc123"]
    assert xref.slack_links(text) == [("C0123ABC", "1760449000.000100"), ("C0999", "1760450000.000200")]
    assert xref.message_ids(text) == ["abc@mail.example"]


def _m(id: str, start: float, title: str, who: str = "") -> xref.Meeting:
    return xref.Meeting(id, start, title, who)


def test_a_meeting_is_named_by_its_title_near_its_date() -> None:
    ms = [
        _m("granola:a", T0 - 2 * 3600, "Vendor pricing review"),
        _m("granola:b", T0 - 3600, "Vendor sync"),
        _m("granola:c", T0 - 10 * DAY, "Budget planning"),
        _m("granola:d", T0 - 3 * 3600, "Daily standup"),
        _m("granola:e", T0 - DAY - 3 * 3600, "Daily standup"),
    ]
    assert xref.named_meeting("Per the vendor pricing review, the quote is due", T0, ms) == "granola:a"
    assert xref.named_meeting("as we said in the vendor sync", T0, ms) == "granola:b"
    assert xref.named_meeting("the budget planning numbers", T0, ms) is None  # ten days back
    assert xref.named_meeting("from today's standup: ship it", T0, ms) == "granola:d"
    assert xref.named_meeting("in yesterday's standup", T0, ms) == "granola:e"
    assert xref.named_meeting("the vendor is late", T0, ms) is None  # a word, not the meeting


def test_two_meetings_that_fit_equally_give_none_unless_one_was_in_the_last_day() -> None:
    ms = [_m("granola:x", T0 - 2 * DAY, "Launch review"), _m("granola:y", T0 - 3600, "Launch review")]
    assert xref.named_meeting("notes from the launch review", T0, ms) == "granola:y"
    ms2 = [_m("granola:x", T0 - 3600, "Launch review"), _m("granola:y", T0 - 1800, "Launch review")]
    assert xref.named_meeting("notes from the launch review", T0, ms2) is None


def test_nearest_needs_the_floor_and_a_margin() -> None:
    assert xref.nearest([("a", 0.9), ("b", 0.7)], 0.8, 0.03) == "a"
    assert xref.nearest([("a", 0.9), ("b", 0.89)], 0.8, 0.03) is None
    assert xref.nearest([("a", 0.7)], 0.8, 0.03) is None
    assert xref.nearest([], 0.8) is None


def test_first_names_leave_the_user_out() -> None:
    assert xref.first_names("Pat Doe, Mike Jerris, Sam <sam@x.com>, luca@x.com", ["Mike Jerris"]) == {
        "pat", "sam", "luca",
    }  # fmt: skip


# --- the store's pass ----------------------------------------------------------------------


class WordEmbedder:
    """Bag of words, hashed: texts sharing words are near (no model needed)."""

    model = "words@256"

    def embed(self, texts: Sequence[str], query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), 256), np.float32)
        for i, t in enumerate(texts):
            for w in index.fts_query(t).replace('"', "").split(" OR "):
                if w:
                    out[i, int(hashlib.blake2b(w.encode(), digest_size=4).hexdigest(), 16) % 256] += 1.0
        out /= np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
        return out


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> index.Index:
    s = index.default()
    assert isinstance(s, index.Index)
    cfg = config.Config()
    cfg.tasks.me = ["Mike Jerris"]
    monkeypatch.setattr(config, "load", lambda: cfg)
    return s


def note(s: index.Index, nid: str, title: str, start: float, who: str = "Pat Doe, Mike Jerris",
         summary: str = "") -> None:  # fmt: skip
    granola.store_note(s, {
        "id": nid, "title": title, "web_url": f"https://notes.granola.ai/d/{nid}", "updated_at": "2026-10-10T10:00:00Z",
        "summary_text": summary or f"Summary of {title}.", "attendees": [{"name": n.strip()} for n in who.split(",")],
        "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(start, dt.UTC).isoformat()},
        "transcript": [{"speaker": {"source": "speaker"}, "text": "We'll send the deck."}],
    })  # fmt: skip


def slack(s: index.Index, ch: str, ts: float, text: str, who: str = "Pat Doe", conv: str = "#launch") -> str:
    t = f"{ts:.6f}"
    id = f"slack:{ch}:{t}"
    s.put(id, "slack", conv, f"{who}: {text}", who, ts,
          {"channel": ch, "conversation": conv, "ts": t, "last_ts": t,
           "permalink": f"https://team.slack.com/archives/{ch}/p{t.replace('.', '')}"})  # fmt: skip
    return id


def rels(s: index.Index, id: str) -> set[tuple[str, str, str]]:
    return {(ln["id"], ln["rel"], ln["dir"]) for ln in s.links(id)}


def test_a_granola_link_in_slack_links_both_ways(store: index.Index) -> None:
    note(store, "n1", "Vendor pricing review", T0 - 3600)
    sid = slack(store, "C1", T0, "notes are at https://notes.granola.ai/d/n1 if you missed it")
    read, made = store.link_xrefs(now=T0 + 60)
    assert read >= 2 and made == 1
    assert ("granola:n1", "mentions", "out") in rels(store, sid)
    assert (sid, "mentions", "in") in rels(store, "granola:n1")
    assert store.link_xrefs(now=T0 + 120) == (0, 0)  # idempotent: nothing changed, nothing read


def test_a_meeting_naming_a_slack_thread_and_mail_naming_both(store: index.Index) -> None:
    sid = slack(store, "C1", T0 - 600, "the jitter numbers")
    store.put(
        f"{sid}#2", "slack", "#launch", "Sam: more numbers", "Sam", T0 - 600, {"channel": "C1", "part": 2}
    )
    note(store, "n2", "Jitter triage", T0, summary="We looked at https://team.slack.com/archives/C1/p"
         + f"{T0 - 600:.6f}".replace(".", "") + " and agreed.")  # fmt: skip
    store.put("mail:7", "mail", "Re: jitter", "thread: message://%3Cxyz%40mail.example%3E and "
              "https://notes.granola.ai/d/n2", "Pat Doe <pat@x.com>", T0 + 3600, {"mail_id": 7})  # fmt: skip
    store.put("mail:6", "mail", "jitter", "first", "Pat Doe <pat@x.com>", T0 - 7200,
              {"mail_id": 6, "message_id": "xyz@mail.example"})  # fmt: skip
    store.link_xrefs(now=T0 + 7200)
    assert ("granola:n2", "mentions", "in") in rels(store, sid)
    assert {("mail:6", "mentions", "out"), ("granola:n2", "mentions", "out")} <= rels(store, "mail:7")


def test_a_meeting_named_by_title_and_a_link_that_changes_with_the_text(store: index.Index) -> None:
    note(store, "n3", "Vendor pricing review", T0 - 3600)
    sid = slack(store, "C1", T0, "per the vendor pricing review, can you send the quote?")
    store.link_xrefs(now=T0 + 60)
    assert ("granola:n3", "mentions", "out") in rels(store, sid)
    slack(store, "C1", T0, "can you send the quote?")  # edited: the link goes
    store.link_xrefs(now=T0 + 120)
    assert rels(store, sid) == set()


def test_a_message_before_its_meeting_was_stored_is_read_again(store: index.Index) -> None:
    sid = slack(store, "C1", T0, "per the vendor pricing review, can you send the quote?")
    store.link_xrefs(now=T0 + 60)
    assert rels(store, sid) == set()
    note(store, "n4", "Vendor pricing review", T0 - 3600)
    store.link_xrefs(now=T0 + 120)  # the meeting: and the messages around it are due again
    store.link_xrefs(now=T0 + 180)
    assert ("granola:n4", "mentions", "out") in rels(store, sid)


def test_github_refs_in_any_kind_link_to_the_github_record_id(store: index.Index) -> None:
    sid = slack(store, "C1", T0, "can you review https://github.com/acme/api/pull/117")
    store.note_add("PRs", "remember acme/api#90 too", ts=T0)
    store.link_xrefs(now=T0 + 60)
    assert ("github:acme/api#117", "mentions", "out") in rels(store, sid)
    assert ("github:acme/api#90", "mentions", "out") in rels(store, "note:1")
    assert set(store.link_targets("github:")) == {"github:acme/api#90", "github:acme/api#117"}


def test_old_records_are_left_alone_and_the_pass_is_bounded(store: index.Index) -> None:
    slack(store, "C1", T0 - 40 * DAY, "https://github.com/acme/api/pull/1")
    for i in range(5):
        slack(store, "C2", T0 - i, f"https://github.com/acme/api/pull/{10 + i}")
    assert store.xref_backlog(now=T0) == 5
    assert store.link_xrefs(limit=3, now=T0)[0] == 3
    assert store.xref_backlog(now=T0) == 2
    store.link_xrefs(now=T0)
    assert store.links(f"slack:C1:{T0 - 40 * DAY:.6f}") == []


def test_a_meeting_matched_by_meaning_needs_a_cue_people_and_a_clear_winner(tmp_path: Any,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    cfg = config.Config()
    cfg.tasks.me = ["Mike Jerris"]
    monkeypatch.setattr(config, "load", lambda: cfg)
    s = index.Index(tmp_path / "v.db", WordEmbedder())
    s.discuss_min = 0.5  # the bag of words' scale, not bge-small's
    note(s, "a", "Weekly eng", T0 - 3 * 3600, "Pat Doe, Mike Jerris",
         "Jitter buffer latency regression on Android; rollback the codec change; Pat measures packet loss")  # fmt: skip
    note(s, "b", "Hiring loop", T0 - 2 * 3600, "Pat Doe, Mike Jerris",
         "Candidate pipeline for the backend role; schedule onsite interviews; recruiter feedback")  # fmt: skip
    yes = slack(s, "C1", T0, "following up from the meeting: jitter buffer latency regression on Android, "
                "rollback the codec change, packet loss measured", "Pat Doe")  # fmt: skip
    stranger = slack(s, "C2", T0, "following up from the meeting: jitter buffer latency regression on Android, "
                     "rollback the codec change", "Quinn Zed")  # fmt: skip
    no_cue = slack(
        s, "C3", T0, "jitter buffer latency regression on Android, rollback the codec change", "Pat Doe"
    )
    assert s.link_xrefs(now=T0 + 60)[0] >= 1
    while s.embed_pending(64):
        pass
    for _ in range(3):
        s.link_xrefs(now=T0 + 60)
    assert ("granola:a", "discusses", "out") in rels(s, yes)
    assert rels(s, stranger) == set()  # nobody from the meeting
    assert rels(s, no_cue) == set()  # not about a meeting
    s.close()


def test_rels_are_documented_and_checked(store: index.Index) -> None:
    assert {"mentions", "discusses"} <= set(links.RELS)
    store.link("slack:C1:1.000000", "granola:x", "mentions")
    with pytest.raises(ValueError, match="rel must be"):
        store.link("a:1", "b:2", "talks_about")


def test_a_labeled_slack_link_keeps_its_url_for_cross_references(store: index.Index) -> None:
    from scout import slack as slack_mod

    msg = {
        "ts": f"{T0:.6f}",
        "user": "U1",
        "text": "please review <https://github.com/acme/api/pull/7|the fix>",
    }
    docs = slack_mod.documents("C5", "#eng", [msg], lambda u: "Pat")
    assert docs[0]["meta"]["urls"] == ["https://github.com/acme/api/pull/7"]
    assert "the fix" in docs[0]["body"] and "github.com" not in docs[0]["body"]
    slack_mod.store_docs(store, docs)
    store.link_xrefs(now=T0 + 60)
    assert ("github:acme/api#7", "mentions", "out") in rels(store, docs[0]["id"])


def test_a_meeting_naming_a_channel_discusses_its_thread(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.Config()
    monkeypatch.setattr(config, "load", lambda: cfg)
    s = index.Index(tmp_path / "c.db", WordEmbedder())
    s.discuss_min = 0.5
    near = slack(s, "C7", T0 - 3600, "TURN server upgrade plan: rolling restart of the media relays tonight",
                 conv="#voice-eng")  # fmt: skip
    slack(s, "C7", T0 - 1800, "lunch order for friday, pizza or tacos", conv="#voice-eng")
    slack(s, "C8", T0 - 1800, "TURN server upgrade plan: rolling restart of the media relays", conv="#other")
    note(s, "ch", "Infra weekly", T0, summary="Pat posted the TURN server upgrade plan in #voice-eng: "
         "rolling restart of the media relays.")  # fmt: skip
    while s.embed_pending(64):
        pass
    s.link_xrefs(now=T0 + 60)
    assert rels(s, "granola:ch") == {(near, "discusses", "out")}
    s.close()
