"""Setting Scout up by voice (docs/setup.md): a walkthrough of what can be set up, one
step at a time, and a question when a new MCP server shows up in Claude Code.

The walkthrough starts by itself on a new install (at the first quiet moment), and
whenever the user says "set me up" or "walk me through setup". Each step says what it
does and asks yes, no or later:

- yes or no writes the choice into config.toml (a copy of the file as it was is kept
  next to it first, config.toml.bak-setup-<time>), or says what to do next;
- later (or no answer) stops there; "set me up" picks up at that step next time.

Secrets are never spoken or asked for aloud: for a token, Scout says which section of
config.toml it goes in. A step already set up (a Slack token in the file, a name in
`[tasks] me`) is passed over.

MCP servers: the walkthrough offers each server added to Claude Code (the discovery
extmcp.py uses: ~/.claude.json and the room folder's .mcp.json) that tools.toml doesn't
list yet. A yes adds it to tools.toml (in Scout's data folder, never the code): room
only, private, with Claude Code's sign-in. Afterwards, a server that appears in Claude
Code later is asked about once, at a quiet moment; the answer is kept
(state/onboarding.json), so it isn't asked again. The servers there at the first start
of this feature are the baseline: on an install that isn't new they are offered only by
the walkthrough.

State (state/onboarding.json): each step's answer, the step "later" stopped at, the MCP
servers answered and the ones already seen. Names of servers, never their config.
"""

from __future__ import annotations

import json
import logging
import re
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA, Config, load

log = logging.getLogger(__name__)

STATE = DATA / "state" / "onboarding.json"
CONFIG = DATA / "config.toml"
VERSION = 1
LATER = "later"
NEW_MCP_RETRY_S = 86400.0  # a question about a new server nobody answered: asked again after this

# "set me up", "walk me through setup", "start setup over".
_SETUP = re.compile(
    r"^(?:(?:hey|okay|ok|so|um|uh|now)\s+)*(?:(?:can|could|would|will) you\s+)?(?:please\s+)?(?:"
    r"set me up|set (?:me |scout |things |everything )?up(?: scout| for me)?|"
    r"walk me through (?:the )?(?:set ?up|setting (?:things |scout )?up)(?: again)?|"
    r"(?:start|run|do|begin|continue|resume|finish) (?:the )?(?:scout )?set ?up|"
    r"(?:start|restart|redo|run) (?:the )?set ?up (?:over|again|from the (?:start|beginning))|"
    r"help me set (?:things |scout )?up)(?:\s+please)?$",
    re.I,
)
_OVER = re.compile(r"\b(?:over|again|redo|restart|from the (?:start|beginning))\b", re.I)


def is_request(text: str) -> bool:
    """Is this the user asking for the walkthrough?"""
    t = " ".join(re.sub(r"[^\w\s']", " ", text).split())
    return bool(_SETUP.match(t))


def from_the_start(text: str) -> bool:
    return bool(_OVER.search(text))


# --- the state -----------------------------------------------------------------------------


def read_state(path: Path = STATE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("v") == VERSION else {}


def save_state(st: dict[str, Any], path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**st, "v": VERSION}, indent=1))
    tmp.replace(path)


def blank_state() -> dict[str, Any]:
    return {"v": VERSION, "answers": {}, "later": "", "mcp": {}, "mcp_seen": [], "auto": False}


def is_new_install(cfg: Config, config_path: Path = CONFIG) -> bool:
    """No config yet, or one that names no user and connects nothing: a new install."""
    if not config_path.exists():
        return True
    return not (
        cfg.tasks.me or cfg.slack.token or cfg.granola.api_key or cfg.phone.enabled or cfg.github.enabled
    )


# --- writing config.toml -------------------------------------------------------------------


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


_HEADER = re.compile(r"^\s*\[(\[)?\s*([A-Za-z0-9_.-]+)\s*\]?\]\s*(?:#.*)?$")


def _balanced(text: str) -> bool:
    """Has a value's text closed every bracket it opened (outside strings)?"""
    depth, quote = 0, ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "#":
            break
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return depth <= 0


