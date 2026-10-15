"""Configuration: defaults, overridden by config.toml in Scout's data folder."""

from __future__ import annotations

import datetime as dt
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]  # the code: the plugin folder, or a checkout
# Everything that must survive a plugin update: config.toml, state/, logs/, models/
# and the compiled helpers in bin/ (macOS ties permissions to the exact helper binary).
DATA = Path(
    os.environ.get("SCOUT_HOME") or Path.home() / "Library" / "Application Support" / "Scout"
).expanduser()


@dataclass
class WakeConfig:
    # Words that wake the assistant when they appear in the first few words of
    # an utterance. Whisper spelled "Scout" right in 25 of 25 tries across five
    # voices, so no near-misses are needed (unlike "Claude", which it heard as
    # "cloud", "clog" and "clod"). Add "claude" here to keep the old name too.
    names: list[str] = field(default_factory=lambda: ["scout"])
    # Near-misses that are also everyday names wake it only right after a greeting:
    # on the OBSBOT mic Whisper heard "Hey Scout" as "Hey Scott", but "Scott said
    # the build failed" is about someone else.
    greeted_names: list[str] = field(default_factory=lambda: ["scott"])
    max_position: int = 3  # name must be within the first N words
    # After a reply, listen this many seconds for a follow-up without the wake word.
    follow_up_seconds: float = 8.0
    # Ending with "hang on" / "wait" / "give me a sec" keeps listening this long.
    wait_seconds: float = 20.0


@dataclass
class AudioConfig:
    input_device: str = ""  # name substring or index; empty = system default
    # When that mic is gone, try these in order ("any" = any other mic except a nearby
    # iPhone's or iPad's), and switch back as soon as it returns. [] = wait for it.
    input_fallback: list[str] = field(default_factory=lambda: ["any"])
    # When the output_device is gone (the TV switched off), play here instead, and switch
    # back when it returns: "default" = the Mac's default output, "any" = any output.
    # Before this, a TV turned off crashed Scout until it came back (seen three times).
    output_fallback: list[str] = field(default_factory=lambda: ["default", "any"])
    output_device: str = ""
    vad_aggressiveness: int = 2  # 0-3, higher filters more non-speech
    silence_ms: int = 1200  # trailing silence that ends an utterance
    min_speech_ms: int = 300
    max_utterance_s: float = 30.0
    echo_tail_ms: int = 400  # ignore speech starting this soon after TTS stops
    # The voice layer: "auto" (= webrtc: WebRTC echo cancellation on one duplex
    # stream; measured best on the Mac mini), "apple" (macOS voice processing;
    # build it with scripts/build-voiceio.sh), or "plain" (no echo cancellation).
    backend: str = "auto"
    # Talking over the assistant stops it (needs an echo-cancelling backend).
    barge_in: bool = True
    # Barge-in waits until the echo canceller has adapted to the room: at least
    # this much playback experience and this much echo removed. Before that, its
    # own voice leaking through (a TV's speakers add delay and processing) looked
    # like the user talking over it and cut its messages short. Backends that
    # don't report it (apple) skip the check.
    # Extra delay the speaker adds that the computer can't see, in ms: a TV's sound
    # processing (a Samsung over HDMI measured ~750 ms). Helps the echo canceller
    # start out right and tells Scout when its voice has really finished playing.
    output_delay_ms: float = 0.0
    barge_in_min_learned_s: float = 3.0
    barge_in_min_erle_db: float = 12.0
    # WebRTC noise suppression on the mic. Off by default: it learns steady room
    # noise and removes that band from speech too (measured on the Mac mini: a
    # 3.5-4 kHz fan tone made it cut 8-11 dB there, and Whisper started hearing
    # "Claude" as "cloud"/"clog"). Whisper handles background noise well.
    noise_suppression: bool = False
    # End of turn: "smart" (Silero VAD + smart-turn model; falls back to "simple"
    # when the models aren't downloaded) or "simple" (silence_ms of quiet).
    turn_detection: str = "smart"
    min_pause_ms: int = 250  # smart: ask the turn model after this much quiet
    max_pause_ms: int = 1500  # smart: end the turn after this much quiet regardless
    turn_threshold: float = 0.5  # smart: "finished" probability that ends the turn
    # Keep the last N utterances (wav + json with transcript and stats) in
    # state/utterances/ for tuning. 0 = off. Audio stays on this machine.
    save_utterances: int = 0


