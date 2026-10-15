#!/usr/bin/env bash
# Install or update Scout's background app from a code folder (the plugin's, or a
# checkout). Idempotent; run by the SessionStart hook, or by hand:
#   scripts/install.sh [code-folder]
# Steps: point DATA/current at the code, sync the Python environment, fetch missing
# models, rebuild a native helper only when its source changed (macOS ties calendar
# and microphone permissions to the exact binary), then (re)start the login item.
# Progress goes to DATA/state/install.json, which `scout status` reads.
set -Eeuo pipefail  # -E: the ERR trap fires in functions too (a failed helper build)
ROOT="$(cd "${1:-$(dirname "$0")/..}" && pwd -P)"
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
OLD_LABEL=com.local.claude-voice
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p "$DATA/state" "$DATA/logs" "$DATA/bin" "$DATA/reference"  # reference: Kiwix .zim files (docs/reference.md)

status() {  # status STATE MESSAGE
    printf '{"status": "%s", "message": "%s", "code": "%s", "at": "%s"}\n' \
        "$1" "$2" "$ROOT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$DATA/state/install.json"
    echo "$(date '+%F %T') $1: $2"
}

# The lock holds the installer's PID, so one killed mid-way (reboot, power loss)
# doesn't block every later install: a lock whose process is gone is taken over.
LOCK="$DATA/.install.lock"
take_lock() {
    local pid
    if mkdir "$LOCK" 2>/dev/null; then echo $$ >"$LOCK/pid"; return 0; fi
    pid="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 1; fi
    # No PID yet: another installer may have just taken it; give it a minute.
    if [ -z "$pid" ] && [ -z "$(find "$LOCK" -maxdepth 0 -mmin +1)" ]; then return 1; fi
    echo "clearing a stale install lock${pid:+ (process $pid is gone)}"
    rm -rf "$LOCK"
    if mkdir "$LOCK" 2>/dev/null; then echo $$ >"$LOCK/pid"; return 0; fi
    return 1
}
if ! take_lock; then
    echo "another install is running"
    exit 0
fi
trap 'rm -rf "$LOCK"' EXIT
trap 'status error "setup failed; see logs/install.log"' ERR

if launchctl print "gui/$(id -u)/$OLD_LABEL" >/dev/null 2>&1; then
    status blocked "the old claude-voice app is running and holds the mic and port; retire it first (scripts/launchd.sh uninstall in that checkout)"
    exit 0
fi
if ! command -v uv >/dev/null; then
    status error "uv is not installed (https://docs.astral.sh/uv/)"
    exit 1
fi

status installing "linking this version"
ln -sfn "$ROOT" "$DATA/current"

status installing "setting up the Python environment"
UV_PROJECT_ENVIRONMENT="$DATA/venv" uv sync --frozen --project "$DATA/current" --quiet

status installing "fetching models (first time: about 350 MB)"
"$DATA/current/scripts/fetch-models.sh" >/dev/null

# Rebuild a helper only when its source changed, so macOS keeps its permissions.
BUILT=""
build_if_changed() {  # NAME SOURCE-DIR [BINARY-NAME]
    local name="$1" src="$2" bin="${3:-$1}" sum
    sum="$( (cd "$src" && find . -type f \( -name '*.swift' -o -name '*.plist' \) -print0 | sort -z | xargs -0 shasum -a 256) | shasum -a 256 | cut -d' ' -f1)"
    if [ -x "$DATA/bin/$bin" ] && [ "$(cat "$DATA/bin/$name.source-sha256" 2>/dev/null)" = "$sum" ]; then
        return
    fi
    status installing "building the $name helper"
    "$DATA/current/scripts/build-$name.sh" >/dev/null
    echo "$sum" >"$DATA/bin/$name.source-sha256"
    BUILT="$BUILT $name"
}
if command -v swiftc >/dev/null; then
    build_if_changed vcal "$DATA/current/native/vcal"
    build_if_changed voiceio "$DATA/current/native/voiceio"
    # A rebuilt Messages helper is a new binary to macOS: Full Disk Access must be
    # granted again (Scout walks the user through it on the next messages question).
    build_if_changed messages "$DATA/current/native/messages" scout-messages
    # Contacts: its own permission, asked for on the first contacts question (docs/contacts.md).
    build_if_changed contacts "$DATA/current/native/contacts" scout-contacts
else
    echo "swiftc missing: skipping the calendar, messages, contacts and Apple voice helpers (xcode-select --install)"
fi
if [ -x "$DATA/bin/scout-messages" ]; then
    status installing "starting the Messages helper"
    restart=""
    case " $BUILT " in *" messages "*) restart=--restart ;; esac
    "$DATA/current/scripts/messages-agent.sh" install ${restart:+"$restart"} >/dev/null ||
        echo "the Messages helper's login item didn't start; see logs/messages-helper.log"
fi

status installing "starting the memory service"
"$DATA/current/scripts/launchd.sh" install memory >/dev/null
# scout-phone only when the phone is turned on ([phone] enabled = true); otherwise its login
# item is removed, so turning the phone off and reinstalling stops it.
if UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" python -c \
    "from scout.config import load; raise SystemExit(0 if load().phone.enabled else 1)" 2>/dev/null; then
    status installing "starting the phone service"
    "$DATA/current/scripts/launchd.sh" install phone >/dev/null
else
    "$DATA/current/scripts/launchd.sh" uninstall phone >/dev/null
fi
status installing "starting the background app"
"$DATA/current/scripts/launchd.sh" install >/dev/null
status ready "running $(basename "$ROOT")"
