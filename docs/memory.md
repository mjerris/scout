# Memory, notes, search and sync

What Scout keeps: facts you tell it, behavior rules, notes and lists, a journal of every
exchange, and a local search index over those plus mail, meetings and Slack, all in
scout-memory, a local service of its own.

## Facts

Tell Scout things to keep: "remember that my dentist is Dr. Lee", "note that
Pat is my manager". Ask "who's my dentist", "what do you know about Pat", "what
do you remember", or "forget my dentist". Plain code answers these, and the
facts live in `memory/facts.md` in Scout's data folder, one `- fact · date` line
each: edit the file and Scout picks the change up. Claude sees a memory
only when it shares words with the request (and not in strict mode); the room
agent can also `remember`, `forget` and `recall`.

## Behavior rules

**Behavior rules** are how you want Scout to behave, not facts: "from now on, call me
Mike", "always read ticket numbers digit by digit". Scout reads the rule back ("Rule:
call you Mike. Keep it?") and keeps it on a yes, in `memory/behavior.md`; every session
(the room, the phone, desk sessions) gets them. Each night the local model drafts rules
from the day's corrections for the morning briefing to offer; "what are my rules" and
"review my rules" list and tidy them; `scoutctl.sh rules` adds one by hand. See
[behavior-rules.md](behavior-rules.md).

## Notes and lists

Notes are markdown files in Scout's data folder, `memory/notes/0003-generator-oil.md`
(the number is the note's id), searchable by words and meaning ("take a note: ...",
"what are my notes about the generator"). Edit or add a file and search follows within
a minute. There's a copy in an Apple Notes folder
called Scout when `[notes] apple = true` and you allow Notes access (macOS asks once,
the first time a note is saved). Lists and tasks use the Mac's Reminders; when that
isn't available (access refused, or not a Mac) Scout keeps them itself, with the same
answers and tools. `[lists] backend` picks `auto`, `reminders` or `scout`.

## Local search (scout-memory)

Memories and the search index live in a separate local service, **scout-memory**
(its own login item, `com.local.scout.memory`, at low CPU priority). It owns
`state/memory.db`, the embedding model and your remembered facts, and serves them
over HTTP on 127.0.0.1:8767 to Scout and any other local program holding the token
in `state/memory.token`: `/v1/docs/*` (put, delete, sync, get, count, recent,
prune), `/v1/search` and `/v1/memories/*` (add, forget, matching, relevant, newest,
list), `/v1/journal/add`, and the links between records: `GET /v1/resolve?id=`
(a record, where it opens, and what it's linked to) and `/v1/links/*`. A task links to
the meeting, email or Slack message it was found in, an emailed meeting summary to the
Granola note of the same meeting, a journal entry to the records it was about; each
record opens where it lives (notes.granola.ai, Slack, `message://` in Mail) or, for
Scout's own, on its page on the web UI (`/r/<id>`, reachable from the phone over
Tailscale). See [links.md](links.md). It keeps running while the voice app restarts;
`scripts/launchd.sh status memory` shows it.

**The journal.** Every exchange with Scout, in the room or by phone, is written to a
markdown file per day: `memory/journal/2026/10/2026-10-13.md` in Scout's data folder.
Each entry has the question, the answer and the tools used with their arguments (a
mail id, a place): what they returned isn't copied, and an answer built from mail or
messages is kept only in open privacy mode. The files are the record: edit or delete
an entry and search follows within a minute. A phone call is written when it ends.

**Search.** The index (`state/memory.db`) holds the journal, your facts, notes, lists,
mail and Granola meetings. A search ranks by meaning over everything of the kinds and
dates asked for, with a local embedding model (bge-small-en-v1.5, CPU, a few hundred MB in
memory), and full text (SQLite FTS5) adds a little for shared words and finds what has
no vector yet. An idle pass embeds new items ahead of time. A long item is embedded in
windows of about 1,000 characters; one with markdown headings (a meeting summary, a
note) is cut at its headings, each window led by the title and the section's heading. On a synthetic year of
20,000 conversations and 50 questions it finds the answer in the top 5 for about
three in four, and in the top 20 for 84%. Claude gets the best 12 matches through the
`local_search` tool. `scripts/eval_retrieval.py` measures search alone (recall@k, MRR,
nDCG@10, numbers only) on that year, on long meeting summaries, on the recall bench's
store, and on pairs taken from a copy of your own store: a task's title should find the
meeting or mail it came from, and a journal turn's question the records its refs line
names. `--floor` shows how close right and wrong records get, which is what
`claude.recall_min_similarity` is set from.
The inbox is indexed too, lazily: while Scout is idle, a pass every 20 minutes reads
mail that arrived since the newest indexed message (Mail.app, the access Scout already
has). Older mail joins the index only when Scout touches it for a task: a Mail search
result (sender and subject, its text filled in on a later pass) or a message read or
summarized. Mail searches use the index first, by words and meaning, and fall back to
asking Mail. Mail text never reaches Claude through search outside open privacy mode. `[index]` in
config.toml tunes it; `semantic = false` keeps it to full text only.

