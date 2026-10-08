#!/usr/bin/env bash
# Install or update Scout's background app from a code folder (the plugin's, or a
# checkout). Idempotent; run by the SessionStart hook, or by hand:
#   scripts/install.sh [code-folder]
# Steps: point DATA/current at the code, sync the Python environment, fetch missing
# models, rebuild a native helper only when its source changed (macOS ties calendar
# and microphone permissions to the exact binary), then (re)start the login item.
# Progress goes to DATA/state/install.json, which `scout status` reads.
set -euo pipefail
ROOT="$(cd "${1:-$(dirname "$0")/..}" && pwd -P)"
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
OLD_LABEL=com.local.claude-voice
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p "$DATA/state" "$DATA/logs" "$DATA/bin"

status() {  # status STATE MESSAGE
    printf '{"status": "%s", "message": "%s", "code": "%s", "at": "%s"}\n' \
        "$1" "$2" "$ROOT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$DATA/state/install.json"
    echo "$(date '+%F %T') $1: $2"
}

if ! mkdir "$DATA/.install.lock" 2>/dev/null; then
    echo "another install is running"
    exit 0
fi
trap 'rmdir "$DATA/.install.lock" 2>/dev/null || true' EXIT
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
build_if_changed() {  # NAME SOURCE-DIR
    local name="$1" src="$2" sum
    sum="$( (cd "$src" && find . -type f \( -name '*.swift' -o -name '*.plist' \) -print0 | sort -z | xargs -0 shasum -a 256) | shasum -a 256 | cut -d' ' -f1)"
    if [ -x "$DATA/bin/$name" ] && [ "$(cat "$DATA/bin/$name.source-sha256" 2>/dev/null)" = "$sum" ]; then
        return
    fi
    status installing "building the $name helper"
    "$DATA/current/scripts/build-$name.sh" >/dev/null
    echo "$sum" >"$DATA/bin/$name.source-sha256"
}
if command -v swiftc >/dev/null; then
    build_if_changed vcal "$DATA/current/native/vcal"
    build_if_changed voiceio "$DATA/current/native/voiceio"
else
    echo "swiftc missing: skipping the calendar and Apple voice helpers (xcode-select --install)"
fi

status installing "starting the background app"
"$DATA/current/scripts/launchd.sh" install >/dev/null
status ready "running $(basename "$ROOT")"