def edit_toml(text: str, edits: dict[tuple[str, str], Any]) -> str:
    """The TOML text with each (table, key) set to its value: the key's line replaced
    where it is, else added to its table, else the table added at the end. Comments and
    everything else stay as they were."""
    lines = text.splitlines()
    for (table, key), value in edits.items():
        new = f"{key} = {toml_value(value)}"
        current, start, end, at = None, -1, len(lines), -1
        for i, line in enumerate(lines):
            m = _HEADER.match(line)
            if m:
                if current == table:  # the table ends at the next header of any kind
                    end = i
                    break
                current = None if m.group(1) else m.group(2)
                if current == table:
                    start, end = i, len(lines)
                continue
            if current == table and re.match(rf"^\s*{re.escape(key)}\s*=", line):
                at = i
        if at >= 0:
            stop = at + 1
            while stop < len(lines) and not _balanced("\n".join(lines[at:stop]).split("=", 1)[1]):
                stop += 1  # a value over several lines (an array)
            lines[at:stop] = [new]
        elif start >= 0:
            last = start
            for i in range(start + 1, end):
                if lines[i].strip() and not lines[i].lstrip().startswith("#"):
                    last = i
            lines.insert(last + 1, new)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{table}]", new]
    return "\n".join(lines) + "\n"


def write_config(
    edits: dict[tuple[str, str], Any], path: Path = CONFIG, now: float | None = None
) -> Path | None:
    """config.toml with these values, after a copy of it as it was (config.toml.bak-setup-
    <time>, mode 600 like the file). Checked before it's kept: a result that doesn't load
    leaves the file alone (ValueError). Returns the copy's path (None: there was no file)."""
    old = path.read_text() if path.exists() else ""
    new = edit_toml(old, edits)
    doc = tomllib.loads(new)
    for (table, key), value in edits.items():
        if (doc.get(table) or {}).get(key) != value:
            raise ValueError(f"{table}.{key} didn't come out as written")
    check = path.with_name(path.name + ".check")
    _write_private(check, new)
    try:
        load(check)  # the same checks Scout makes at start
    finally:
        check.unlink(missing_ok=True)
    backup = None
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        backup = path.with_name(f"{path.name}.bak-setup-{stamp}")
        _write_private(backup, old)
    _write_private(path, new)
    return backup


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


# --- MCP servers ---------------------------------------------------------------------------