@dataclass
class GateConfig:
    """Local checks every transcript passes before it can reach Claude."""

    min_logprob: float = -1.0  # Whisper confidence floor with the wake word
    min_logprob_followup: float = -0.9  # stricter floor without it
    max_no_speech_prob: float = 0.6
    max_compression_ratio: float = 2.4
    max_words_per_second: float = 6.0  # of VAD-voiced speech
    # A clip of at least coverage_min_seconds with fewer words per second than this
    # was mostly not transcribed (often our own voice mixed in); drop it.
    min_words_per_second: float = 0.5
    coverage_min_seconds: float = 4.0
    followup_min_words: int = 2
    followup_min_snr_db: float = 10.0  # above the room's noise floor
    followup_max_drop_db: float = 12.0  # quieter than your last wake request by more = someone else


@dataclass
class AsrConfig:
    model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "en"
    # Optional context Whisper sees before each clip. Off by default: Whisper spells
    # "Scout" right on its own, and a prompt that IS the start of what's said gets
    # treated as already said and dropped ("Hey Scout." turned "Hey Scout, what
    # time is it?" into "What time is it?", so the request was ignored). If a wake
    # word gets misheard, name it without the greeting, e.g. "Scout".
    prompt: str = ""


@dataclass
class TtsConfig:
    model: str = "models/kokoro-v1.0.onnx"
    voices: str = "models/voices-v1.0.bin"
    voice: str = "af_heart"
    speed: float = 1.1
    chimes: bool = True
    working_sound: bool = True  # soft tick while tools run with nothing to say
    # A long lookup with nothing said yet: a short spoken line ("Checking your mail.")
    progress_speech: bool = True
    progress_delay_s: float = 2.0  # ...after this long
    progress_again_s: float = 12.0  # "Still working on it." this much later (0 = never)


