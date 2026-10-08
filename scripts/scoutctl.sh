#!/usr/bin/env bash
# Manage Scout's background app.
#   scoutctl.sh status | restart | logs [N] | uninstall [--purge]
# uninstall stops and removes the login item; --purge also deletes Scout's data
# folder (config, saved rules, models, logs). Removing the plugin itself is
# `claude plugin uninstall scout`.
set -euo pipefail
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
LABEL=com.local.scout
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
MESSAGES_LABEL=com.local.scout.messages
MESSAGES_PLIST="$HOME/Library/LaunchAgents/$MESSAGES_LABEL.plist"

case "${1:-status}" in
status)
    if [ -f "$DATA/state/install.json" ]; then
        echo "setup: $(cat "$DATA/state/install.json")"
    else
        echo "setup: not run yet (it starts with the next Claude session)"
    fi
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        launchctl print "$DOMAIN/$LABEL" | grep -E '^\s+(state|pid|last exit code) =' | sed 's/^\s*/app: /'
    else
        echo "app: not loaded"
    fi
    if [ -s "$DATA/state/web_token" ]; then
        port=$(UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" \
            python -c "from scout.config import load; print(load().web.port)" 2>/dev/null || echo 8765)
        curl -s -m 3 -H "Authorization: Bearer $(cat "$DATA/state/web_token")" \
            "http://127.0.0.1:$port/api/status" | head -c 600 || echo "web: no answer"
        echo
    fi
    if launchctl print "$DOMAIN/$MESSAGES_LABEL" >/dev/null 2>&1; then
        launchctl print "$DOMAIN/$MESSAGES_LABEL" | grep -E '^\s+(state|pid) =' | sed 's/^\s*/messages helper: /'
    else
        echo "messages helper: not loaded"
    fi
    echo "code: $(cd "$DATA/current" 2>/dev/null && pwd -P || echo none)"
    echo "data: $DATA"
    ;;
restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "restarted"
    ;;
logs)
    tail -n "${2:-40}" "$DATA/logs/scout.log" "$DATA/logs/install.log" 2>/dev/null
    ;;
uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$DATA/current"
    echo "removed the login item"
    launchctl bootout "$DOMAIN/$MESSAGES_LABEL" 2>/dev/null || true
    rm -f "$MESSAGES_PLIST" "$DATA/state/messages.sock" "$DATA/state/messages_token"
    echo "removed the Messages helper's login item (turn off scout-messages in System Settings, Privacy and Security, Full Disk Access)"
    if [ "${2:-}" = "--purge" ]; then
        rm -rf "$DATA"
        echo "deleted $DATA"
    else
        echo "kept $DATA (config, rules, models); add --purge to delete it"
    fi
    ;;
*)
    echo "usage: $0 status | restart | logs [N] | uninstall [--purge]" >&2
    exit 2
    ;;
esac
