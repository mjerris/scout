#!/usr/bin/env bash
# Manage Scout's background app.
#   scoutctl.sh status | restart | logs [N] | uninstall [--purge]
#   scoutctl.sh phone pair | list | forget <device> | calls   (pair: a one-time code + QR for the app; calls: active ones)
#   scoutctl.sh phone pair list | approve <id> | deny <id>   (answer a device asking to pair)
#   scoutctl.sh echo show | reset   (the echo path measured per speaker and mic: echo_cal.py)
#   scoutctl.sh rules list | add "<rule>" [--enforced <code>] [--origin <text>] | drop <words> [--yes] | prompt
#     (Scout's behavior rules, memory/behavior.md: docs/behavior-rules.md)
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
PHONE_LABEL=com.local.scout.phone
PHONE_PLIST="$HOME/Library/LaunchAgents/$PHONE_LABEL.plist"

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
    echo "sync:"  # a line per source: ok, failing, stalled, off or waiting (syncstate.py)
    UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" \
        python -m scout.syncstate 2>/dev/null | sed 's/^/  /' || echo "  unknown (no install yet)"
    UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" \
        python -c "from scout import reference; print(reference.status_line())" 2>/dev/null \
        || echo "reference: not read"
    echo "mcp servers:"  # a line per server in tools.toml: up, failing, auth needed or off (extmcp.py)
    UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" \
        python -m scout.extmcp 2>/dev/null | sed 's/^/  /' || echo "  unknown (no install yet)"
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
echo)
    UV_PROJECT_ENVIRONMENT="$DATA/venv" uv run --frozen --quiet --project "$DATA/current" \
        python -m scout.echo_cal "${2:-show}"
    ;;
phone)
    shift
    UV_PROJECT_ENVIRONMENT="$DATA/venv" exec uv run --frozen --quiet --project "$DATA/current" \
        python -m scout.phone_devices "$@"
    ;;
rules)
    shift
    UV_PROJECT_ENVIRONMENT="$DATA/venv" exec uv run --frozen --quiet --project "$DATA/current" \
        python -m scout.behavior "$@"
    ;;
logs)
    tail -n "${2:-40}" "$DATA/logs/scout.log" "$DATA/logs/install.log" 2>/dev/null
    ;;
uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$DATA/current"
    echo "removed the login item"
    launchctl bootout "$DOMAIN/$PHONE_LABEL" 2>/dev/null || true
    rm -f "$PHONE_PLIST"
    launchctl bootout "$DOMAIN/$MESSAGES_LABEL" 2>/dev/null || true
    rm -f "$MESSAGES_PLIST" "$DATA/state/messages.sock" "$DATA/state/messages_token"
    echo "removed the Messages helper's login item (turn off scout-messages in System Settings, Privacy and Security, Full Disk Access)"
    echo "the Contacts permission stays until you remove scout-contacts in System Settings, Privacy and Security, Contacts"
    if [ "${2:-}" = "--purge" ]; then
        rm -rf "$DATA"
        echo "deleted $DATA"
    else
        echo "kept $DATA (config, rules, models); add --purge to delete it"
    fi
    ;;
*)
    echo "usage: $0 status | restart | logs [N] | uninstall [--purge] | phone pair [list|approve <id>|deny <id>]|list|forget <device>|calls | rules list|add|drop|prompt" >&2
    exit 2
    ;;
esac