@dataclass
class ClaudeConfig:
    cwd: str = "~"
    model: str = ""  # empty = Claude Code's default
    permission_mode: str = "default"
    # The room's session starts fresh after this many quiet minutes, or once a turn carries
    # this many tokens of context (Scout puts recent spoken items, memories and recall into
    # each request itself). 0 = never.
    session_idle_min: float = 20.0
    session_max_tokens: int = 60000
    # Claude Code settings files the room assistant loads. Empty (the default) keeps it
    # lean: no ~/.claude CLAUDE.md, memory, skills or hooks in every request (about a
    # third fewer tokens a turn, scripts/bench_brain.py); what it needs from
    # ~/.claude/settings.json comes in through inherit_settings below. ["user"] loads
    # all of it, as before (then set plugins = "enabled" too).
    setting_sources: list[str] = field(default_factory=list)
    # With "user" not in setting_sources: these keys of ~/.claude/settings.json still
    # apply to the room (your allow/deny rules, your model and effort). The "settings"
    # approval policy brings your hooks along too.
    inherit_settings: list[str] = field(
        default_factory=lambda: ["permissions", "model", "effortLevel", "alwaysThinkingEnabled"]
    )
    # Claude Code plugins for the room assistant (the Agent SDK doesn't load them by
    # itself): "scout" = Scout's own skills (the default); "enabled" = those plus every
    # plugin enabled in ~/.claude/settings.json; "none". Their tools still ask by voice
    # unless pre-approved.
    plugins: str = "scout"
    confirm_timeout_s: float = 30.0
    # "strict": only auto_allow_* below run without a spoken yes, even if your
    # ~/.claude settings allow more.
    # "settings": your settings' allow rules and hooks apply; you're asked out
    # loud only where Claude Code would normally prompt.
    # "settings_no_hooks": like "settings", but your settings' hooks are off (a
    # hook that answers "allow" would otherwise skip every prompt).
    approval_policy: str = "strict"
    # Under the settings policies, always ask out loud for these (permission rules).
    always_ask: list[str] = field(
        default_factory=lambda: [
            "Bash(git push:*)",
            "Bash(gh pr merge:*)",
            "Bash(gh pr create:*)",
        ]
    )
    # Show the voice session in Remote Control under this name ("" = off).
    remote_control: str = ""
    # Tools that run without asking under the strict policy.
    auto_allow_tools: list[str] = field(
        default_factory=lambda: [
            "Read",
            "Glob",
            "Grep",
            "LS",
            "WebSearch",
            "TodoWrite",
            "ToolSearch",
            "Task",
            "Agent",
            "BashOutput",
            "NotebookRead",
            "WebFetch",
        ]
    )
    # Programs allowed as a single plain Bash command (no pipes, chaining or redirects).
    auto_allow_commands: list[str] = field(
        default_factory=lambda: [
            "date",
            "cal",
            "uptime",
            "whoami",
            "pwd",
            "ls",
            "df",
            "du",
            "which",
            "sw_vers",
        ]
    )
    extra_system_prompt: str = ""
    # Answer everyday requests in plain code first (time, date, timers, volume,
    # play/pause, today's calendar): instant and no tokens. Anything else goes to Claude.
    local_first: bool = True
    # Tier 1: a small local model (MLX) that answers calendar and mail lookups
    # itself, read-only, and hands everything else to Claude. "" turns it off.
    # Download it with scripts/fetch-models.sh (Qwen3 4B, 2.3 GB): it was the only
    # candidate that handled all 13 test lookups with none of 23 "needs Claude"
    # requests kept (scripts/bench_tier1.py), deciding in a median 0.7 s. It also
    # writes the email summaries Claude gets instead of email (privacy.mode).
    local_model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    # Tier 1 also answers the weather, the task list, and questions about what's in
    # Scout's memory ("what did Pat say about the budget", "when did I last change the
    # oil"): searched locally, answered by the local model only from what it found and
    # checked against it in code; anything else goes to Claude (scripts/bench_recall.py).
    local_recall: bool = True
    # How close (cosine, bge-small's scale) a record found by meaning must be for recall
    # to read it; with none that close the model isn't asked and Claude answers.
    # Measured with scripts/eval_retrieval.py --floor.
    recall_min_similarity: float = 0.64
    # Follow-ups to local recall ("what did he say about it?", "and when is that due?")
    # rewritten into a standalone question from what was just said, before the search
    # (followup.py; scripts/bench_followup.py: 20 of 22 synthetic follow-ups answered
    # right locally, against 1 of 22 as asked, none wrong). Needs local_recall.
    local_rewrite: bool = True
    # Prefix each request with context (date and time, running timers, recent local
    # answers) and, for calendar or mail questions, the data itself, so Claude can
    # answer in one pass instead of calling a tool first (each round trip is ~1.5-2.5 s).
    request_context: bool = True


@dataclass
class WebConfig:
    enabled: bool = True
    # Addresses to listen on: "localhost" (which Tailscale HTTPS forwards to), "lan"
    # (this Mac's home-network address), "tailscale" (its tailnet address), or
    # literal IPs. Never all interfaces.
    hosts: list[str] = field(default_factory=lambda: ["localhost", "lan", "tailscale"])
    port: int = 8765
    # Where links to Scout's record pages (/r/<id>) point, e.g. "https://<mac>.<tailnet>.ts.net".
    # Empty: the Tailscale Serve name forwarding to this port, else the tailnet address.
    public_url: str = ""


@dataclass
class FloorConfig:
    hold_seconds: float = 10.0  # how long a session may keep the floor between turns
    room_wait_seconds: float = 15.0  # a wake request waits this long for another session to finish


@dataclass
class CalendarConfig:
    # Working hours: free time is looked for between these ("HH:MM", 24-hour).
    work_start: str = "09:00"
    work_end: str = "17:00"


@dataclass
class BriefingConfig:
    # Speak the briefing (today's calendar, due reminders, unread mail count) every
    # day at this local time, e.g. "07:30". Empty = off; "brief me" works either way.
    at: str = ""


@dataclass
class WeatherConfig:
    # Where "what's the weather" means when no place is named, e.g. "Luther, Michigan".
    home: str = ""


@dataclass
class IndexConfig:
    # Scout's local store and search (state/scout.db): memories, conversations, notes,
    # lists, mail and calendar, searched on this Mac; only the best matches reach Claude.
    semantic: bool = True  # also rank by meaning (a local embedding model, ~70 MB download)
    model: str = "BAAI/bge-small-en-v1.5"
    dims: int = 0  # 0: the model's own size (384); nomic models can be cut, e.g. 256
    unload_after_s: float = 900.0  # free the model (~0.7 GB in use) after this long idle (0: keep it)
    # Index new mail through Mail.app in the background, and older mail lazily as Scout
    # touches it for a task (search results, messages read): fast local mail search.
    mail: bool = True
    background_embed: bool = True  # embed new items ahead of time while idle
    min_similarity: float = 0.6  # a match by meaning alone needs at least this (bge-small's scale)


