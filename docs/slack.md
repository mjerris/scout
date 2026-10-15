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
markup made plain (mentions become names, links their labels). A thread is one item,
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

## Privacy

Slack text is private, like mail and meetings: `local_search` gives Claude only the
conversation's title outside open privacy mode, and nothing at all in strict mode. A
task found in Slack is shown like one found in an email (its context stays on the Mac
outside open mode; in strict mode only "a task from a Slack message").

## Tasks

The task farm (docs/tasks-design.md) reads Slack with the local model only, so the
text never leaves the Mac. Before any AI: bot messages are skipped, and so are
messages without an action cue ("can you", "please", "by Friday", "I'll", ...). A
conversation's new messages are read together, in pieces of up to 3,000 characters,
so a busy channel is a few reads, not one per message; the farm remembers the newest
message read in each conversation (`state/tasks_farm.json`). A read that fails is tried
again next pass before the rest of that conversation.

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

- "Luca messaged you on Slack."
- "Luca sent you 3 messages on Slack." (a burst: messages are gathered until eight
  seconds pass with nothing new, at most a minute)
- "Luca and Sam messaged you on Slack."

Only who, never what they wrote, in every privacy mode. Your own messages, bots,
edits, and messages in channels aren't announced; each message is announced at most
once. It's said like the pulse: it waits for the floor, so it never talks over you
or a session, and in the pulse's `quiet_hours` it's only logged.

scout-memory hears the event and queues the alert (the item's id and the name to say;
no text); the Scout app takes the queue every five seconds (`POST /v1/slack/alerts`,
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
