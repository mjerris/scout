#!/usr/bin/env bash
# The plugin's SessionStart hook: make sure Scout's background app is installed,
# running this plugin version, and kept alive by launchd. The usual case (already
# current and loaded) returns in milliseconds; setup itself runs in the background
# (scripts/install.sh), so a session never waits for it.
set -u
[ -n "${SCOUT_ROOM:-}" ] && exit 0          # the room assistant's own Claude sessions
[ "$(uname -s)" = Darwin ] || exit 0
ROOT="$(cd "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}" && pwd -P)"
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
LABEL=com.local.scout

current="$(cd "$DATA/current" 2>/dev/null && pwd -P || true)"
if [ "$current" = "$ROOT" ] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    exit 0
fi
if launchctl print "gui/$(id -u)/com.local.claude-voice" >/dev/null 2>&1; then
    echo "Scout is installed but waiting: the old claude-voice app still holds the microphone (retire it to switch over)."
    exit 0
fi
if [ -d "$DATA/.install.lock" ]; then
    exit 0                                    # setup already under way
fi
mkdir -p "$DATA/logs"
nohup "$ROOT/scripts/install.sh" "$ROOT" >>"$DATA/logs/install.log" 2>&1 </dev/null &
echo "Scout is setting up its background app (progress: scout status; log: $DATA/logs/install.log)."
exit 0