**Granola.** With a Granola API key in `[granola] api_key` (Granola: Settings,
Connectors, API keys; a Business plan or above), scout-memory brings your meeting
notes into the index every 30 minutes: the summary and the transcript, in chunks with
each speaker named. Granola stays the record. Like mail, a meeting's text reaches
Claude only in open privacy mode; otherwise Claude sees the title.

**Slack.** With a Slack user token (`xoxp-...`) in `[slack] token`, scout-memory brings
in the messages of every conversation you're in (channels, private channels, DMs and
group DMs; archived and muted ones left out, and any named in `[slack] skip`) every
15 minutes, each from where it left off (the last two weeks, the first time). A
thread is one item with its replies. The task farm reads a conversation's new
messages together with the local model, so Slack text stays on the Mac; it reads only
DMs, group DMs and channel messages or threads you wrote in or are mentioned in, and
bot messages and messages without an action cue are skipped. Like mail, Slack text reaches Claude
only in open privacy mode. With an app-level token (`xapp-...`) in `[slack] app_token`,
messages also arrive live over Socket Mode (the poll then runs hourly as the backstop),
a DM from someone in `[slack] announce` is said out loud ("Luca messaged you on
Slack.", who and never what), and `slack_draft` / `slack_send` send a message as you
after your spoken yes to the read-back. Making the app and its tokens:
[slack.md](slack.md).

## Sync status: "is everything syncing?"

Each sync records every pass in `state/sync_status.json`: Mail, meeting notes by
email, Granola, Slack, GitHub, the task farm, Reminders mirroring, the vectors for
search by meaning, and link upkeep (with the records still to read for cross-references). It keeps when the pass ran, whether it worked, what it added
or updated, and what's still waiting where that's known. An error keeps only its type
and a short message with quoted text, links and addresses taken out.

Each source is ok, failing (its last pass failed), stalled (no good pass in 3 times its
interval, 30 minutes at least), off (not set up) or waiting (no pass yet).
`scripts/scoutctl.sh status` prints a line for each, scout-memory serves them at
`GET /v1/sync`, and "is everything syncing?" or "what's the sync status?" gets a spoken
answer. Messages has no sync to report: Scout reads it live.

Syncs heal themselves first, in plain code. A pass that waits for a quiet room (the task
farm, Reminders mirroring, the mail index) runs anyway once it's 3 intervals late, in the
first few seconds with nobody speaking or being listened to; a failed pass is retried
after 1, 2, 4... minutes; a stalled source's loop is woken, silently. The pulse speaks
(once a day per source) only when that didn't work or you have to act, and says what to
do: "Reminders mirroring needs you. Turn on Reminders access for Scout in System
Settings...", "Mail sync has been failing since 9 this morning, and retrying hasn't
fixed it...", or a stall still there 10 minutes after its loop was woken. A Messages
helper that stops answering is restarted before you're told. `[sync]` tunes it.

## Links: "give me a link to that"

Everything Scout says keeps the ids of the records it was about: the tasks the pulse
named, the note a recall answer quoted, the tasks and emails a Claude turn looked up.
Right after, "give me a link to that" (or "send me that link") puts the record's link,
such as the Granola meeting a task was found in, into your Scout reminders list, and
"where did that come from" says which meeting or email it was. Claude gets the same
ids in its request context and the `open_link` and `send_link` tools (the room, the
phone and other sessions). Details and the privacy rules: [links.md](links.md).

**Cross-references.** The store links what points at what, both ways: a Slack message
or email that pastes a Granola link, a Slack permalink or a `message://` link, names a
meeting by its title ("per the vendor sync", "in today's standup"), or follows up on one
(matched by meaning, with someone in common, only when it's clear), and a meeting whose
notes name a Slack thread. GitHub issues, PRs and commits named anywhere become records
with their state (`[github] enabled = true`; the token is the `gh` CLI's): "what's the
status of PR 117", "what GitHub issues am I waiting on", and a task like "review #117"
opens the PR and is done when it merges. Rules and measurements:
[links.md](links.md#store).