def listed_servers(tools: Path) -> set[str]:
    """The servers tools.toml names (on or off): decided already."""
    try:
        doc = tomllib.loads(tools.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    return {
        str(s.get("from_claude") or s.get("name") or "")
        for s in doc.get("server") or []
        if isinstance(s, dict)
    } - {""}


def claude_code_servers(cfg: Config, claude_json: Path | None = None) -> list[str]:
    """The MCP servers added to Claude Code that Scout could use, by name."""
    from . import extmcp

    found = extmcp.claude_servers(claude_json or extmcp.CLAUDE_JSON, Path(cfg.claude.cwd).expanduser())
    return sorted(n for n in found if extmcp._NAME.fullmatch(n) and n not in extmcp.RESERVED)


def unlisted_servers(cfg: Config, tools: Path, claude_json: Path | None = None) -> list[str]:
    have = listed_servers(tools)
    return [n for n in claude_code_servers(cfg, claude_json) if n not in have]


def add_server(name: str, tools: Path, now: float | None = None) -> Path | None:
    """A Claude Code server added to tools.toml: the room only, private, its sign-in kept
    (from_claude). A copy of the file as it was is kept first. Returns the copy's path."""
    from . import extmcp

    if not extmcp._NAME.fullmatch(name) or name in extmcp.RESERVED:
        raise ValueError(f"{name!r} isn't a server name Scout can use")
    if name in listed_servers(tools):
        return None
    old = tools.read_text() if tools.exists() else (
        "# Scout's rules for your MCP servers (docs/external-mcp.md). Mode 600.\n"
    )  # fmt: skip
    block = (
        f'\n[[server]]\nfrom_claude = {toml_value(name)}\nchannels = ["room"]\nprivate = true\n'
        "# Added by Scout's setup. Tools that only read run without asking; anything else asks.\n"
    )
    new = old.rstrip("\n") + "\n" + block
    tomllib.loads(new)  # never write a file that doesn't parse
    backup = None
    if tools.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        backup = tools.with_name(f"{tools.name}.bak-setup-{stamp}")
        _write_private(backup, old)
    _write_private(tools, new)
    return backup


# --- the steps -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What an answer does: values for config.toml, a macOS permission to ask for
    ("contacts", "calendar", "reminders"), and what to say after."""

    values: dict[tuple[str, str], Any] = field(default_factory=dict)
    access: str = ""
    say: str = ""


@dataclass(frozen=True)
class Step:
    key: str
    intro: str  # what it does
    question: str  # asked as yes, no or later
    detail: str  # on the screen with the question: where things go, exactly
    yes: Outcome
    no: Outcome = Outcome()
    done: Callable[[Config], bool] = lambda cfg: False  # already set up: passed over


_CONF = "Scout's config file, config.toml in its data folder"


STEPS: tuple[Step, ...] = (
    Step(
        "phone",
        "Scout can talk with you from your phone, through SignalWire, with Tailscale reaching this Mac.",
        "Set up the phone?",
        "Phone: put space, project_id and api_token in the [phone] section of config.toml (never said "
        "aloud), set enabled = true, run scripts/tailscale.sh, then pair from the phone app "
        "(docs/phone-design.md).",
        Outcome(
            say=f"Okay. Put your SignalWire space, project ID and API token in the phone section of {_CONF}. "
            "Then run the Tailscale setup script, and pair from the phone app."
        ),
        done=lambda cfg: cfg.phone.enabled,
    ),
    Step(
        "lists",
        "Your lists and reminders can live in the Mac's Reminders app, or in Scout's own store on this Mac.",
        "Use the Reminders app for them?",
        'Lists: [lists] backend = "reminders" (yes) or "scout" (no).',
        Outcome({("lists", "backend"): "reminders"}, access="reminders", say="Okay, Reminders it is."),
        Outcome({("lists", "backend"): "scout"}, say="Okay, I'll keep lists myself."),
        done=lambda cfg: cfg.lists.backend != "auto",
    ),
    Step(
        "notes",
        "Notes you ask me to keep are stored and searched on this Mac. A copy can also go to Apple Notes.",
        "Copy notes to Apple Notes too?",
        "Notes: [notes] apple = true or false.",
        Outcome({("notes", "apple"): True}),
        Outcome({("notes", "apple"): False}),
    ),
    Step(
        "privacy",
        "How much of your private data Claude sees: in balanced mode it gets short summaries of your mail, "
        "written on this Mac, instead of the mail itself.",
        "Keep it balanced? Say no for strict, where Claude sees no email text at all.",
        'Privacy: [privacy] mode = "balanced" (yes) or "strict" (no). "open" can be set in config.toml.',
        Outcome({("privacy", "mode"): "balanced"}),
        Outcome({("privacy", "mode"): "strict"}, say="Okay, strict."),
    ),
    Step(
        "mail",
        "I can read and search your mail through the Mail app, with a quick index kept on this Mac.",
        "Turn on mail?",
        "Mail: [index] mail = true or false. macOS asks once to let Scout use Mail.",
        Outcome({("index", "mail"): True}, say="Okay. The first time, the Mac asks to let me use Mail."),
        Outcome({("index", "mail"): False}),
    ),
    Step(
        "calendar",
        "I can read your calendars, to say what's on and find free time, and add events when you say yes.",
        "Turn on the calendar?",
        "Calendar: macOS asks for calendar access once (Privacy and Security, Calendars).",
        Outcome(access="calendar"),
        Outcome(say="Okay, I won't ask for the calendar."),
    ),
    Step(
        "contacts",
        "I can look people up in your Contacts, and show names instead of numbers.",
        "Turn on Contacts?",
        "Contacts: [contacts] enabled = true or false. macOS asks for access once.",
        Outcome({("contacts", "enabled"): True}, access="contacts"),
        Outcome({("contacts", "enabled"): False}),
    ),
    Step(
        "slack",
        "I can keep a searchable copy of your Slack messages, find tasks in them, and say who messaged you.",
        "Set up Slack?",
        "Slack: make an app in your workspace (docs/slack.md), then put its user token (xoxp-...) in "
        "[slack] token in config.toml. Never said aloud.",
        Outcome(
            say=f"Okay. Make a Slack app for your workspace, as Scout's Slack guide says, and put its "
            f"user token in the slack section of {_CONF}. I'll never ask for it out loud."
        ),
        done=lambda cfg: bool(cfg.slack.token),
    ),
    Step(
        "github",
        "I can follow the GitHub issues and pull requests that come up in your mail, meetings and Slack.",
        "Turn on GitHub?",
        "GitHub: [github] enabled = true. It uses the gh command's sign-in, or a token in [github] token.",
        Outcome(
            {("github", "enabled"): True},
            say=f"Okay. I'll use the GitHub command line's sign-in, or a token in the github section of {_CONF}.",
        ),
        done=lambda cfg: cfg.github.enabled,
    ),
    Step(
        "granola",
        "I can search your Granola meeting notes, and find tasks in them.",
        "Set up Granola?",
        "Granola: an API key from Granola (Settings, Connectors, API keys) in [granola] api_key in "
        "config.toml. Never said aloud.",
        Outcome(
            say=f"Okay. Make an API key in Granola's settings, under connectors, and put it in the granola "
            f"section of {_CONF}."
        ),
        done=lambda cfg: bool(cfg.granola.api_key),
    ),
    Step(
        "reference",
        "Places and driving directions work with nothing to set up. I can also answer from references kept "
        "on this Mac, like Wikipedia and repair guides, even offline.",
        "Get the offline references?",
        "Reference: run scripts/fetch-reference.sh (a few gigabytes) into the reference folder "
        "(docs/reference.md).",
        Outcome(
            say="Okay. Run the fetch reference script in Scout's scripts folder; it downloads a few gigabytes."
        ),
    ),
)

STEP_KEYS = ("name", *(s.key for s in STEPS), "mcp")


# --- the walkthrough -------------------------------------------------------------------------

AskYes = Callable[[str, str], Awaitable[bool | str | None]]  # (spoken, on screen) -> yes/no/"later"/None
AskText = Callable[[str], Awaitable[str | None]]  # the spoken question -> what was said, or None
Say = Callable[[str], Awaitable[None]]
Access = Callable[[str], Awaitable[str]]  # "contacts"... -> "" when granted, else why not


def answer_of(a: bool | str | None) -> str:
    """A spoken answer as a step's: "yes", "no", or "later" (later, or no answer)."""
    if a is True or a == "always":
        return "yes"
    if a is False:
        return "no"
    return LATER


_NAME_LEAD = re.compile(
    r"^(?:(?:well|so|okay|ok|um|uh|hi|hey)[,\s]+)*(?:my name is|my name's|i am|i'm|im|it's|its|it is|"
    r"this is|call me|you can call me|just)\s+",
    re.I,
)


def spoken_name(text: str) -> str:
    """The name in "My name is Pat Lee." / "call me Pat" / "Pat"; "" when it isn't one."""
    t = " ".join(str(text or "").split()).strip(" .,!?\"'")
    t = _NAME_LEAD.sub("", t).strip(" .,!?\"'")
    t = re.sub(r"[,\s]+(?:please|thanks|thank you)$", "", t, flags=re.I)
    words = t.split()
    if not words or len(words) > 4 or not all(re.fullmatch(r"[A-Za-z][A-Za-z'.-]*", w) for w in words):
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in words)


