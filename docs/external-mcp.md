# Your own MCP servers

Scout can use MCP servers you have added to Claude Code, and others you describe for it,
in the room and on the phone. Nothing about any one server is in Scout's code: what Scout
may do with each server lives in one local file, `tools.toml` in Scout's data folder
(`~/Library/Application Support/Scout/tools.toml`, mode 600). The code is `extmcp.py`.

## The usual case: a server you already added to Claude Code

```toml
[[server]]
from_claude = "acme"           # its name in `claude mcp list`
channels = ["room", "phone"]   # where Scout offers it; default ["room"]
private = true                 # its results are your private data (see Privacy)
hint = "Acme's help desk: tickets and customers."   # one line, for the prompts
```

Scout copies nothing from Claude Code's config into its own files: at start it reads the
server's entry (`~/.claude.json`: user scope, the room folder's `.mcp.json`, then local
scope, the later one winning as in Claude Code) and gives it to its Claude sessions under
the same name.

**Sign-in.** An HTTP server added without headers uses OAuth, and Claude Code keeps its
tokens by the server's name and config. Scout's sessions run the same Claude Code CLI as
the same user, so given the server under the same name and URL, the CLI finds that sign-in
and connects: there is nothing to do. (Under another name it would ask for a sign-in
again; that is why a `from_claude` server keeps its name.) Scout never reads those
credentials. If the sign-in has expired or was never done, `scout status` says
`auth needed`: sign in once with `claude mcp login <name>` (or `/mcp` in Claude Code),
then restart Scout.

## Servers that aren't in Claude Code

```toml
[[server]]
name = "widgets"
command = "uvx"
args = ["widgets-mcp"]
env_file = "widgets.env"                     # in the data folder, mode 600: KEY=value lines
env = { WIDGETS_KEY = "${WIDGETS_KEY}" }

[[server]]
name = "wiki"
url = "https://wiki.example.com/mcp"
env_file = "wiki.env"
headers = { Authorization = "Bearer ${WIKI_TOKEN}" }
```

An env file that anyone else can read is refused. Secrets never go on a command line
(the Agent SDK passes MCP config as an argument, which any local user can see with `ps`):
each header and env value travels in the CLI's environment and the config names it as
`${...}`, which the CLI expands.

## Rules for the tools

`[[server.tool]]` tables, in order; the first one that matches a tool decides.

| key | |
|---|---|
| `name` | a glob over the tool's own name (`ticket_*`) |
| `pattern` | or a regular expression (whole name) |
| `destructive` | or `true`: the tools the server itself marks destructive |
| `deny` | `true`: never offered, in the room or on the phone |
| `asks` | `true`: a spoken yes in the room every time (two steps by phone); `false`: never asks |
| `asks_when` | ask only when this argument is set (for a server with its own `confirm=true` step) |
| `readback` | the spoken question, with `{argument}` or `{argument\|what to say without it}` |
| `phone` | `true`: offered on the phone too |

Defaults, for a tool no rule matches:

- **Room:** runs without asking when the server marks it read-only (`readOnlyHint`) and
  not destructive; anything else asks. Reading only brings data in; a tool that doesn't
  say it only reads may change something under your name, so it is treated as a write.
- **Phone:** not offered. You choose what is safe to use from a phone, tool by tool.

A tool that asks works like Scout's own actions: the read-back is spoken, your yes or no
is the answer, every decision is kept in the trust log (`state/trust.json`), and "yes,
always" is just this once (these can't be turned into "just do it").

## Where Scout uses them

- **Room** (the mic and the web page): the room's Claude session gets each server whose
  `channels` include `room`, the hints in its system prompt, and every call to their
  tools passes Scout's check (a permission "ask" rule for each server, so even tools your
  own Claude Code settings allow come to Scout first). Denied tools are hidden from
  Claude once Scout has seen the server's tool list.
- **Phone:** each server with a `phone = true` tool becomes one phone function,
  `mcp_<name>`, that takes the caller's request in their words. A Claude session with only
  that server's phone tools answers it (a little slower than Scout's own tools; a long one
  is sent into the call when ready). A tool that asks isn't run: it comes back as an
  action with a code, read to the caller, and runs only after their yes
  (`confirm_action`, checked by the call's yes gate), straight on Scout's connection to the
  server with no model turn.
- **Desk sessions** use your servers through Claude Code itself, where you added them.

## Privacy

`private = true`: in strict privacy mode the server is off for Claude (its results would
reach Claude as they are; Scout can't summarize them first the way it does mail). In
balanced and open modes Claude sees the results, and what was said from them is kept as
private in the journal and in what Scout said lately, like mail.

## Health

Scout keeps one Claude session of its own connected to the servers (no model turns run
in it). `scout status` prints a line per server:

    acme: up, 42 tools (phone, room)
    widgets: failing (room): spawn widgets-mcp ENOENT
    wiki: auth needed (room): sign in once: claude mcp login wiki (or /mcp in Claude Code)
    old: off (enabled = false)

It is checked when Scout starts, every ten minutes (a failing server gets a reconnect),
and again, with `tools.toml` reread, whenever the room's Claude session starts fresh
(`claude.session_idle_min`). A problem with the file itself (a bad rule, an unknown
server, a readable env file) is listed too. A server that fails is reported there and in
Scout's log, never silently left out.

`tools.example.toml` in the code folder has a complete example for a made-up server.