@dataclass
class ListsConfig:
    # Lists and tasks: "auto" uses the Mac's Reminders when its access works and Scout's
    # own lists otherwise; "reminders" or "scout" to choose one.
    backend: str = "auto"


@dataclass
class NotesConfig:
    # Notes always live in Scout's store (searchable); with apple = true a copy also goes
    # to an Apple Notes folder (asks for Notes automation access once).
    apple: bool = True
    apple_folder: str = "Scout"


@dataclass
class MemoryServiceConfig:
    # scout-memory: the local service holding memories and the search index.
    host: str = "127.0.0.1"
    port: int = 8767


@dataclass
class PulseConfig:
    # A check every few minutes, in plain code, that speaks only when something needs you:
    # an event about to start, a timed reminder coming due, today's overlapping events.
    enabled: bool = True
    every_min: float = 15.0
    event_lead_min: float = 10.0
    quiet_hours: str = "22:00-08:00"  # only logged in these hours


@dataclass
class StarlinkConfig:
    # Read the Starlink dish's local API (no account, no sign-in): status, outages,
    # obstructions, data used. Off unless turned on.
    enabled: bool = False
    address: str = "192.168.100.1:9200"
    announce: bool = True  # say outages, recoveries and new alerts out loud
    announce_outage_s: float = 60.0  # only outages at least this long are announced
    quiet_hours: str = "22:00-08:00"  # announcements are only logged in these hours ("" = never quiet)
    # Never announced or mentioned: is_heating (snow melt), lower_signal_than_predicted
    # (a flat-mounted dish on a roof; the link is still fine).
    ignore_alerts: list[str] = field(default_factory=lambda: ["is_heating", "lower_signal_than_predicted"])
    poll_s: float = 30.0


@dataclass
class ReferenceConfig:
    # The offline reference (src/scout/reference.py, docs/reference.md): Kiwix ZIM archives
    # (Wikipedia, WikiMed, iFixit...) read in place, never copied into Scout's store.
    dir: str = "reference"  # where the .zim files are; relative to Scout's data folder
    # Which archives, by name ("wikipedia_en_all_nopic": its newest copy in dir) or file
    # name. Empty: every .zim in dir.
    archives: list[str] = field(default_factory=list)
    # When Scout answers a question from them itself, with the local model: "offline"
    # (only when the internet is down; otherwise Claude answers, and can search them
    # too), "always", or "never".
    local_answers: str = "offline"


@dataclass
class PhoneConfig:
    # Talk to Scout from the phone app through SignalWire (docs/phone-design.md). Off
    # unless turned on. api_token and signing_key are secrets: keep this file mode 600.
    enabled: bool = False
    space: str = ""  # e.g. "example.signalwire.com"
    project_id: str = ""
    api_token: str = ""
    signing_key: str = ""
    # scout-phone listens here; only Tailscale Funnel reaches it from outside.
    host: str = "127.0.0.1"
    port: int = 8768
    # The Funnel base SignalWire calls, e.g. "https://<mac>.<tailnet>.ts.net:8443". Webhook
    # URLs and request signatures are built on it, so it must be exactly what SignalWire uses.
    public_url: str = ""
    # The agent's address the paired app dials; it is part of each call's signature.
    address: str = ""
    call_skew_s: int = 60  # a call's signed timestamp may be this far from the Mac's clock
    # The SignalWire subscriber the browser test page (/phone) signs in as. SignalWire creates
    # it the first time a token is minted for it; any string works (often an email).
    subscriber: str = "scout-owner"
    # How a phone app pairs (docs/phone-design.md): "tailnet" (a request from your own
    # Tailscale login, through Tailscale Serve, pairs at once; anyone else is asked about in
    # the room, or uses a QR code), "approve" (always asked about in the room), "code"
    # (only a one-time code from `scoutctl phone pair` or the /phone page).
    pair_mode: str = "tailnet"
    # Tailscale logins that count as you, besides this Mac's own (tailscale status).
    owners: list[str] = field(default_factory=list)


