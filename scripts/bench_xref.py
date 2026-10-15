"""Cross-references between conversations and meetings (xref.py): precision and recall on
a SYNTHETIC set, and counts on a read-only copy of the live store.

    uv run python scripts/bench_xref.py            # the synthetic set (bge-small, ~1 min)
    uv run python scripts/bench_xref.py --live     # counts only, on a copy of the live store

The synthetic set: 16 meetings over ten days (each with attendees and a summary), and
Slack messages about them in every way the rules know: a Granola link pasted, the title
named, "today's standup", and follow-ups that only talk about what was said (matched by
meaning). Hard negatives: messages with meeting words about something no meeting covered,
follow-ups from people who weren't there, a meeting's topic ten days later, and two
meetings on one topic in the same window. It prints, for the rule by meaning, precision
and recall at each cosine floor, which is how xref.DISCUSS_MIN was chosen, and then the
whole pass (every rule) at the floor in xref.py.

--live --github also fetches the GitHub items named into the copy (GET only, with the gh
CLI's token) and prints counts by state, type and privacy: no titles.

--live copies state/memory.db into .tmp/bench_xref/ (SQLite's backup, the live file
opened read-only), runs the pass over the copy's last 30 days, and prints counts: links
by rel and by the kinds they join, GitHub items named, and what already held for Slack
(items with vectors, with permalinks, tasks found in Slack). No text is printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scout import granola, index, xref  # noqa: E402

TMP = ROOT / ".tmp" / "bench_xref"
LIVE = Path(os.environ.get("SCOUT_HOME") or Path.home() / "Library" / "Application Support" / "Scout") / "state" / "memory.db"
DAY = 86400.0
T0 = dt.datetime(2026, 10, 14, 17, 0).timestamp()
ME = "Mike Jerris"

# (title, attendees, hours before T0, summary, follow-ups that only talk about it)
MEETINGS: list[tuple[str, str, float, str, list[str]]] = [
    ("Weekly eng", "Pat Doe, Sam Lee", 30,
     "Jitter buffer latency regressed on Android after the codec change. Decision: roll back the codec change. "
     "Pat measures packet loss on the test fleet; Sam files the regression.",
     ["following up on the meeting: rolled back the codec change, Android jitter is back to normal",
      "Pat, did you get the packet loss numbers from the test fleet we discussed?"]),
    ("Q4 pricing", "Dana Cruz, Lee Park", 50,
     "Discussed raising the per-minute voice price by 10 percent for new customers. Enterprise contracts keep "
     "their rate until renewal. Dana drafts the customer email; finance models churn.",
     ["per our discussion, here's the draft email about the 10 percent per-minute increase",
      "action items from the call: finance to model churn for the voice price increase"]),
    ("Hiring loop: backend", "Quinn Ray, Ana Gomez", 20,
     "Candidate pipeline for the backend role. Two onsite interviews next week; Ana schedules them. Recruiter "
     "asked for feedback on the take-home exercise by Thursday.",
     ["as discussed, I scheduled both onsites for the backend candidates next week",
      "following up: can everyone send take-home feedback to the recruiter by Thursday?"]),
    ("Vendor sync", "Ravi Shah, Pat Doe", 5,
     "Carrier vendor quote for SIP trunking is late. Ravi chases the vendor for the revised quote and the SLA "
     "credits for last month's outage.",
     ["after the call I pinged the carrier again about the revised SIP trunking quote",
      "re the SLA credits for last month's outage: vendor says they'll send them Friday"]),
    ("Launch review", "Ana Gomez, Sam Lee", 26,
     "Go/no-go for the video SDK launch. Blockers: docs for the React Native package and the pricing page. "
     "Launch moves to Tuesday if docs aren't merged by Monday.",
     ["recap: video SDK launch slips to Tuesday unless the React Native docs land Monday",
      "the pricing page blocker from the launch meeting is fixed now"]),
    ("Security incident retro", "Lee Park, Ravi Shah", 70,
     "Retro on the leaked API key in a public repo. Rotate all keys older than 90 days, add secret scanning to "
     "CI, and write the customer notice.",
     ["action items from the retro: secret scanning is on in CI, key rotation is next",
      "notes from the retro meeting: customer notice is drafted, needs legal review"]),
    ("Board prep", "Dana Cruz, Quinn Ray", 100,
     "Prepare the board deck: ARR bridge, hiring plan, and the AI agents roadmap slide. Quinn owns the hiring "
     "slide; Dana updates the ARR numbers after month close.",
     ["for the board deck we discussed: ARR bridge updated with September close",
      "following up: Quinn, hiring plan slide for the board deck?"]),
    ("Daily standup", "Sam Lee, Pat Doe", 3,
     "Sam: finishing the WebRTC reconnect fix. Pat: on-call handoff, two pages overnight from the media "
     "servers. No blockers.",
     ["from standup: the WebRTC reconnect fix is in review",
      "re the overnight pages from the media servers mentioned this morning, root cause was a full disk"]),
    ("Daily standup", "Sam Lee, Pat Doe", 27,
     "Sam: load testing the conference bridge at 500 participants. Pat: upgrading the TURN servers.",
     ["after yesterday's sync: conference bridge held 500 participants in the load test"]),
    ("Customer call: Acme Health", "Lee Park, Dana Cruz", 8,
     "Acme Health wants HIPAA BAA signed before pilot. They asked about call recording retention and "
     "transcription accuracy for medical terms.",
     ["per the call with Acme Health, legal is reviewing the BAA",
      "follow-up for the customer: recording retention is configurable, sending the docs on transcription"]),
    ("Design review: agent builder", "Ana Gomez, Quinn Ray", 45,
     "Reviewed the drag-and-drop agent builder mockups. Concerns: too many steps to add a tool; need an empty "
     "state with templates. Ana revises the flow.",
     ["following up from the design review: revised flow cuts adding a tool to two steps",
      "added the template empty state we talked about in the meeting"]),
    ("1:1 Pat", "Pat Doe", 52,
     "Pat wants to move to the media team next quarter. Discussed growth plan, conference talk proposal, and "
     "on-call load.",
     ["per our 1:1, I submitted the conference talk proposal"]),
    ("Infra cost review", "Ravi Shah, Sam Lee", 75,
     "Cloud bill up 18 percent. Biggest items: idle GPU nodes and cross-region egress. Ravi turns off idle GPU "
     "nodes; Sam moves recordings storage to the same region.",
     ["as discussed in the cost review, idle GPU nodes are off — saving about 9k a month",
      "moved recordings storage to the same region; egress should drop"]),
    ("Partner sync: Telco", "Dana Cruz, Ravi Shah", 120,
     "Telco partner wants a white-label portal and number porting API. Timeline Q1. Dana sends the API spec.",
     ["sent the porting API spec to the telco partner, per the meeting"]),
    ("Docs planning", "Ana Gomez, Lee Park", 32,
     "Docs restructure: quickstarts first, then guides, then reference. Migrate the old SWML pages; Lee writes "
     "the voice quickstart.",
     ["following up on docs planning: voice quickstart draft is up for review"]),
    ("Roadmap offsite", "Quinn Ray, Dana Cruz, Sam Lee", 200,
     "H1 roadmap themes: agents, video, and reliability. Cut the legacy fax product.",
     []),
]

# Messages with meeting words that no meeting in the set is about (each from someone who
# was at some meeting, so only meaning can rule them out).
NEGATIVES = [
    ("Pat Doe", "following up: is anyone going to the team lunch on Friday?"),
    ("Sam Lee", "as discussed, I'll be out Monday for a dentist appointment"),
    ("Dana Cruz", "action items for the holiday party: book the venue and the caterer"),
    ("Ravi Shah", "per the call with my landlord, the office lease renews in March"),
    ("Ana Gomez", "recap of the conference I attended: great talks on Rust and WebAssembly"),
    ("Lee Park", "following up on the expense report, can you approve it?"),
    ("Quinn Ray", "notes from the parenting class: babies need a lot of sleep"),
    ("Pat Doe", "after the call with the bank, my mortgage refinance is approved"),
    ("Sam Lee", "follow-up: the coffee machine on floor 3 is broken again"),
    ("Dana Cruz", "as mentioned, the parking garage closes early on Thursday"),
    ("Ravi Shah", "meeting room B projector doesn't work, IT ticket filed"),
    ("Ana Gomez", "we agreed to switch the team offsite to a cabin in the mountains"),
    ("Lee Park", "discussed with HR: open enrollment for benefits ends next week"),
    ("Quinn Ray", "sync with the travel agent: flights to Lisbon booked"),
    # On a meeting's topic, but from someone who wasn't there:
    ("Zed Quill", "following up on the meeting: rolled back the codec change, Android jitter is back to normal"),
    ("Mo Vance", "per our discussion, here's the draft email about the 10 percent per-minute increase"),
]


def build(ix: index.Index) -> tuple[dict[str, str | None], dict[str, str]]:
    """Fill a store; returns (message id -> the meeting it's about or None, id -> how)."""
    want: dict[str, str | None] = {}
    how: dict[str, str] = {}
    n = 0

    def msg(text: str, who: str, ts: float, about: str | None, kind: str) -> None:
        nonlocal n
        n += 1
        t = f"{ts:.6f}"
        id = f"slack:C{n:03d}:{t}"
        ix.put(id, "slack", "#team", f"{who}: {text}", who, ts, {"channel": f"C{n:03d}", "ts": t, "last_ts": t})
        want[id], how[id] = about, kind

    for i, (title, who, hours, summary, follows) in enumerate(MEETINGS):
        start = T0 - hours * 3600
        nid = f"m{i:02d}"
        granola.store_note(ix, {
            "id": nid, "title": title, "web_url": f"https://notes.granola.ai/d/{nid}",
            "summary_text": summary, "attendees": [{"name": p} for p in [*who.split(", "), ME]],
            "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(start, dt.UTC).isoformat()},
        })  # fmt: skip
        gid = f"granola:{nid}"
        first = who.split(", ")[0]
        later = start + 2 * 3600
        if title not in ("Daily standup",):
            msg(f"notes here https://notes.granola.ai/d/{nid}", first, later, gid, "link")
        if len(xref.title_words(title)) >= 2 or title.lower().endswith(("sync", "review")):
            msg(f"per the {title.lower()}, I'll take the next step", first, later + 60, gid, "title")
        for j, f in enumerate(follows):
            msg(f, first if j % 2 == 0 else who.split(", ")[-1], later + 600 * (j + 1), gid, "meaning")
    # "today's standup" (the 3-hours-back one is today)
    msg("from today's standup: nothing blocked", "Sam Lee", T0, "granola:m07", "day")
    for j, (who, text) in enumerate(NEGATIVES):
        msg(text, who, T0 - 3600 * (j + 1), None, "negative")
    # A meeting's topic, ten days on: too late to be about it.
    msg("following up: the codec change rollback fixed Android jitter", "Pat Doe", T0 + 10 * DAY, None, "late")
    # Two meetings on one topic in the window: an answer here can't be sure.
    for k, hours in enumerate((10, 12)):
        start = T0 - hours * 3600
        nid = f"dup{k}"
        granola.store_note(ix, {
            "id": nid, "title": f"Status meeting {k}", "web_url": f"https://notes.granola.ai/d/{nid}",
            "summary_text": "Status of the billing migration to the new ledger; cut-over planned for November.",
            "attendees": [{"name": "Lee Park"}, {"name": ME}],
            "calendar_event": {"scheduled_start_time": dt.datetime.fromtimestamp(start, dt.UTC).isoformat()},
        })  # fmt: skip
    msg("following up from the meeting: billing migration to the new ledger, cut-over in November",
        "Lee Park", T0 - 3600 * 9, None, "ambiguous")  # fmt: skip
    return want, how


def synthetic() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "synthetic.db"
    for p in TMP.glob("synthetic.db*"):
        p.unlink()
    ix = index.Index(db, index.LocalEmbedder())
    ix._xref_settings = lambda: ([ME], xref.Allow())  # type: ignore[method-assign]
    want, how = build(ix)
    t0 = time.perf_counter()
    while ix.embed_pending(256):
        pass
    print(f"synthetic: {len(want)} messages, {ix.count('meeting')} meeting items, embedded in {time.perf_counter() - t0:.0f}s")

    # The rule by meaning alone: each message with a meeting cue, its candidates and cosines.
    rows = []
    meetings = ix._xref_meetings(T0 - 30 * DAY, T0 + 30 * DAY)
    for id, about in want.items():
        h = ix.get(id)
        assert h is not None
        if how[id] not in ("meaning", "negative", "late", "ambiguous") or not xref.MEETING_TALK.search(h.body):
            continue
        people = xref.first_names(h.who, [ME])
        cands = [m.id for m in meetings if h.ts - xref.DISCUSS_BEFORE_S <= m.start <= h.ts + xref.DISCUSS_AFTER_S
                 and people & xref.first_names(m.who, [ME])]  # fmt: skip
        mine = ix._vectors([id])
        assert mine is not None
        scored = []
        for c in cands:
            v = ix._vectors([c])
            if v is not None and len(v):
                scored.append((c, float((mine @ v.T).max())))
        rows.append((id, about, scored))
    positives = sum(1 for _, a, _ in rows if a)
    wrong = sorted((max(x[1] for x in sc), how[id]) for id, a, sc in rows if sc and (a is None or max(sc, key=lambda x: x[1])[0] != a))
    print("the best cosine of each message whose best meeting is wrong (or that has none):",
          ", ".join(f"{v:.2f} ({k})" for v, k in wrong[-8:]))
    print(f"\nby meaning only: {len(rows)} messages with a meeting cue, {positives} about a meeting")
    print("floor  margin  linked  right  precision  recall")
    for floor in [round(0.70 + 0.01 * i, 2) for i in range(21)]:
        linked = right = 0
        for _, about, scored in rows:
            got = xref.nearest(scored, floor, xref.DISCUSS_MARGIN)
            if got is not None:
                linked += 1
                right += got == about
        p = right / linked if linked else 1.0
        print(f"{floor:.2f}   {xref.DISCUSS_MARGIN:.2f}    {linked:5d}  {right:5d}  {p:9.2f}  {right / positives:6.2f}")

    # The whole pass, every rule, at the floor in xref.py.
    while ix.link_xrefs(now=T0 + 11 * DAY)[0]:
        pass
    by_how: dict[str, Counter[str]] = {}
    for id, about in want.items():
        got = [ln["id"] for ln in ix.links(id) if ln["dir"] == "out" and ln["id"].startswith("granola:")]
        c = by_how.setdefault(how[id], Counter())
        c["n"] += 1
        if got:
            c["linked"] += 1
            c["right"] += got == [about]
    print(f"\nthe whole pass (DISCUSS_MIN {xref.DISCUSS_MIN}):")
    print("case        n  linked  right")
    for k, c in by_how.items():
        print(f"{k:10s} {c['n']:3d}  {c['linked']:5d}  {c['right']:5d}")
    linked = sum(c["linked"] for c in by_how.values())
    right = sum(c["right"] for c in by_how.values())
    pos = sum(1 for a in want.values() if a)
    print(f"precision {right}/{linked} = {right / max(1, linked):.2f}; recall {right}/{pos} = {right / pos:.2f}")
    ix.close()


def live(fetch: bool = False) -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    db = TMP / "live.db"
    for p in TMP.glob("live.db*"):
        p.unlink()
    src = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    ix = index.Index(db, index.LocalEmbedder())
    print(f"live copy: {ix.count()} items ({ix.count('slack')} Slack, {ix.count('meeting')} meeting, "
          f"{ix.count('mail')} mail, {ix.count('note')} notes, {ix.count('task')} tasks)")  # fmt: skip
    c = ix._conn
    q: Any = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
    print("already there for Slack:")
    print(f"  with vectors: {q("SELECT count(DISTINCT v.id) FROM vecs v JOIN docs d ON d.id = v.id WHERE d.kind = 'slack'")}"
          f" of {ix.count('slack')}; waiting for vectors: {q("SELECT count(*) FROM pending WHERE kind = 'slack'")}")
    print(f"  with a permalink: {q("SELECT count(*) FROM docs WHERE kind = 'slack' AND json_extract(meta, '$.permalink') IS NOT NULL")}")
    print(f"  tasks found in Slack: {q("SELECT count(*) FROM docs WHERE kind = 'task' AND json_extract(meta, '$.from') = 'slack'")}, "
          f"with found_in links to a stored Slack item: {q("SELECT count(*) FROM links l JOIN docs d ON d.id = l.dst WHERE l.rel = 'found_in' AND d.kind = 'slack'")}, "
          f"with a Slack link line: {q("SELECT count(*) FROM docs WHERE kind = 'task' AND json_extract(meta, '$.link') LIKE 'https://%slack.com/%'")}")
    pending = q("SELECT count(*) FROM pending")
    if pending:
        t0 = time.perf_counter()
        while ix.embed_pending(256):
            pass
        print(f"embedded the copy's {pending} waiting items in {time.perf_counter() - t0:.0f}s")
    t0 = time.perf_counter()
    reads = 0
    while (got := ix.link_xrefs())[0]:
        reads += got[0]
    print(f"cross-reference pass: {reads} records read in {time.perf_counter() - t0:.1f}s")
    kinds = Counter()
    for src_id, dst, rel in c.execute("SELECT src, dst, rel FROM links WHERE rel IN ('mentions', 'discusses')"):
        a = src_id.split(":", 1)[0]
        b = "github" if dst.startswith("github:") else dst.split(":", 1)[0]
        kinds[(rel, a, b)] += 1
    print("links made (rel, from, to): count")
    for k, v in sorted(kinds.items()):
        print(f"  {k}: {v}")
    gh = ix.link_targets("github:")
    print(f"GitHub items named: {len(gh)} in {len({x.split('#')[0].split('@')[0] for x in gh})} repos")
    if fetch:
        from scout import config, github

        cfg = config.GithubConfig(enabled=True)
        get = github.fetcher(github.token(""))
        state = TMP / "github.json"
        state.unlink(missing_ok=True)
        asked = 0
        while (got := github.sync_pass(ix, get, cfg, state))[0]:
            asked += got[0]
        recs = ix.recent(["github"], limit=5000)
        print(f"GitHub (read-only, {asked} requests): {len(recs)} records; by state "
              f"{dict(Counter(str(h.meta.get('state')) for h in recs))}; by type "
              f"{dict(Counter(str(h.meta.get('type')) for h in recs))}; private {sum(bool(h.meta.get('private')) for h in recs)}, "
              f"public {sum(not h.meta.get('private') for h in recs)}; not given {len(github.load_state(state).get('missing') or {})}")  # fmt: skip
    ix.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="counts on a read-only copy of the live store")
    ap.add_argument("--github", action="store_true", help="with --live: fetch the GitHub items named (read-only)")
    args = ap.parse_args()
    live(args.github) if args.live else synthetic()
