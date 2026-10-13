# Slack

Scout keeps a searchable copy of your Slack messages on this Mac and reads them for
tasks, like mail. Slack stays the record. The code is `src/scout/slack.py`; the sync
runs in scout-memory, and the task farm reads new messages in the Scout app.

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
   ```

5. Restart scout-memory: `scripts/launchd.sh restart memory`.

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
every pass. A deleted message stays in the copy.

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