@dataclass
class Walkthrough:
    """One run of the walkthrough, from where the last one stopped."""

    cfg: Config
    ask_yes: AskYes
    ask_text: AskText
    say: Say
    access: Access
    config_path: Path = CONFIG
    tools_path: Path = field(default_factory=lambda: DATA / "tools.toml")
    state_path: Path = STATE
    claude_json: Path | None = None
    now: Callable[[], float] = time.time
    changed: list[str] = field(default_factory=list)  # "table.key" written this run

    async def run(self, over: bool = False) -> str:
        """The steps not answered yet, in order. Returns what to say at the end."""
        st = read_state(self.state_path) or blank_state()
        if over:
            st.update(answers={}, later="", mcp={})
        resumed = bool(st.get("later"))
        st["later"] = ""
        if not any(st["answers"].get(k) for k in STEP_KEYS) and not resumed:
            await self.say(
                "Let's set Scout up. I'll go through each thing I can do, and you say yes, no, or later. "
                "Say later at any point, and we'll pick up there next time you say set me up."
            )
        elif resumed:
            await self.say("Picking up where we left off.")
        for key in STEP_KEYS:
            if st["answers"].get(key) in ("yes", "no"):
                continue
            got = await self._step(key, st)
            if got == LATER:
                st["later"] = key
                save_state(st, self.state_path)
                return "Okay, we'll pick up from here next time you say set me up."
            st["answers"][key] = got
            save_state(st, self.state_path)
        st["finished"] = self.now()
        save_state(st, self.state_path)
        tail = " Some of it takes effect when Scout restarts." if self.changed else ""
        return "That's everything. Say set me up again any time to go through it." + tail

    async def _step(self, key: str, st: dict[str, Any]) -> str:
        if key == "name":
            return await self._name()
        if key == "mcp":
            return await self._servers(st)
        step = next(s for s in STEPS if s.key == key)
        if step.done(self.cfg):
            return "yes"
        got = answer_of(await self.ask_yes(f"{step.intro} {step.question}", step.detail))
        if got == LATER:
            return got
        out = step.yes if got == "yes" else step.no
        if out.values:
            await self._write(out.values)
        if out.access:
            why = await self.access(out.access)
            if why:
                await self.say(f"The Mac didn't allow it yet: {why}")
        if out.say:
            await self.say(out.say)
        return got

    async def _name(self) -> str:
        if self.cfg.tasks.me:
            return "yes"
        for _ in range(2):
            heard = await self.ask_text("First, what should I call you?")
            if heard is None:
                return LATER
            name = spoken_name(heard)
            if not name:
                continue
            got = answer_of(
                await self.ask_yes(f"I'll call you {name}. Is that right?", f'[tasks] me = ["{name}"]')
            )
            if got == LATER:
                return got
            if got == "yes":
                await self._write({("tasks", "me"): [name, *self.cfg.tasks.me]})
                return "yes"
        await self.say("Let's leave your name for now; you can put it in the tasks section as me.")
        return "no"

    async def _servers(self, st: dict[str, Any]) -> str:
        answered: dict[str, str] = st.setdefault("mcp", {})
        names = [
            n for n in unlisted_servers(self.cfg, self.tools_path, self.claude_json) if n not in answered
        ]
        st["mcp_seen"] = sorted({*st.get("mcp_seen", []), *claude_code_servers(self.cfg, self.claude_json)})
        if names:
            await self.say(
                f"You have {len(names)} MCP server{'s' * (len(names) != 1)} in Claude Code that Scout "
                "doesn't use yet. I'll ask about each."
            )
        for n in names:
            got = await self.offer_server(n)
            if got == LATER:
                return got
            answered[n] = got
            save_state(st, self.state_path)
        return "yes"

    async def offer_server(self, name: str) -> str:
        """Ask about one Claude Code server; a yes adds it to tools.toml."""
        got = answer_of(await self.ask_yes(
            f"Use your {spoken_server(name)} server in Scout too? In the room, with nothing sent without asking.",
            f"MCP server {name}: added to tools.toml as from_claude, room only, private (docs/external-mcp.md).",
        ))  # fmt: skip
        if got == "yes":
            add_server(name, self.tools_path, self.now())
            self.changed.append(f"tools.toml {name}")
            await self.say("Okay, it's on from the next conversation.")
        return got

    async def _write(self, values: dict[tuple[str, str], Any]) -> None:
        try:
            write_config(values, self.config_path, self.now())
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            log.warning("setup: config.toml not changed (%s)", type(exc).__name__)
            await self.say("I couldn't change the config file, so I left it as it was.")
            return
        self.changed += [f"{t}.{k}" for t, k in values]
        log.info("setup: config.toml: %s", ", ".join(f"{t}.{k}" for t, k in values))


