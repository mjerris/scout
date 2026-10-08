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
  is being said, so a quiet stretch doesn't sound like a hang.
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
  copies and loads Scout's skills plus every plugin enabled in
  `~/.claude/settings.json` (`claude.plugins`); their tools ask by voice unless
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
facts live in `state/memory.json` in Scout's data folder. Claude sees a memory
only when it shares words with the request (and not in strict mode); the room
agent can also `remember`, `forget` and `recall`.
