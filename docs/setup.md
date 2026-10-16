# Setting Scout up by voice

Scout can walk you through what it can do and set each thing up as you go. The code is
`src/scout/onboarding.py`; the room's side is in `assistant.py` (`_walkthrough`,
`_setup_watch`).

## The walkthrough

It starts by itself on a new install, at the first quiet moment after Scout starts (a
config file that names no user and connects nothing counts as new). Any time after,
say **"set me up"** or **"walk me through setup"**; **"start setup over"** goes through
every step again.

Each step says what it does, then asks. Answer **yes**, **no**, or **later**:

| step | yes | no |
|---|---|---|
| your name | `[tasks] me` gets the name you say | left for later in the file |
| phone | where the SignalWire details go, and the Tailscale and pairing steps | nothing |
| lists | `[lists] backend = "reminders"` (macOS asks for Reminders access) | `"scout"`: Scout's own lists |
| notes | `[notes] apple = true` | `false` |
| privacy | `[privacy] mode = "balanced"` | `"strict"` (`"open"` only in the file) |
| mail | `[index] mail = true` | `false` |
| calendar | macOS asks for calendar access | nothing |
| Contacts | `[contacts] enabled = true`, and macOS asks for access | `false` |
| Slack | where the token goes (docs/slack.md) | nothing |
| GitHub | `[github] enabled = true` (the `gh` sign-in, or a token in the file) | nothing |
| Granola | where the API key goes | nothing |
| maps and references | the reference download script (maps need nothing) | nothing |
| MCP servers | each Claude Code server not in tools.toml: added to it (below) | remembered, not asked again |

A step that's set up already (a name in `[tasks] me`, a Slack token, the phone on, a
lists backend chosen) is passed over.

**Later** (or "not now", or no answer, or "stop") ends the walkthrough at that step;
"set me up" picks up there next time. Answers are kept in `state/onboarding.json`.

**Secrets are never spoken or asked for.** For a token or key, Scout says which section
of `config.toml` it goes in (the screen shows the exact key), and you put it there.

**Writing the config.** Each change is written into `config.toml` in Scout's data folder
in place: the key's line is replaced where it is, or added to its section, or the section
is added; comments and everything else stay. Before writing, the result is loaded with
Scout's own checks (a value it would refuse leaves the file alone), and a copy of the file
as it was is kept next to it, `config.toml.bak-setup-<time>`, mode 600 like the file.
Most changes take effect when Scout restarts.

## MCP servers

The walkthrough offers each MCP server added to Claude Code (found the way
docs/external-mcp.md describes: `~/.claude.json` and the room folder's `.mcp.json`) that
`tools.toml` doesn't list yet. A yes adds it to `tools.toml` in Scout's data folder, never
to the code:

```toml
[[server]]
from_claude = "<its name>"
channels = ["room"]
private = true
```

Room only and private: its read-only tools run without asking, anything else asks, and
nothing is offered on the phone until you add tool rules (docs/external-mcp.md). A copy of
tools.toml as it was is kept first (`tools.toml.bak-setup-<time>`).

**A new server later.** Every ten minutes Scout looks at Claude Code's servers. One that
wasn't there before, isn't in tools.toml and was never answered is asked about once, at a
quiet moment (nobody talking, nothing going on, not in `[pulse] quiet_hours`): "Use your
<name> server in Scout too?". Yes adds it as above; no is remembered and never asked
again; no answer asks again a day later. The servers Claude Code had when this feature
first ran on an install already in use are not "new": the walkthrough offers them.
