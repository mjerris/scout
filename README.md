# scout

An always-on voice front-end for a Claude Code agent on a Mac mini. Speech
recognition and speech synthesis run locally on Apple Silicon; only the request
text goes to Claude, and plain code or a small local model answers what it can
before Claude is asked.

```
mic ─┐                                                    ┌→ Silero VAD + smart-turn → MLX Whisper → gate → "Hey Scout …" → Claude
     ├─ voice layer: one duplex engine, echo cancelled ───┤
spk ←┘   (WebRTC AEC3, or Apple voice processing)         └← Kokoro TTS ← markdown → speech ←────────────────────────────────┘
```

## Quick start

```sh
claude plugin marketplace add mjerris/claude-plugins   # once: Michael Jerris's plugins
claude plugin install scout@mjerris
```

The next Claude Code session installs and starts the background app (a login item, so
it runs whether or not a session is open). Say "Hey Scout, set me up" and it walks you
through the rest by voice. `/scout:status` shows progress. Running from a checkout,
keeping it on at login, logs and the data folder: [docs/install.md](docs/install.md).

## Talking to Scout

Say "Hey Scout, …" with the request in the same breath, or "Hey Scout", wait for the
chime, then speak. For 8 seconds after an answer, a follow-up needs no wake word if it
reads as meant for Scout. "Scout, stop" always works, even over its own voice. Every
transcript passes a local gate first, so noise and talk in the room cost no tokens.

| Doc | What's in it |
|---|---|
| [Voice](docs/voice.md) | The echo-cancelled audio engine, barge-in, end of turn, the wake word and follow-up rules, the gate, working sounds, pronunciation, tuning |
| [Permissions](docs/permissions.md) | What runs on its own and what asks out loud, "yes, always", earned trust, typed approval from a desk session |
| [Setting up by voice](docs/setup.md) | The walkthrough that turns each feature on, one yes at a time |
| [Web page and devices](docs/web.md) | Live transcript, typed requests and approvals, push-to-talk from a phone over Tailscale |

## What it can do

| Doc | What's in it |
|---|---|
| [Mail](docs/mail.md) | Read, search, draft and (after a spoken yes) send through Mail.app; the fast index read |
| [Calendar, reminders, briefing, pulse](docs/calendar.md) | The Mac's calendars and Reminders, free time, the morning briefing, heads-ups as things start or come due |
| [Messages](docs/messages.md) | Read texts through one Full Disk Access helper; send after a read-back |
| [Contacts](docs/contacts.md) | Look people up, text or call them, one person across mail, meetings and Slack |
| [Slack](docs/slack.md) | Searchable copy of your Slack, who just messaged you said out loud, catch-up on a chat, send after a yes |
| [Mac controls, weather, maps, Starlink, offline reference](docs/lookups.md) | Media, volume, apps and timers; Open-Meteo, OpenStreetMap, the dish's local API; Wikipedia with no internet ([details](docs/reference.md)) |
| [Phone calls](docs/phone.md) | The phone app calls a SignalWire agent that answers through Scout; pairing, push, a test call ([design](docs/phone-design.md)) |
| [Siri](docs/siri-design.md) | "Hey Siri, ask Scout" from the paired phone, even locked (CarPlay, AirPods), with a separate key whose abilities you set |
| [Other Claude Code sessions](docs/sessions.md) | `discuss` (speak and listen), the floor, and Scout's mail, calendar and Slack tools in any session |
| [Your own MCP servers](docs/external-mcp.md) | Servers from Claude Code or `tools.toml`, in the room and on the phone, with your rules |

## What it remembers

| Doc | What's in it |
|---|---|
| [Memory, notes, search and sync](docs/memory.md) | Facts, notes and lists, the journal, the local search index over mail, meetings and Slack, sync status, links between records |
| [Tasks](docs/tasks.md) | What you owe and are owed, found in meetings, mail and Slack, ordered in plain code ([design](docs/tasks-design.md)) |
| [Behavior rules](docs/behavior-rules.md) | "From now on, call me Mike": how you want Scout to behave, for every session |
| [Links](docs/links.md) | "Give me a link to that": from what Scout said to the record behind it |

## How it decides, and what Claude sees

| Doc | What's in it |
|---|---|
| [Answered on the Mac](docs/local-answers.md) | Plain code and the local model (Qwen3 4B) first: recall from your records, follow-up rewriting, measurements, what a Claude turn costs |
| [Privacy](docs/privacy.md) | `privacy.mode`: what of your mail, calendar, meetings and memories reaches Claude, and the scam and injection screen |

## Files and development

Everything that must survive an update lives in Scout's data folder,
`~/Library/Application Support/Scout/` (`SCOUT_HOME` moves it): `config.toml` (every key
has a default; see `config.example.toml`), `pronounce.txt`, `tools.toml`, `state/`,
`memory/`, `logs/`, `models/` and the native helpers in `bin/`.

`bash scripts/run-ci.sh` runs the checks GitHub Actions runs: ruff lint and format,
strict mypy, ShellCheck and pytest. More in [docs/install.md](docs/install.md#checks).
