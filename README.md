# claude-voice

An always-on voice front-end for a Claude Code agent on a Mac mini. Speech
recognition and speech synthesis run locally on Apple Silicon; only the request
text goes to Claude.

```
mic → WebRTC VAD → MLX Whisper (large-v3-turbo) → "hey Claude …" → Claude Agent SDK session
                                                                       │
speakers ← Kokoro TTS (ONNX) ← markdown → speech ←─────────────────────┘
```

- **Wake word:** say "Hey Claude, …" (or just "Claude, …") with the request in
  the same breath, or say "Hey Claude", wait for the chime, then speak. For 8
  seconds after a reply you can follow up without the wake word.
- **Stop:** "Claude, stop" interrupts speech and the running request. "Claude,
  new conversation" starts a fresh session.
- **Permissions:** `claude.approval_policy` picks who decides what runs
  without asking:
  - `strict` (default): read-only tools and a short list of harmless commands
    (`date`, `ls`, ...) run on their own; everything else is asked out loud.
  - `settings`: your `~/.claude` allow rules and hooks apply; you're asked
    only where Claude Code would normally prompt.
  - `settings_no_hooks`: your allow rules apply but your hooks are off, since a
    hook that answers "allow" would skip every prompt.

  Spoken prompts are short ("Run command?", "Edit config.py?"). Answer yes or
  no; the web page shows the details. No answer within 30 s counts as a skip.
  Under the settings policies, `claude.always_ask` rules (git push, PR
  merge/create by default) are always asked.
- **Changing the app by voice:** the voice agent can't edit this project. When
  you ask it to change how it listens, talks or asks, it calls its
  `request_app_change` tool, which appends to `state/change-requests.jsonl`
  and logs `change request: ...` for the Claude session that maintains the app.
- **Remote Control:** set `claude.remote_control = "<name>"` to start the voice
  session with Remote Control enabled.
- **Web page:** the log prints `http://<mini-ip>:8765/?token=…`. The page shows
  a live transcript and lets you type requests, approve or deny actions, stop,
  mute the mic and reset the session. The token lives in `state/web_token`;
  delete that file to rotate it.

## Setup

```sh
scripts/fetch-models.sh     # Kokoro voice model (~350 MB); Whisper (~1.6 GB) downloads on first run
cp config.example.toml config.toml   # optional; every key has a default
scripts/run.sh              # foreground run; allow microphone access when macOS asks
```

Claude uses the account the `claude` CLI is logged in with. Run `claude` once
first if you have never logged in.

## Always on

```sh
scripts/launchd.sh install    # start at login, restart if it exits
scripts/launchd.sh status | restart | uninstall
```

- The Mac must be logged in, so a LaunchAgent has an audio session (System
  Settings → Users & Groups → automatic login).
- `run.sh` holds a `caffeinate` assertion, so the mini won't idle-sleep while
  the assistant runs.
- Under launchd, macOS attributes mic access to the process itself rather than
  your terminal. If the log says *microphone has delivered pure silence*, enable
  it under System Settings → Privacy & Security → Microphone.

Logs: `logs/claude-voice.log` (plus `logs/launchd.*.log` under launchd).

## Tuning

`uv run claude-voice --list-devices` lists audio devices for `[audio]
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
