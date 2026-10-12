#!/bin/sh
# Foreground run of scout-memory (memories and local search), as its login item does.
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/venv"
exec uv run --frozen scout-memory "$@"
