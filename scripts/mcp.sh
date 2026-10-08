#!/usr/bin/env bash
# Start Scout's MCP server (stdio) for a Claude session: discuss, voice_status, mail
# and calendar. It only forwards calls to the running app, which does the work.
set -euo pipefail
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
# The installed version (DATA/current) once setup has run; this plugin's own folder before that.
if [ -d "$DATA/current" ]; then
    CODE="$DATA/current"
else
    CODE="$(cd "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}" && pwd -P)"
fi
export UV_PROJECT_ENVIRONMENT="$DATA/venv"
exec uv run --frozen --quiet --project "$CODE" python -m scout.mcp_server
