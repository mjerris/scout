# scout

An always-on voice front-end for a Claude Code agent on a Mac mini. Speech
recognition and speech synthesis run locally on Apple Silicon; only the request
text goes to Claude.

```
mic ─┐                                                    ┌→ Silero VAD + smart-turn → MLX Whisper → gate → "hey Claude …" → Claude
     ├─ voice layer: one duplex engine, echo cancelled ───┤
spk ←┘   (WebRTC AEC3, or Apple voice processing)         └← Kokoro TTS ← markdown → speech ←────────────────────────────────┘
```

- **Voice layer:** one audio engine owns the mic and the speakers, so the
  echo canceller knows exactly what was played and removes the assistant's
  own voice before Whisper hears it. `audio.backend`: `auto` (= `webrtc`,
  WebRTC AEC3 + noise suppression; cross-platform), `apple` (macOS voice
  processing in a small Swift helper, `scripts/build-voiceio.sh`), or `plain`
  (no canceller; the app then strips its own words from transcripts
  instead). Measured on the Mac mini with `python -m scout.measure`:
  WebRTC removed 27.7 dB of the assistant's voice (Whisper could recover 2%
  of its words, vs 97% with no canceller); Apple 15 dB / 6%, with ~100 ms
  more latency, so WebRTC is the default.
- **Barge-in:** with a canceller, talking over the assistant stops it. Over
  a reply, what you say becomes the follow-up; over a desk session's
  `discuss` message, it becomes the session's answer. `audio.barge_in`.
- **End of turn:** Silero VAD finds speech; after a short pause the
  smart-turn model judges from how you sounded whether you're finished. A
  finished sentence ends the turn ~0.2 s after you stop; "…, um" waits up to
  `audio.max_pause_ms` (1.5 s). `audio.turn_detection = "simple"` goes back
  to a fixed silence (`audio.silence_ms`).

- **Wake word:** say "Hey Scout, …" (or just "Scout, …") with the request in
  the same breath, or say "Hey Scout", wait for the chime, then speak. For 8
  seconds after a reply the room heard you can follow up without the wake word
  (replies that only went to your phone open no such window).
- **Stop:** "Scout, stop" (also "Stop, Claude", "stop, stop", "that's enough")
  interrupts speech and the running request, cancels a pending question, ends a
  session's listen or floor hold, and drops anything queued. "Claude, new
  conversation" starts a fresh session.
