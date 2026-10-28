# Scout for your other Claude Code sessions

Any Claude Code session on the Mac can talk out loud through Scout and use its mail,
calendar, reminders, messages and Slack tools, with the same voice confirmations as
the room.

## Voice (MCP)

The app also runs an MCP server, so any Claude Code session (at your desk,
in a terminal) can talk out loud through the same mic, voice and filtering. The plugin
adds it to every session; from a checkout, add it by hand:

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

## Mail, calendar and reminders for every session

The mail, calendar and reminders tools are defined once (`shared_tools.py`) and offered
both to the room assistant and, through the MCP server, to any other Claude
Code session, as `mcp__plugin_scout_scout__calendar_events`,
`mcp__plugin_scout_scout__mail_recent`, and so on. The running app always does the work: other sessions' calls go to it
(`POST /api/tool`), so macOS's calendar and Mail permissions belong to the app
alone. Sending mail, adding events, and adding or completing reminders are
confirmed by voice in the room on every call ("From myproject: Send email to sam@example.com, subject Lunch?"),
whatever the calling session's own permission settings say.

A send's read-back is short: the question first (the action and who it goes to), then
the message itself when it's 15 words or less, else how it starts and roughly how long
it is ("It starts: Hey Joao, I went through the setup. It's about 300 words."). The
whole text is on the web page and in the session. An answer said over the read-back
counts once the question has been said, if it's a clear yes or no and not Scout's own
words coming back, and it ends the read-back.

How to use them (which tool, untrusted email, how to say results out loud) is
the `mail-calendar` skill in `skills/`; `scripts/install-skills.sh` links it
into `~/.claude/skills` for the room and every other session.

## Your own MCP servers

Scout can use MCP servers you already added to Claude Code (same name, same sign-in) or
describe for it, in the room and on the phone, by rules in a local
`tools.toml` in its data folder (mode 600): which servers, where, what is private, what
asks for your yes, what is off. Nothing server-specific is in Scout's code. `scout status`
says whether each one is up. Details: [external-mcp.md](external-mcp.md);
example: [`tools.example.toml`](../tools.example.toml). A server added to Claude Code later is offered once, by
voice, at a quiet moment.