@dataclass
class TasksConfig:
    # Scout's task list (docs/tasks-design.md): memory/tasks/, ordered by plain code,
    # filled from meetings and email.
    me: list[str] = field(default_factory=list)  # your names and addresses, to tell your tasks from others'
    mirror: bool = True  # open tasks in Reminders: yours in mirror_list, others' in waiting_list
    mirror_list: str = "Scout"
    waiting_list: str = "Waiting On"
    farm: bool = True  # find tasks in new meetings and email
    farm_every_min: float = 10.0
    backfill_days: float = 14.0  # on the first run, read meetings and mail this far back
    extract_model: str = "claude-haiku-5-5"  # reads meetings for tasks ("" = Claude Code's default)


@dataclass
class GranolaConfig:
    # Granola meeting notes in Scout's local search (src/scout/granola.py): a personal API
    # key from Granola (Settings, Connectors, API keys; Business plan or above). Empty: off.
    api_key: str = ""
    every_min: float = 30.0  # how often to look for new and changed notes


@dataclass
class SlackConfig:
    # Slack messages in Scout's local search and the task farm (src/scout/slack.py,
    # docs/slack.md): a Slack user token (xoxp-...) from an app made in your workspace.
    # Empty: off. The token is only ever sent to slack.com.
    token: str = ""
    every_min: float = 15.0  # how often to look for new messages
    backfill_days: float = 14.0  # the first time, read each conversation this far back
    skip: list[str] = field(default_factory=list)  # channel names (or DM names) never read
    # Live events over Socket Mode (src/scout/slack_events.py): the app's app-level token
    # (xapp-..., scope connections:write). Empty: polling only. Also only sent to slack.com.
    app_token: str = ""
    # While live events are flowing, the poll (the backstop for a sleeping Mac or a
    # dropped connection) runs this often instead of every_min.
    live_every_min: float = 60.0
    # People whose DMs (and group DMs) Scout says out loud, "Luca messaged you on Slack.":
    # names as Slack shows them (a first name matches: "Luca" is "Luca Pradovera") or
    # user ids. Needs app_token. Only who, never what they wrote.
    announce: list[str] = field(default_factory=list)
    # Which DMs are said: "listed" (from the people in announce), "all" (every DM and group
    # DM), or "off". announce_mentions: also say an @-mention of you in a channel
    # ("Luca mentioned you in #launch on Slack.").
    announce_dms: str = "listed"
    announce_mentions: bool = False


@dataclass
class GithubConfig:
    # GitHub issues, pull requests and commits named in Slack, mail, meetings, notes and
    # tasks become records of their own (src/scout/github.py, docs/links.md "GitHub"):
    # title, state, author and assignees, kept fresh. Read-only. The token is `token`
    # when set, else the one the Mac's `gh` CLI is logged in with (`gh auth token`);
    # it's only ever sent to api.github.com, never logged.
    enabled: bool = False
    token: str = ""
    orgs: list[str] = field(default_factory=list)  # only these owners' repos ([] and no repos: all)
    repos: list[str] = field(default_factory=list)  # and/or these "owner/repo"
    every_min: float = 10.0  # how often to fetch new items and refresh the ones due
    open_every_min: float = 30.0  # an open item is checked again this often
    closed_every_days: float = 7.0  # a closed or merged one this rarely (a commit never)


@dataclass
class MessagesConfig:
    # The messages_* tools read iMessage and SMS history through the scout-messages
    # helper (the only program given Full Disk Access). false = the tools refuse.
    enabled: bool = True


@dataclass
class ContactsConfig:
    # Look people up in the Mac's Contacts (read-only) through the scout-contacts helper,
    # which has its own Contacts permission. false = the contacts tools refuse, and
    # Messages and Mail show numbers and addresses as they are.
    enabled: bool = True


@dataclass
class PrivacyConfig:
    # What of your private data Claude sees (src/scout/privacy.py):
    # "strict": no email text (not even summaries of it), calendar notes or memories;
    #   Claude sees senders, subjects and event titles and times, and says when that
    #   isn't enough. Scout itself still summarizes mail aloud with the local model.
    # "balanced": summaries written by the local model instead of email text (Claude
    #   asks for the full text, with mail_read_full, only when a task needs the exact
    #   words); event titles and times without notes or attendees; only memories
    #   that bear on the request.
    # "open": everything, as the tools return it.
    # Summaries need the local model (claude.local_model).
    mode: str = "balanced"


