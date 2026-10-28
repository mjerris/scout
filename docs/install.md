# Installing and running Scout

Install Scout as a Claude Code plugin (the usual way) or run it from a checkout; keep
it running at login; where its files, logs and checks are.

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

## Setup from a checkout

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg` (`brew install ffmpeg`;
push-to-talk decodes browser recordings with it).

```sh
scripts/fetch-models.sh     # Kokoro voice (~350 MB), Silero VAD, smart-turn; Whisper (~1.6 GB) downloads on first run
scripts/build-voiceio.sh    # optional: the Apple voice-processing helper (audio.backend = "apple")
scripts/build-vcal.sh       # optional: calendar access (EventKit helper; calendar.md)
scripts/build-messages.sh   # optional: read-only Messages history (messages.md)
scripts/messages-agent.sh install   # ...and its own login item
scripts/build-contacts.sh   # optional: read-only Contacts lookups (contacts.md)
cp config.example.toml "$HOME/Library/Application Support/Scout/config.toml"   # optional; every key has a default
scripts/run.sh              # foreground run; allow microphone access when macOS asks
```

Scout keeps everything that must survive an update in its data folder,
`~/Library/Application Support/Scout/` (set `SCOUT_HOME` to move it):
`config.toml`, `pronounce.txt`, `state/` (saved rules, the web token),
`logs/`, `models/` and the compiled helpers in `bin/`. Paths like `state/…` and `logs/…`
in these docs are inside it. Moving from a claude-voice checkout:
`scripts/migrate-claude-voice.sh ~/src/claude-voice`.

Claude uses the account the `claude` CLI is logged in with. Run `claude` once
first if you have never logged in.

## Setting up by voice

On a new install Scout walks you through what it can do (your name, the phone, where
lists live, mail, calendar, Contacts, Slack, GitHub, Granola, references, your MCP
servers), one yes, no or later at a time, and writes `config.toml` (keeping a copy of it
first). Say "set me up" to start or pick up where you left off. Tokens are never asked for
aloud: Scout says where they go. Details: [setup.md](setup.md).

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

## Remote Control

Set `claude.remote_control = "<name>"` to start the voice
session with Remote Control enabled.

## Checks

Checks: `bash scripts/run-ci.sh` runs LINT (ruff), FMT (ruff format; applied
locally, checked in CI), TYPES (mypy strict), SHELL (shellcheck) and TEST
(pytest, including a few hundred real-world phrases in `tests/data/phrases.json`),
the same script GitHub Actions runs. `scripts/install-hooks.sh` adds a pre-commit hook
for lint, format and types.
