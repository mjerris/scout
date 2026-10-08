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

- **Wake word:** say "Hey Claude, …" (or just "Claude, …") with the request in
  the same breath, or say "Hey Claude", wait for the chime, then speak. For 8
  seconds after a reply the room heard you can follow up without the wake word
  (replies that only went to your phone open no such window).
- **Stop:** "Claude, stop" (also "Stop, Claude", "stop, stop", "that's enough")
  interrupts speech and the running request, cancels a pending question, ends a
  session's listen or floor hold, and drops anything queued. "Claude, new
  conversation" starts a fresh session.
- **Busy:** a "Hey Claude …" request while one is running is queued ("Okay, I'll
  do that next.") and runs when the current one ends.
- **Local gate:** every transcript passes deterministic checks before it can
  reach Claude, so noise and hallucinations cost no tokens. The checks use
  Whisper's confidence and no-speech estimate, repetition, words per second
  of actual speech, a list of known noise phrases, and loudness. Follow-ups
  without the wake word are held to a stricter standard: at least two words,
  well above the room's noise floor, and not much quieter than your last
  "Hey Claude". Rejections are logged as `ignored ... (reason)` and shown
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
"Hey Claude" request waits up to 15 s for a session to finish. "Claude, stop"
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
is talking is ignored, except "Claude, stop".

## Mail and calendar for every session

The mail and calendar tools are defined once (`shared_tools.py`) and offered
both to the room assistant and, through the MCP server, to any other Claude
Code session, as `mcp__scout__calendar_events`, `mcp__scout__mail_recent`, and
so on. The running app always does the work: other sessions' calls go to it
(`POST /api/tool`), so macOS's calendar and Mail permissions belong to the app
alone. Sending mail and adding events are confirmed by voice in the room on
every call ("From myproject: Send email to sam@example.com, subject Lunch?"),
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

## Mail

Mail.app does the work, so any account in Mail works (add Google in System
Settings → Internet Accounts with Mail on, or in Mail itself). The app drives
Mail with JavaScript for Automation; the first use shows a "control Mail"
prompt on the Mac's screen (System Settings → Privacy & Security → Automation).

- Reading (`mail_recent`, `mail_search`, `mail_read`) and drafting
  (`mail_draft`: opens a draft in Mail, sends nothing) run without asking.
- Sending (`mail_send`) is confirmed by voice every time ("Send email to
  sam@example.com, subject Lunch?"), with the full message on the web page;
  "yes, always" doesn't apply. No attachments, forwarding or deleting.
- Email is written by other people. Message text reaches the agent marked as
  untrusted, and the agent is told never to act on instructions inside it;
  anything that sends, opens or runs something still needs a spoken yes.