@dataclass
class Config:
    wake: WakeConfig = field(default_factory=WakeConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    web: WebConfig = field(default_factory=WebConfig)
    floor: FloorConfig = field(default_factory=FloorConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    briefing: BriefingConfig = field(default_factory=BriefingConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    contacts: ContactsConfig = field(default_factory=ContactsConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    starlink: StarlinkConfig = field(default_factory=StarlinkConfig)
    reference: ReferenceConfig = field(default_factory=ReferenceConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    granola: GranolaConfig = field(default_factory=GranolaConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    tasks: TasksConfig = field(default_factory=TasksConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    pulse: PulseConfig = field(default_factory=PulseConfig)
    memory_service: MemoryServiceConfig = field(default_factory=MemoryServiceConfig)
    lists: ListsConfig = field(default_factory=ListsConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)

    def path(self, p: str) -> Path:
        """Resolve a config path relative to Scout's data folder."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else DATA / q


def _coerce(key: str, val: Any, cur: Any) -> Any:
    """Check a config value against the default's type, with clear errors."""
    if isinstance(cur, bool):
        if not isinstance(val, bool):
            raise ValueError(f"config {key} must be true or false, not {val!r}")
        return val
    if isinstance(cur, int):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(f"config {key} must be a whole number, not {val!r}")
        return val
    if isinstance(cur, float):
        if isinstance(val, bool) or not isinstance(val, int | float):
            raise ValueError(f"config {key} must be a number, not {val!r}")
        return float(val)
    if isinstance(cur, str):
        if isinstance(val, int) and not isinstance(val, bool) and key.endswith("_device"):
            return str(val)  # a device index
        if not isinstance(val, str):
            raise ValueError(f"config {key} must be a string, not {val!r}")
        return val
    if isinstance(cur, list):
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ValueError(f"config {key} must be a list of strings, not {val!r}")
        return val
    return val


def _merge(obj: Any, data: dict[str, Any], where: str) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        val = data.pop(f.name)
        cur = getattr(obj, f.name)
        if is_dataclass(cur):
            if not isinstance(val, dict):
                raise ValueError(f"config [{where}{f.name}] must be a section, not {val!r}")
            _merge(cur, dict(val), f"{where}{f.name}.")
        else:
            setattr(obj, f.name, _coerce(f"{where}{f.name}", val, cur))
    if data:
        raise ValueError(f"unknown config keys: {', '.join(where + k for k in data)}")


def _quiet_hours_ok(spec: str) -> bool:
    """'HH:MM-HH:MM' (may wrap midnight), or empty for never quiet."""
    if not spec:
        return True
    parts = spec.split("-")
    if len(parts) != 2 or not all(":" in p for p in parts):
        return False
    try:
        for p in parts:
            dt.time.fromisoformat(p.strip())
    except ValueError:
        return False
    return True


def _check(cfg: Config) -> None:
    """Values a section can't use: warn and keep the default (a bad quiet_hours once
    stopped the Starlink watcher for good)."""
    for name, section in (("pulse", cfg.pulse), ("starlink", cfg.starlink)):
        if not _quiet_hours_ok(section.quiet_hours):
            default = type(section)().quiet_hours
            log.warning(
                "config %s.quiet_hours = %r is not HH:MM-HH:MM; using %r",
                name, section.quiet_hours, default,
            )  # fmt: skip
            section.quiet_hours = default
    if cfg.slack.announce_dms not in ("listed", "all", "off"):
        log.warning("config slack.announce_dms = %r is not listed, all or off; using 'listed'",
                    cfg.slack.announce_dms)  # fmt: skip
        cfg.slack.announce_dms = "listed"
    if cfg.reference.local_answers not in ("offline", "always", "never"):
        log.warning("config reference.local_answers = %r is not offline, always or never; using 'offline'",
                    cfg.reference.local_answers)  # fmt: skip
        cfg.reference.local_answers = "offline"


def load(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or DATA / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            _merge(cfg, tomllib.load(fh), "")
    _check(cfg)
    return cfg
