#!/bin/sh
# Foreground run; keeps the Mac awake while the assistant is running.
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# The Python environment lives in Scout's data folder, so it survives plugin updates.
export UV_PROJECT_ENVIRONMENT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/venv"
exec /usr/bin/caffeinate -i -s uv run --frozen scout "$@"
