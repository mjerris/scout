#!/bin/sh
# Foreground run of scout-phone (calls from the paired phone app, via SignalWire), as its
# login item does. It starts only with [phone] enabled = true, signing_key and public_url set.
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/venv"
exec uv run --frozen scout-phone "$@"