- **Busy:** a "Hey Scout …" request while one is running is queued ("Okay, I'll
  do that next.") and runs when the current one ends.
- **Local gate:** every transcript passes deterministic checks before it can
  reach Claude, so noise and hallucinations cost no tokens. The checks use
  Whisper's confidence and no-speech estimate, repetition, words per second
  of actual speech, a list of known noise phrases, and loudness. Follow-ups
  without the wake word are held to a stricter standard: at least two words,
  well above the room's noise floor, and not much quieter than your last
  "Hey Scout". Rejections are logged as `ignored ... (reason)` and shown
  struck through on the web page. Thresholds are under `[gate]` in the config.
- **Permissions:** `claude.approval_policy` picks who decides what runs
  without asking:
  - `strict` (default): read-only tools and a short list of harmless commands
    (`date`, `ls`, ...) run on their own; everything else is asked out loud.
  - `settings`: your `~/.claude` allow rules and hooks apply; you're asked
    only where Claude Code would normally prompt.
  - `settings_no_hooks`: your allow rules apply but your hooks are off, since a
    hook that answers "allow" would skip every prompt.

  Under every policy: **shell commands** always ask (except the harmless list
  and exact commands you've said "yes, always" to; your `~/.claude` Bash allow
  rules don't apply to the voice agent), **fetching a page or opening a URL**
  asks once per site, **secrets** (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`,
  `~/.config/gh`, keychains, this app's `state/`) can't be read at all, and
  this app's own files can't be changed. `claude.always_ask` rules (git push,
  PR merge/create) are always asked, even with a saved rule, in any spelling
  (`git -C ~/x push`, `env git push`).

  Spoken prompts are short ("Run git push?", "Edit config.py in src?"); the web
  page shows the full command first. Answers are strict: only a clear yes or no
  counts ("yes", "sure, go ahead", "no", "don't"); anything unclear ("okay, hold
  on", "what does it do?", "I'm not sure") gets "yes or no?" again. A question
  asked only on your phone can't be answered by the room's mic. No answer within
  30 s counts as a skip.
- **Built-in tools:** search Netflix, YouTube, Google, Amazon, Wikipedia or
  Maps; open an app; see which app is in front and which are open; list and
  switch Chrome tabs; full screen on/off; play/pause/next/previous (Spotify or
  Music, or the video in the front browser); volume get/set/up/down/mute;
  timers that announce themselves; web search. These never ask. Opening a URL
  or a new tab asks once per site. Each tool's AppleScript is fixed in
  `mac.py`, and your words are passed in only as checked arguments, so these
  can't be used to run arbitrary scripts. Timers live in memory and are lost
  on restart.
- **Offline reference:** Wikipedia, WikiMed, iFixit and other Kiwix archives on
  the Mac, searched in place: `reference_search` and `reference_read` for Claude,
  and with the internet down Scout answers "look up X" from them itself, with a
  link to the article. [docs/reference.md](docs/reference.md).
- **"Yes, always":** answer a prompt with "yes, always" (or "yes, don't ask me
  again", or tap **Always** on the web page) and it stops asking for that exact
  shell command, that site (for page fetches and URLs), or that tool (for MCP
  tools that only read). File edits, writes, always-ask commands and MCP tools
  that send, post, create or delete always ask. "Yes, but always ask me" is a
  one-time yes. Saved rules are in `state/voice_allow.json` and can be removed
  from the web page.
- **"Hang on":** end with "hang on", "give me a sec" or ", wait" and it keeps
  listening (20 s) and joins what you say next onto the request ("tell them not
  to wait" is an ordinary request). Saying "never mind" or "stop" instead
  cancels it.
- **Working sounds:** a soft tick every few seconds while tools run and nothing
  is being said, so a quiet stretch doesn't sound like a hang. When a lookup
  (Claude's or a local one like the weather or a route) has said nothing for
  2 s, it says what it's doing ("Checking your mail.", "Getting the weather.",
  "One moment."), and "Still working on it." 12 s later if it's still going.
  Turn it off with `tts.progress_speech = false`; the timing is
  `tts.progress_delay_s` and `tts.progress_again_s` (0 = no second line).
- **Pronunciation:** regex rules fix how words are spoken ("config.toml" →
  "config dot toml") and common mishearings ("Claud" → "Claude"). Built-in
  defaults are in `src/scout/pronounce_default.txt`; add your own in
  `pronounce.txt` in the project root (`TTS|STT  pattern  replacement`).
- **Changing the app by voice:** the voice agent can't edit this project. When
  you ask it to change how it listens, talks or asks, it calls its
  `request_app_change` tool, which appends to `state/change-requests.jsonl`
  and logs `change request: ...` for the Claude session that maintains the app.
- **Remote Control:** set `claude.remote_control = "<name>"` to start the voice
  session with Remote Control enabled.
- **Web page:** open `http://<mini-ip>:8765/?token=<token>` (the token is in
  `state/web_token`; the log doesn't print it). The page drops the token from
  the address bar and keeps a cookie. It shows a live transcript and lets you
  type requests, approve or deny actions, stop, mute the mic and reset the
  session. Delete `state/web_token` and restart the app to rotate the token.
  Over the LAN address the page is plain HTTP; use the Tailscale HTTPS address
  (below) on networks you don't trust, or drop "lan" from `web.hosts`.

## Voice for your other Claude Code sessions (MCP)

The app also runs an MCP server, so any Claude Code session (at your desk,
in a terminal) can talk out loud through the same mic, voice and filtering:

```sh
claude mcp add --scope user voice -- uv run --project ~/src/scout python -m scout.mcp_server
```

- `discuss(message, wait_for_response=True, listen_timeout=30, hold_floor=False,
  wait_for_floor=15, voice="")` speaks `message` and returns what you say back.
  No wake word is needed, and the reply passes the same local gate as
  everything else.
- `voice_status()` shows who has the floor and which voices are available.

**The floor (conch):** one speaker at a time. The room assistant and each
session take the floor for an exchange; `hold_floor` keeps it for a few
seconds so a back-and-forth isn't interrupted, and others queue in order. A
"Hey Scout" request waits up to 15 s for a session to finish. "Scout, stop"
always works. The web page shows who has the floor.

## Phone and other devices (Tailscale)

```sh
scripts/tailscale.sh enable     # https://<this-mac>.<tailnet>.ts.net, tailnet-only
scripts/tailscale.sh disable | status
```

Over HTTPS the web page can use the device's microphone: **Hold to talk**
sends a clip through the same gate (no wake word needed), and **play here**
plays replies on that device. Install Tailscale on your phone, sign in to the
same tailnet, and open the printed link. The first time, Tailscale asks you
to enable Serve for the tailnet.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`brew install ffmpeg`;
push-to-talk decodes browser recordings with it).

```sh
scripts/fetch-models.sh     # Kokoro voice (~350 MB), Silero VAD, smart-turn; Whisper (~1.6 GB) downloads on first run
scripts/build-voiceio.sh    # optional: the Apple voice-processing helper (audio.backend = "apple")
scripts/build-vcal.sh       # optional: calendar access (EventKit helper; see Calendar below)
scripts/build-messages.sh   # optional: read-only Messages history (see Messages below)
scripts/messages-agent.sh install   # ...and its own login item
cp config.example.toml "$HOME/Library/Application Support/Scout/config.toml"   # optional; every key has a default
scripts/run.sh              # foreground run; allow microphone access when macOS asks
```

Scout keeps everything that must survive an update in its data folder,
`~/Library/Application Support/Scout/` (set `SCOUT_HOME` to move it):
`config.toml`, `pronounce.txt`, `state/` (saved rules, the web token),
`logs/`, `models/` and the compiled helpers in `bin/`. The paths below
(`state/…`, `logs/…`) are inside it. Moving from a claude-voice checkout:
`scripts/migrate-claude-voice.sh ~/src/claude-voice`.

Claude uses the account the `claude` CLI is logged in with. Run `claude` once
first if you have never logged in.

## Install as a Claude Code plugin

Scout is one plugin: the background app, its MCP server (voice, mail, calendar)
and its skills. Installing it is all the setup there is.

```sh
claude plugin marketplace add mjerris/claude-plugins   # once: Michael Jerris's plugins
claude plugin install scout@mjerris
```

The next Claude session's start runs the plugin's SessionStart hook
(`scripts/ensure-running.sh`). When Scout isn't installed, or this plugin version
isn't the one running, it runs `scripts/install.sh` in the background: it links the
version into the data folder, syncs the Python environment, fetches the models,
builds the native helpers when their source changed, and installs and starts the
`com.local.scout` login item. After that the hook returns in milliseconds, and the
app runs whether or not any Claude session is open. `/scout:status` shows progress
(the first setup takes a few minutes); `claude plugin update scout` brings a new
version, which the next session start switches to.

- Desk sessions get Scout's tools as `mcp__plugin_scout_scout__*` (discuss,
  voice_status, mail, calendar). The room assistant has its own in-process
  copies and loads Scout's skills (`claude.plugins = "scout"`); `"enabled"` adds
  every plugin enabled in `~/.claude/settings.json`, whose tools ask by voice unless
  pre-approved.
- `/scout:uninstall` stops the app and removes the login item, keeping the data
  folder unless asked to purge; then `claude plugin uninstall scout`.
- Working from a checkout instead: `claude --plugin-dir ~/src/scout` loads it for
  one session, and `scripts/install.sh` installs the background app from it by hand.

## Always on

```sh
scripts/launchd.sh install    # start at login, restart if it exits
scripts/launchd.sh status | restart | uninstall
```

- The Mac must be logged in, so a LaunchAgent has an audio session (System
  Settings → Users & Groups → automatic login).
- With `audio.backend = "apple"`, the helper needs microphone access too; the
  first start under launchd may prompt for it.
- `run.sh` holds a `caffeinate` assertion, so the mini won't idle-sleep while
  the assistant runs.
- If the microphone stops delivering audio (unplugged, device changed) or
  playback keeps failing, the app exits so launchd restarts it.
- Under launchd, macOS attributes mic access to the process itself rather than
  your terminal. If the log says *microphone has delivered pure silence*, enable
  it under System Settings → Privacy & Security → Microphone.

Logs: `logs/scout.log` (plus `logs/launchd.*.log` under launchd). Each
heard line includes the stats the gate used, e.g. `[conf -0.31, no-speech 0.01,
2.4 words/s, 24 dB over noise]`.

Everything said and heard is also written to
`logs/transcript.jsonl`, one event per line (rotated at midnight, 30 days kept). Set
`audio.save_utterances = 50` to keep the last 50 clips (wav + transcript +
stats) in `state/utterances/` for tuning; off by default.

Checks: `bash scripts/run-ci.sh` runs LINT (ruff), FMT (ruff format; applied
locally, checked in CI), TYPES (mypy strict), SHELL (shellcheck) and TEST
(pytest, including a few hundred real-world phrases in `tests/data/phrases.json`),
the same script GitHub Actions runs. `scripts/install-hooks.sh` adds a pre-commit hook
for lint, format and types.

## Tuning

`uv run scout --list-devices` lists audio devices for `[audio]
input_device` and `output_device`. Other useful knobs in `config.toml`:

| Key | Effect |
|---|---|
| `claude.model = "sonnet"` | faster, cheaper replies |
| `claude.cwd` | the directory the agent works in |
| `asr.model = "mlx-community/whisper-small-mlx"` | less RAM and faster, slightly less accurate |
| `tts.voice` | e.g. `af_bella`, `am_michael`, `bf_emma`, `bm_george` |
| `audio.silence_ms` | how long a pause ends your sentence |
| `wake.follow_up_seconds = 0` | always require the wake word |

Without headphones the mic hears the assistant too. Speech that starts while it
is talking is ignored, except "Scout, stop".

## Mail, calendar and reminders for every session

The mail, calendar and reminders tools are defined once (`shared_tools.py`) and offered
both to the room assistant and, through the MCP server, to any other Claude
Code session, as `mcp__scout__calendar_events`, `mcp__scout__mail_recent`, and
so on. The running app always does the work: other sessions' calls go to it
(`POST /api/tool`), so macOS's calendar and Mail permissions belong to the app
alone. Sending mail, adding events, and adding or completing reminders are
confirmed by voice in the room on every call ("From myproject: Send email to sam@example.com, subject Lunch?"),
whatever the calling session's own permission settings say.

How to use them (which tool, untrusted email, how to say results out loud) is
the `mail-calendar` skill in `skills/`; `scripts/install-skills.sh` links it
into `~/.claude/skills` for the room and every other session.

## Calendar

The assistant reads the Mac's own calendar store, so any account synced to
this Mac works: add Google (or iCloud, Exchange) in System Settings → Internet
Accounts with Calendars on. macOS holds the account credentials; the app never
sees a token.

- `scripts/build-vcal.sh` builds the helper (`native/vcal`, EventKit).
- The first calendar question shows a macOS "allow calendar access" prompt on
  the Mac's screen. Allow it once; it's listed afterwards under System Settings →
  Privacy & Security → Calendars.
- Reading (`calendar_events`, `calendar_list`) runs without asking. Adding an
  event (`calendar_create_event`) is confirmed by voice every time ("Add
  Dentist, Friday October 9, 3:00 PM, to Home?"); "yes, always" doesn't apply.
  There is no edit or delete.
- Free time is worked out in plain code, no model: `calendar_free` (start, end,
  min_minutes) lists each day's free gaps and busy blocks inside working hours
  (`[calendar] work_start`/`work_end`, default 9:00 to 17:00). Overlapping
  events merge; all-day events and events shown as free don't block time.
- Answered instantly without Claude (tier 0): "am I free at 3 / tomorrow at 3 /
  Thursday at 3pm" (a time without am or pm from 1 to 6 means the afternoon),
  "when am I free tomorrow", "what's my first free hour tomorrow", "how busy is
  my day / tomorrow".

## Reminders

Reminders come from the Mac's Reminders store through the same helper, so
iCloud and other synced lists work.

- Reminders have their own macOS permission: the first reminders question
  shows an "allow access to Reminders" prompt on the Mac's screen (System
  Settings → Privacy & Security → Reminders). Rebuild the helper
  (`scripts/build-vcal.sh`) first so it carries the reminders usage text.
- Reading (`reminders_list`: open reminders, due ones first, by `list`, with
  `include_completed` or `due_before`; `reminder_lists`) runs without asking.
  Adding (`reminder_add`: title, list, due, notes) and completing
  (`reminder_complete`: id and title, which must match, so a stale id can't
  finish the wrong one) are confirmed by voice every time ("Add milk to
  Shopping?", "Mark milk done?"); "yes, always" doesn't apply. No delete.
- A list name matches ignoring case, spaces, punctuation and a trailing "list":
  "shopping list" finds Shopping, "to-do" finds To Do.
- Tier 0 only reads ("what's on my shopping list", "what are my reminders
  today", "what's due tomorrow"). "Add milk to my shopping list" goes to Claude
  on purpose: its `reminder_add` call is the one that asks you first.

## Briefing

"Brief me", "morning briefing" or "what's my day look like" gets one short
spoken summary, built in plain code: today's calendar (how many events, the
next one, the longest free stretch), reminders due today or overdue, and how
many unread emails there are (from the inbox's last 30 days) and who they're
mostly from. No subjects or message text are read. A source that isn't
available is named ("I couldn't check mail.") and the rest still plays.

Set `[briefing] at = "07:30"` to hear it every day at that time; it waits its
turn for the floor like a timer does.

## Mail

Mail.app does the work, so any account in Mail works (add Google in System
Settings → Internet Accounts with Mail on, or in Mail itself). The app drives
Mail with JavaScript for Automation; the first use shows a "control Mail"
prompt on the Mac's screen (System Settings → Privacy & Security → Automation).

- Reading (`mail_recent`, `mail_search`, `mail_read`, `mail_read_full`) and
  drafting (`mail_draft`: opens a draft in Mail, sends nothing) run without
  asking. What the reads give Claude depends on the privacy mode (below).
- Sending (`mail_send`) is confirmed by voice every time ("Send email to
  sam@example.com, subject Lunch?"), with the full message on the web page;
  "yes, always" doesn't apply. No attachments, forwarding or deleting.
- Email is written by other people. Message text reaches the agent marked as
  untrusted, and the agent is told never to act on instructions inside it;
  anything that sends, opens or runs something still needs a spoken yes.
- Finding mail (newest, unread, by sender or subject) is fast once
  `scout-messages` (below) has Full Disk Access: it reads Mail's own index,
  `~/Library/Mail/V<N>/MailData/Envelope Index`, read-only, envelopes only
  (sender, subject, date, read, mailbox; never message text), in the inbox of
  each account. Without it Scout scripts Mail, which has to fetch every inbox
  message's date first (7 s and more on a 60,000-message inbox). It's the same
  single grant as Messages, to the same helper; nothing else gets it. Message
  text still comes from Mail, by id, and each message read for an index row is
  checked against that row (subject, sender, time received): if Mail's id ever
  meant another message, Scout finds the right one by subject and date, logs
  `MAIL INDEX ID MISMATCH`, and goes back to scripting Mail for the session.
  `mail_mac.verify_index_ids()` compares the two directly (ids and counts only).

## Weather

A built-in `weather` tool (Open-Meteo: free, no API key, nothing to sign in to): current
conditions, 24 hourly steps and 7 days, with wind speed, gusts and direction. Everyday
questions ("what's the weather in Luther, Michigan", "... tonight / tomorrow / in the
morning", "what's the wind forecast for tonight", "will it rain tomorrow") are answered
locally without Claude. `[weather] home` sets the place used when none is named.

## Maps

Nearby places and driving distances from OpenStreetMap (free, no key, no sign-in):
"where's the nearest dump station near Cadillac", "how far is Traverse City from
Luther" are answered locally; Claude gets `places_near` and `drive_route`. Searches are
cached for a day; the public map server is sometimes busy, and Scout says so. Drive
times are for a car: they don't know an RV's height or weight.

## Starlink

Off unless turned on; it reads the dish's own local API (no account, no sign-in):

```toml
[starlink]
enabled = true
quiet_hours = "22:00-08:00"   # announcements are only logged in these hours
```

"Is Starlink down?", "why is the internet so slow?" and "how much data have we used
today?" are answered locally. Scout announces outages longer than a minute, when it's
back, and new dish alerts; it tallies data used per day. Claude and other sessions get
the read-only `starlink` tool with exact values.

## Phone calls (SignalWire)

Off unless turned on (design: [docs/phone-design.md](docs/phone-design.md)). A paired
phone app calls a SignalWire AI agent; SignalWire does the speech and the conversation,
and the Mac only receives signed webhooks through Tailscale Funnel. **What Scout says on
the phone passes through SignalWire's speech recognition, model and text-to-speech**; the
privacy mode applies as it does at home.

```toml
[phone]
enabled = true
signing_key = "<signing key>"                        # SignalWire Dashboard, API Credentials
public_url = "https://<mac>.<tailnet>.ts.net:8443"   # the Funnel base
address = "<the agent's address>"                    # what the app dials
```

`scout-phone` (its own login item, `com.local.scout.phone`, started by the installer
only when `enabled = true`; `scripts/run-phone.sh` runs it in the foreground) listens on
127.0.0.1:8768. It refuses to start without `signing_key` and `public_url`. On a call it
offers the read-only shared tools (calendar, reminders, notes, mail, texts, weather,
places, routes, Starlink, local search) and `ask_scout`, which answers through Scout's
own tiers (local intents, memory, the local model with its recall from Scout's records,
Claude on a session of its own) and
returns text: nothing is spoken in the room and a room conversation is left alone.
Actions (adding a reminder or calendar event, completing a reminder, deleting a note) take
two steps: the tool says what it would do and gives a one-time code, the agent reads it
back, and `confirm_action` does it only if a SignalWire semantic gate (a separate
decision model reading the conversation) finds that you clearly said yes; a yes read out
of an email or message doesn't count. Email is never sent from the phone.

Pairing, at home:

```sh
scripts/scoutctl.sh phone pair "My iPhone"   # QR code + JSON with the phone's secret
scripts/scoutctl.sh phone list
scripts/scoutctl.sh phone forget <device>    # a lost phone stops working at once
```

Each call carries `userVariables = {device, ts, nonce, sig}` with
`sig = hex(HMAC-SHA256(secret, "device|ts|nonce|address"))` (the secret base64url-decoded
to its 32 bytes, `ts` in Unix seconds). A call whose device is unknown or forgotten, whose
timestamp is more than `call_skew_s` off, whose nonce was used before or whose signature
doesn't match gets no tools: it hears a short goodbye and is hung up. Every tool call is
checked again against its call. Basic-auth credentials and the SWAIG token secret are
kept in `state/phone.auth` and `state/phone.secret`, the paired phones in
`state/phone_devices.json` (all mode 600).

### Test call from a browser

`/phone` on the web UI (tailnet only, never through Funnel) places a verified call from
the browser. Besides `address` it needs `space`, `project_id` and `api_token` in
`[phone]`: Scout mints a one-hour SignalWire subscriber token for the page
(`POST /api/fabric/subscribers/tokens` as subscriber `[phone] subscriber`, default
`scout-owner`; SignalWire creates that subscriber the first time, and bills subscribers
monthly).

1. Open `https://<mac>.<tailnet>.ts.net/phone?token=<the contents of state/web_token>`
   (HTTPS, so the page may use the microphone).
2. **Pair this browser** with a name. The secret stays in this browser's storage;
   `scoutctl.sh phone forget <device>` revokes it.
3. **Call Scout**. The page loads `@signalwire/js` 4.0.0-rc.4 from jsDelivr and dials
   `address` with signed `userVariables`, as the app will.

## Messages (iMessage and SMS)

Scout can read your texts, never send them. Messages keeps its history in
`~/Library/Messages/chat.db`, which macOS guards with Full Disk Access. That
permission goes to one small helper, `bin/scout-messages` (`native/messages`),
not to Scout's Python, Claude or a terminal:

- It runs as its own login item, `com.local.scout.messages`, so macOS checks the
  permission against the helper itself. The installer builds it (only when its
  source changed: a rebuilt helper is a new program to macOS and needs the
  switch again) and `scripts/scoutctl.sh uninstall` removes it.
- It opens the database read-only and answers a few fixed questions on a Unix
  socket in `state/` (owner-only, with a fresh random token in
  `state/messages_token` on every start): the newest messages, messages from one
  person, unread messages, one conversation, and a text search. At most 50
  messages and 365 days per answer, 60 lookups a minute, message text only (no
  attachments), no sending, no SQL from callers.
- It also answers three questions about Mail's index (`mail_status`,
  `mail_recent`, `mail_search`; see Mail above), under the same token, limits
  and log. Rebuilding the helper for a source change needs the switch again for
  both.
- Every lookup is logged to `logs/messages-access.log` (what kind, how many,
  which program asked; never message text or search words).
- Names: it maps phone numbers and addresses to names from Contacts. macOS asks
  once, on the first real lookup; without it you see numbers.

The tools (`messages_recent`, `messages_from`, `messages_unread`,
`messages_search`) are read-only and run without asking, for the room and other
sessions alike. Text messages are written by other people and reach the agent
marked as untrusted, like email. `messages.enabled = false` in `config.toml`
turns them off.

**First use.** Full Disk Access has no "allow" prompt, so the first messages
question sets it up: Scout opens System Settings at Privacy & Security → Full
Disk Access and a Finder window with `scout-messages` selected, and says what to
do. Drag `scout-messages` into the list (or click +, press Cmd-Shift-G and paste
`~/Library/Application Support/Scout/bin/scout-messages`), turn it on, and
Scout says "Got it" a few seconds later. To take it back, turn it off or remove
it in the same list.

## Trust: what asks first

Everything that changes something asks by voice first. One action at a time can earn
"just do it": answer a confirmation with "yes, always", or say "always allow adding
reminders"; "ask me before adding reminders" takes it back, and "what do you do
without asking" lists them. Adding and completing reminders, adding calendar events
and deleting notes can be promoted; sending email always asks. After five yeses in a
row Scout mentions the option once. Every decision is logged in `state/trust.json`.

## Answered on the Mac (tier 1)

Before Claude, a small local model (`claude.local_model`, Qwen3 4B on MLX) gets one
chance at a request plain code didn't answer. It only picks a read-only lookup or
"Claude", and plain code decides first: anything with an action word (send, reply,
add, change, order...), judgment ("should", "why", "what do you think"), or more than
one step or sentence ("...and text it to Carol", "...? Reply that it's fine", "... - do
it") goes straight to Claude without asking the model. A verb asked about in the past is
a question, not an action: "when did I last change the oil" and "what did Pat tell me"
pass; "did you send it" (Scout's own doing) goes to Claude.

What it answers: the calendar, the inbox (what an email says is summarized on the Mac),
and with `claude.local_recall` (on by default) the weather, the task list, and
**recall**: questions about what's in Scout's records: "what did Pat say about the Q4
budget", "when did I last change the generator oil", "what was decided in the vendor
meeting", "what's my wifi password". A recall searches the local store (below) by the
question and by the model's few words, shows the model the best five records, and takes
its answer only if it names one record, quotes it word for word, and passes checks in
plain code. A record found by meaning is shown only if it's close enough
(`claude.recall_min_similarity`, 0.64); with none that close the model isn't asked and
Claude answers. The answer's refs (what "send me a link to that" opens) are the one
record it quoted, never the others it was shown. The checks: the record has the question's topic words (a note about the truck's tires
doesn't answer "when did I rotate the RV tires"), every number in the answer is in the
record, and nearly every other word too. Anything else, including "not found", goes to
Claude, which still has the request's context. Emails that address an AI or are lures
are never shown to the model. An answer that read mail, a meeting or Slack is private
like other local answers (withheld from Claude in strict mode). The phone's "ask Scout"
goes through the same tiers, so these are answered on the Mac by phone too.

Measured with `scripts/bench_recall.py`, on a synthetic store (21 records, one a lure,
among 300 distractors) and 100 labelled requests: 22 answerable recall questions, 15
with no answer in the store, 10 weather and task lookups, 27 actions or judgment phrased
as questions, and the 41 routing requests of `scripts/bench_tier1.py`:

| Qwen3 4B (Instruct 2507, 4-bit) | |
|---|---|
| Answered locally when it should have gone to Claude | 0 of 60 (0 of 27 adversarial, 0 of 15 with no answer) |
| Answerable recall questions answered right locally | 16 of 22, none wrong (2 more went to a mail search; the rest to Claude) |
| Routing (weather, tasks, the original 41) | 10 of 10, 41 of 41 |
| Time (median, on the Mac mini with Scout running) | routing 1.5 s, a recall's search and answer 2.4 s: 3.8 s in all. Claude needs at least two round trips for the same (a search, then the answer; ~1.5-2.5 s each) |

### Follow-ups

"What did he say about it?", "and when is that due?" and "what about the other one?"
name no subject. A search by their own words finds nothing, or the wrong thing. With
`claude.local_rewrite` (on by default), such a question is first rewritten as one that
stands alone, from what was said in the last ten minutes.

- Plain code spots a follow-up: a short question with a pronoun that points back, or
  one that starts "and ..." or "what about ...". Other questions pay nothing (a few
  microseconds to check).
- When the last answer was about one record and one person, code replaces the pronouns:
  after Pat's budget email, "what did she say about hiring" becomes "what did Pat say
  about hiring".
- Otherwise the local model writes it from the last three turns (what you asked, what
  Scout said, the titles of the records): JSON only, at most 40 tokens, two seconds at
  most, and only words from the conversation. Anything else and Claude takes it.
- The records just spoken of are read first. The answer prompt shows your own words.
  Every topic word of the follow-up must be in the record: "what about my Chicago
  flight?" isn't answered from the Denver one.
- A follow-up that acts or asks for judgment ("and send it to Sam", "should we switch")
  goes to Claude before any rewrite.

All of this stays on the Mac. Claude gets the request as you said it, under the same
privacy rules.

Measured with `scripts/bench_followup.py`: two-turn conversations over the same
synthetic store, each follow-up run as asked, rewritten, and rewritten by the model
alone. The held-out set was written after tuning, and run once.

| Qwen3 4B (Instruct 2507, 4-bit) | As asked | Rewritten |
|---|---|---|
| Follow-ups answered right locally | 1 of 22, 2 wrong | 20 of 22, none wrong |
| Held-out follow-ups answered right locally | 0 of 10 | 4 of 10, none wrong (5 in an earlier run) |
| Answered locally when it should have gone to Claude | 0 of 12; held out 1 of 5 | 0 of 12; held out 0 of 5 |
| Rewrite time on follow-ups | | p50 0.40 s, p95 0.57 s (by code alone: under 10 ms) |

A rewritten follow-up about the records just spoken of skips the routing call, so a
follow-up takes about as long as before. Of `bench_recall.py`'s 115 requests, plain code
takes 8 for follow-ups; all 8 act or judge, so they go to Claude with no rewrite. Most
misses are a word the record lacks ("when is Luis coming", "where is it flying from"),
and those go to Claude.

## Usage: what answered

Every request is logged in `state/ledger.jsonl`: which tier answered (0 plain code,
1 the local model, or Claude), how long it took, and for Claude the turn's own cost
(the session reports a running total; Scout records the difference), turns and tokens
(uncached input, cache writes, cache reads, output). "How much have you spent today /
this week?" answers from it locally, tokens included.

### What a Claude turn costs

The room assistant and the phone's "ask Scout" are lean by default: they load no
`~/.claude` settings files (`claude.setting_sources = []`), so your CLAUDE.md, auto
memory, personal skills, hooks and other plugins aren't sent with every request. Only
Scout's own skills load (`claude.plugins = "scout"`), and `claude.inherit_settings`
copies just the parts of `~/.claude/settings.json` the room uses: your permission rules
(so the `settings` approval policies behave as before), model and effort. The `settings`
policy brings your hooks along too. `setting_sources = ["user"]` with `plugins =
"enabled"` restores the old full load.

`scripts/bench_brain.py` measures it with the real room options (a few synthetic
questions, one calling the weather tool; about $0.15 a variant on Opus). On Opus, a
first turn went from about 29k input tokens to 22k and a turn with one tool call from
97k to 72k. Sonnet costs about 40% less a turn than Opus, with a slower
first word.

## Pulse

Every 15 minutes Scout checks, in plain code, for an event about to start, a timed
reminder coming due, and (once each morning) today's overlapping events, and says each
once. Quiet hours (22:00-08:00) only log. `[pulse]` tunes or turns it off.

## Sync status: "is everything syncing?"

Each sync records every pass in `state/sync_status.json`: Mail, meeting notes by
email, Granola, Slack, the task farm, Reminders mirroring, the vectors for search by
meaning, and link upkeep. It keeps when the pass ran, whether it worked, what it added
or updated, and what's still waiting where that's known. An error keeps only its type
and a short message with quoted text, links and addresses taken out.

Each source is ok, failing (its last pass failed), stalled (no good pass in 3 times its
interval, 30 minutes at least), off (not set up) or waiting (no pass yet).
`scripts/scoutctl.sh status` prints a line for each, scout-memory serves them at
`GET /v1/sync`, and "is everything syncing?" or "what's the sync status?" gets a spoken
answer. The pulse says it once a day when a source has failed for an hour or stalled:
"Mail sync has been failing since 9 this morning." Messages has no sync to report:
Scout reads it live.

## Links: "give me a link to that"

Everything Scout says keeps the ids of the records it was about: the tasks the pulse
named, the note a recall answer quoted, the tasks and emails a Claude turn looked up.
Right after, "give me a link to that" (or "send me that link") puts the record's link,
such as the Granola meeting a task was found in, into your Scout reminders list, and
"where did that come from" says which meeting or email it was. Claude gets the same
ids in its request context and the `open_link` and `send_link` tools (the room, the
phone and other sessions). Details and the privacy rules: [docs/links.md](docs/links.md).

## Car stereo (Kenwood DNR1008RVS)

The Kenwood has no network or remote-control API (CarPlay, Android Auto and
Bluetooth only), so the one way in is audio: pair the Mac mini with it over Bluetooth
(Kenwood: Bluetooth setup, add a device; Mac: System Settings, Bluetooth). If the
Kenwood shows up as a sound output on the Mac, put it first in Scout's output list
and keep the TV as the fallback; Scout switches when one comes or goes:

```toml
[audio]
output_device = "KENWOOD"
output_fallback = ["SAMSUNG", "default", "any"]
```

Untested: some head units only accept phones; if the Mac never appears as an audio
device there, this route isn't available.

## Privacy: what Claude sees

Scout keeps your private data on the Mac where a local model can do the job.
Email is summarized by the tier 1 model (`claude.local_model`) on this Mac, and
`privacy.mode` in config.toml (enforced in code, `src/scout/privacy.py`) decides
what reaches Claude, for the room and every other session alike:

| | `strict` | `balanced` (default) | `open` |
|---|---|---|---|
| Email listings | sender, subject | sender, subject, one-line local summary | sender, subject |
| One email (`mail_read`) | sender, recipients, subject | same, plus a few-sentence local summary | full text |
| Exact text (`mail_read_full`) | refused | full text (for quoting in a reply) | full text |
| Calendar | titles, times, places | titles, times, places | plus notes and attendees |
| Memories | none | the ones that bear on the request | the ones that bear on the request |
| Local answers from private data | withheld | shared | shared |

In strict mode Claude says plainly when a task needs more. Asked directly, Scout
itself always answers locally: "what did the Rover email say" and "summarize my
unread mail" are read and summarized on the Mac and spoken, without Claude.

Emails that address an AI, try to give instructions, or are password or prize
lures are caught in code before the model sees them and described as "looks
like a scam or a manipulation attempt" (small models relay injected claims as
fact). Other summaries are attributed to the sender ("Pat asks you to...").

## Memory

Tell Scout things to keep: "remember that my dentist is Dr. Lee", "note that
Pat is my manager". Ask "who's my dentist", "what do you know about Pat", "what
do you remember", or "forget my dentist". Plain code answers these, and the
facts live in `memory/facts.md` in Scout's data folder, one `- fact · date` line
each: edit the file and Scout picks the change up. Claude sees a memory
only when it shares words with the request (and not in strict mode); the room
agent can also `remember`, `forget` and `recall`.


## Tasks

Scout keeps a task list of what you owe and what others owe you, and fills it from your
meetings and email (design: `docs/tasks-design.md`). Each task is a markdown file in
`memory/tasks/` (`0012-send-the-budget-to-pat.md`): a title, `- key: value` fields
(status, owner, due, importance, urgency, effort, pin, source) and its context. Edit,
add or delete files and the list follows. Open tasks are mirrored to Reminders (yours in
**Scout**, others' in **Waiting On**); completing one there completes the task.

The order is plain code, so it's stable and says why: a pin from you, then overdue, due
today or tomorrow, important, urgent, quick wins when nothing presses, people you've
told Scout about; undated tasks sink with age, and what others owe you comes up when
it's due. Ask "what should I work on?", "what am I waiting on?", "add a task...",
"mark task 3 done", "make the budget top priority". The morning briefing names your
top three and follow-ups that are overdue.

**Found tasks.** Every 10 minutes while Scout is idle it reads what's new (the last two
weeks, the first time), once each. Meetings, Granola notes and meeting notes that come
by email, are read by Claude (`[tasks] extract_model`), a few at a time. Email and Slack
are read by the local model and stay on the Mac; no-reply senders, newsletters and mail without an action
cue are skipped without any AI. Found tasks are added directly, each with the quote
that shows it and its source (`task_why`). A task that repeats an open one adds to it.
Put your names and addresses in `[tasks] me` so it can tell your tasks from others'.

**Meeting notes by email.** Google Meet's Gemini notes, Zoom, Otter, Fireflies, Fathom,
tl;dv and Teams recaps are recognized by sender and subject and stored as meetings, in
full, so they're searched and read for tasks like Granola notes. Only what's in the
email: a transcript that lives only in a linked Google Doc isn't reachable.

## Notes and lists

Notes are markdown files in Scout's data folder, `memory/notes/0003-generator-oil.md`
(the number is the note's id), searchable by words and meaning ("take a note: ...",
"what are my notes about the generator"). Edit or add a file and search follows within
a minute. There's a copy in an Apple Notes folder
called Scout when `[notes] apple = true` and you allow Notes access (macOS asks once,
the first time a note is saved). Lists and tasks use the Mac's Reminders; when that
isn't available (access refused, or not a Mac) Scout keeps them itself, with the same
answers and tools. `[lists] backend` picks `auto`, `reminders` or `scout`.

## Local search

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
Tailscale). See docs/links.md. It keeps running while the voice app restarts;
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
messages together with the local model, so Slack text stays on the Mac; bot messages
and messages without an action cue are skipped. Like mail, Slack text reaches Claude
only in open privacy mode. Making the app and its token: [docs/slack.md](docs/slack.md).
