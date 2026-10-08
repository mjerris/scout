#!/bin/sh
# Foreground run; keeps the Mac awake while the assistant is running.
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec /usr/bin/caffeinate -i -s uv run --frozen claude-voice "$@"
