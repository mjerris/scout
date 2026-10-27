# Slack

Scout keeps a searchable copy of your Slack messages on this Mac and reads them for
tasks, like mail. Slack stays the record. With an app-level token it hears new
messages as they're posted, can say out loud who just DMed you, and can send a message
as you after a spoken yes. The code is `src/scout/slack.py` (the sync, and storing
events), `slack_events.py` (the live connection, in scout-memory), `slack_alerts.py`
(saying DMs, in the Scout app) and `slack_send.py` (sending); the task farm reads new
messages in the Scout app.

## Setting it up

Scout reads Slack as you, with a **user token** (`xoxp-...`) from a Slack app made in
your own workspace. It sees what you can see, and only the conversations you're in.

1. Go to https://api.slack.com/apps, choose **Create New App**, then **From scratch**.
   Name it (say "Scout") and pick your workspace.
2. Under **OAuth & Permissions**, in **User Token Scopes** (not Bot Token Scopes),
   add:
   - `channels:history`, `groups:history`, `im:history`, `mpim:history` (messages in
     public channels, private channels, DMs and group DMs)
   - `channels:read`, `groups:read`, `im:read`, `mpim:read` (the list of them)
   - `users:read` (people's names)
   - `chat:write` (sending a message as you, with `slack_send`; leave it out and
     sending fails, the rest works)
   - `search:read` is optional; Scout doesn't use it yet.
3. Choose **Install to Workspace** and allow it. Many workspaces need an admin to
   approve a new app first: Slack then says it was sent for approval, and the install
   finishes once they do.
4. Copy the **User OAuth Token** (it starts `xoxp-`) into Scout's config
   (`~/Library/Application Support/Scout/config.toml`):

   ```toml
   [slack]
   token = "xoxp-..."
   # every_min = 15       # how often to look for new messages
   # backfill_days = 14   # the first time, read each conversation this far back
   # skip = ["random", "Pat Doe"]   # channel names, or the person a DM is with
   # app_token = "xapp-..."         # live events (below); empty: polling only
   # live_every_min = 60  # the poll while live events are flowing
   # announce = ["Luca"]  # say out loud when they DM you
   ```

5. For live events (optional): under **Socket Mode**, turn it on; Slack asks for an
   **app-level token** with the scope `connections:write` (it starts `xapp-`). Under
   **Event Subscriptions**, turn events on and, in **Subscribe to events on behalf of
   users**, add `message.im`, `message.mpim`, `message.channels` and
   `message.groups`; reinstall the app when Slack asks. Put the token in
   `app_token`.
6. Restart scout-memory: `scripts/launchd.sh restart memory`, and the Scout app too
   when you set `announce` (it says the alerts).

Keep the app in your workspace (don't distribute it): Slack's tighter limits on
conversation history are for apps distributed outside the Slack Marketplace, not for
an app a workspace makes for itself. The token lives only in config.toml; it's sent
only to slack.com, never logged, never given to Claude. Removing the app (or the
token from the config) stops the sync; what was stored ages out of search like mail.

## What's stored

Every `every_min` minutes, scout-memory:

1. Lists the conversations you're a member of (`users.conversations`: public and
   private channels, DMs, group DMs). Archived ones are left out, and so are muted
   ones when Slack says which (from your preferences; best effort) and any named in
   `skip`.
2. Reads each one's messages since its own resume point (`conversations.history`),
   the newest message stored last time, less two days, so an edit or a new reply to a
   recent thread is picked up. The first time it goes back `backfill_days`.
   Joins, leaves and topic changes are left out.
3. For a message with replies, reads the thread (`conversations.replies`) when it
   changed since last time.

Each message is one item of kind `slack` (`slack:<channel>:<ts>`), titled `#channel`,
`DM with Pat Doe` or `Group DM with pat, sam`, with the senders' names and Slack's
markup made plain (mentions become names, links their labels; a labeled link's URL is
kept in the item's `urls`, for cross-references: docs/links.md). A thread is one item,
the root then its replies, split into parts of about 1,000 characters when long. Each
keeps its permalink. Names are looked up once (`users.info`) and remembered.

State is in `state/slack.json`: per conversation, the newest message stored and when
it was checked. A conversation quiet for a week is checked every two hours instead of
every pass. A deleted message stays in the copy, unless its deletion came as a live
event (below).

**Rate limits.** History and replies are Slack's Tier 3 (about 50 requests a minute), so
requests go out at most one every 1.2 seconds, and a 429 waits for its `Retry-After`
(up to a minute) and tries again. A pass over a hundred conversations takes about
two minutes.

**Only what changed, while live events flow.** Slack's conversation lists give a user
token no latest message or read mark per conversation (`users.conversations` and
`conversations.list` carry neither; `client.counts` refuses an xoxp token), so the live
events say which conversations changed: each one heard is marked in the state
(`heard`). While the events have been up without a break since a conversation was last
read, the pass reads it only when an event was heard in it since then, or its resume
point moved past that read; the rest were brought in by the events already. Once a day
(`SWEEP_EVERY_S`), and for every conversation last read before the events' last break,
the pass reads them all again, the safety net. Each pass logs its counts: "slack: N
messages (N items changed) in N s; N conversations read, N unchanged (the full sweep)".
Measured on the live workspace (2026-10-16, about 1,005 conversations, read-only into a
scratch store): a pass took 1,250 to 1,335 s reading every one; with live events, 19 s
reading 7 (983 unchanged).

## Privacy

Slack text is private, like mail and meetings: `local_search` gives Claude only the
conversation's title outside open privacy mode, and nothing at all in strict mode. A
task found in Slack is shown like one found in an email (its context stays on the Mac
outside open mode; in strict mode only "a task from a Slack message").

## Tasks

The task farm (docs/tasks-design.md) reads Slack with the local model only, so the
text never leaves the Mac. It reads only what involves you, decided before any AI
(`taskfarm.slack_involves`):

- every DM and group DM you're in;
- in a channel, a message or thread you wrote in, or one that @-mentions you or names
  you ("Mike, can you ..."). Channel chatter between other people is skipped and never
  reaches the model. Your Slack user id comes from `auth.test` (scout-memory keeps it in
  `state/slack.json` as `me`); each stored item says who wrote each line and whom it
  @-mentions by id (meta `lines`). Items stored before that go by the "Name:" labels
  and @names, matched against your Slack names and `[tasks] me`. Until your id or a
  name is known, only DMs are read. User-group mentions (`@eng`) don't count yet: they
  would need the `usergroups:read` scope.

Bot messages and messages without an action cue ("can you", "please", "by Friday",
"I'll", ...) are skipped too. A conversation's new messages are read together, in pieces
of up to 3,000 characters, so a busy channel is a few reads, not one per message; the
farm remembers the newest message read in each conversation (`state/tasks_farm.json`),
past skipped messages too. A read that fails is tried again next pass before the rest
of that conversation.

What it finds is kept only when it's your business (`taskfarm.slack_owner_ok`): in a
DM, everything; a task for you needs you addressed (an @-mention or your name on the
quoted line, or the same person's line just before it) or your own words, and in a
group DM an unaddressed ask counts as to you; a task someone else owes ("waiting on
Luca") needs you to have asked for it or been asked: the quoted line is yours or speaks
to you, or it's that person answering a line that is. "Luca, can you follow up on
ticket 61723?" "Sure" between two other people is no task of yours.

Open tasks found in Slack before this rule were checked against it once, on the first
farm pass after the update (`taskfarm.recheck_once`, state `slack_scope_checked`): one
that fails is set `dropped` with the line "Dropped: from a channel conversation that
didn't involve you" and its reminder completed, like `task_drop`. Set it open again to
keep it. A task whose Slack messages are no longer stored is left open.

## Events

With `app_token` set, scout-memory keeps a WebSocket open to Slack (Socket Mode:
Scout dials out, nothing on this Mac listens for Slack), and Slack sends each message
in your conversations as it's posted, edited or deleted. The connection's life
(`slack_events.py`):

- `apps.connections.open` with the app token gives a `wss://` URL for one
  connection; Slack's `hello` opens it (`scout status` shows `slack_events
  connected`).
- Every envelope is acked by its `envelope_id` at once, before the event is
  handled, so Slack never resends one that arrived. One it does resend (after a
  drop) is handled once, by its `event_id`.
- A connection lasts as long as `hello` says; a new one is opened two minutes
  before that, and the old one closed after the new one says hello. A `disconnect`
  (`warning`, `refresh_requested`) does the same right away. `link_disabled` means
  Socket Mode was turned off for the app: `failing`, tried again every five minutes.
- A dropped socket (the Mac slept, the network went: WebSocket pings go unanswered
  for 15 seconds) reconnects, waiting 1 second, then 2, 4, up to five minutes while
  it keeps failing (`reconnecting`; `failing` when the URL can't be had, e.g. a bad
  token).
- Stopping scout-memory closes the socket before the process ends.

Each event is stored with the poller's own code: the same item ids, titles, thread
grouping, names, markup cleanup, skip list and muted and archived rules. A new message
is its own item, or (a reply) its thread is read again (`conversations.replies`); an
edit updates its item; a deletion takes the message out of the copy, or a deleted
reply out of its thread. Joins, topic changes and Slack's own bookkeeping are left
out. Each event moves its conversation's resume point up, in the same
`state/slack.json`.

**The poll stays, as the backstop** for a sleeping Mac or a connection that's down.
While events have been flowing without a break since a pass began, the next pass
comes `live_every_min` (an hour) later instead of `every_min` (15 minutes), and
reads each conversation back ten minutes before its resume point instead of two days
(the edits and new replies came as events). When the connection drops, or an event
couldn't be stored, the 15-minute passes and the full look-back come back by
themselves.

## Alerts

`announce` names the people whose DMs Scout says out loud in the room: a Slack name
(display, real or user name; one word is a first name, so "Luca" matches "Luca
Pradovera") or a user id. A new DM or group DM from one of them makes the Scout app
say once:

- "Luca messaged you on Slack." (one message or a burst, said once)
- "Luca and Sam messaged you on Slack."
- "Slack messages from Luca, Sam, and 2 others." (three people or more)

A group DM is said as a conversation, not as messages to you:

- "Nina and Omar are talking in your group chat on Slack."
- "Nina wrote in your group chat with Omar on Slack."
- "Omar asked you something in the group chat with Nina on Slack." (a message that
  names or @-mentions you, with a question; "mentioned you" without one)

Only who, never what was written, and never how many messages. To hear what was said, ask
"Hey Scout, what are they saying?" (or "catch me up"): the `slack_catch_up` tool reads that
conversation since your last message there and Scout sums it up.

How often it speaks:

- **One conversation, once.** A DM, a group DM, or the mentions in one channel are
  gathered until `announce_quiet_s` (45) seconds pass with nothing new there, then
  said once. A conversation that keeps going is said `announce_max_wait_s` (180)
  seconds after its first message anyway.
- **One announcement at a time.** At most one Slack announcement every
  `announce_min_gap_min` (5) minutes; what settles in between waits and is merged
  into the next one. 0 turns the gap off.
- **A group DM once.** A group chat is said once while it goes on, however long; it's
  said again only after 30 minutes with nothing in it. A message there that names or
  @-mentions you is said within about 10 seconds, whatever the gap.
- **Not where you're talking.** A conversation you wrote in within
  `announce_skip_if_replied_min` (5) minutes isn't announced, and what was waiting
  from it is dropped when you write there: you've seen it. scout-memory hears your own
  messages like anyone's and queues only where you wrote, not what.
- **Not during a call or a turn.** While you're on the phone with Scout, or the room
  is mid-request, nothing is said; it's kept and said, merged, once that's over.

`announce_dms` picks which DMs: `"listed"` (the default: only the people in
`announce`), `"all"` (every DM and group DM), or `"off"`. `announce_mentions = true`
also says an @-mention of you in a channel: "Luca mentioned you in #launch on Slack."
(only a mention of you by name, not @here or @channel). A name in `announce` is said as
written there; anyone else by their first name.

Only who, never what they wrote, in every privacy mode. Your own messages, bots,
edits, and messages in channels aren't announced; each message is announced at most
once. It's said like the pulse: it waits for the floor, so it never talks over you
or a session, and in the pulse's `quiet_hours` it's only logged.

scout-memory hears the event and queues the alert (the item's id and the name to say;
no text; for your own message only the conversation and time); the Scout app takes the queue every five seconds (`POST /v1/slack/alerts`,
each alert handed out once; one not taken within ten minutes is dropped as stale).
The sentence carries the message's id, so "give me a link to that" right after opens
it in Slack (docs/links.md).

## Sending

Two tools, in the room, on the phone and in other Claude sessions (MCP):

- `slack_draft` (to, text) works out who or where: a person (a DM; their name in
  Slack, or a first name) or a channel you're in (`#launch`). More than one match
  ("Luca" when there are two) lists them, for Claude to ask which. One match makes a
  draft and sends nothing.
- `slack_send` (the draft's id) sends it, as you (`chat.postMessage` with the user
  token), and only after a yes: in the room Scout reads it back ("Send to Luca
  Pradovera on Slack: running ten minutes late?") and waits for your spoken or
  clicked yes; from another session the room is asked the same way; on the phone
  it's two steps, the read-back and then `confirm_action` after the caller's yes.
  "Yes, always" never applies to it. A draft is sent at most once and lasts half an
  hour.

People and your conversations are read once an hour (`users.list`,
`users.conversations`) to resolve names. A DM goes to your existing DM with that
person, or to their user id when there is none.