def spoken_server(name: str) -> str:
    return " ".join(name.replace("_", " ").replace("-", " ").split())


# --- new servers, asked about at a quiet moment ----------------------------------------------


def new_servers(
    cfg: Config, tools: Path, state: dict[str, Any], now: float, claude_json: Path | None = None
) -> list[str]:
    """Servers added to Claude Code since Scout last looked, not in tools.toml and never
    answered (one nobody answered is asked again after NEW_MCP_RETRY_S)."""
    seen = set(state.get("mcp_seen") or [])
    answered = state.get("mcp") or {}
    asked = state.get("mcp_asked") or {}
    return [
        n
        for n in unlisted_servers(cfg, tools, claude_json)
        if n not in seen
        and n not in answered
        and (n not in asked or now - float(asked[n] or 0) >= NEW_MCP_RETRY_S)
    ]


def startup_state(cfg: Config, config_path: Path = CONFIG, state_path: Path = STATE,
                  claude_json: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """The state at Scout's start: made the first time, with the servers Claude Code has
    now as the baseline, and `auto` set on a new install (the walkthrough is due)."""
    st = read_state(state_path)
    if st:
        return st
    st = blank_state()
    st["auto"] = is_new_install(cfg, config_path)
    if not st["auto"]:  # an install in use: today's servers are offered by the walkthrough only
        st["mcp_seen"] = claude_code_servers(cfg, claude_json)
    save_state(st, state_path)
    log.info("setup: %s; %d MCP servers in Claude Code", "a new install" if st["auto"] else "set up already",
             len(claude_code_servers(cfg, claude_json)))  # fmt: skip
    return st
