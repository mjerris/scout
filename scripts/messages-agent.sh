#!/usr/bin/env bash
# The Messages helper's own login item (com.local.scout.messages). launchd starts
# DATA/bin/scout-messages directly, so macOS checks Full Disk Access against that
# binary alone: Scout's Python, Claude and the terminal never need it.
#   scripts/messages-agent.sh install [--restart] | uninstall | restart | status
# install is a no-op when the login item is already loaded and unchanged, unless
# --restart (the installer passes it after rebuilding the helper).
set -euo pipefail
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
# The path goes into XML: escape it so a folder like "R&D" can't break the plist.
DATA_XML=$(printf '%s' "$DATA" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
LABEL=com.local.scout.messages
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
HELPER="$DATA/bin/scout-messages"

case "${1:-}" in
install)
    if [ ! -x "$HELPER" ]; then
        echo "no $HELPER; build it with scripts/build-messages.sh" >&2
        exit 1
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$DATA/logs" "$DATA/state"
    NEW="$PLIST.new"
    cat >"$NEW" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DATA_XML/bin/scout-messages</string><string>serve</string>
    <string>--home</string><string>$DATA_XML</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$DATA_XML/logs/messages-helper.log</string>
</dict>
</plist>
PLIST
    if cmp -s "$NEW" "$PLIST" && launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        rm -f "$NEW"
        if [ "${2:-}" = "--restart" ]; then
            launchctl kickstart -k "$DOMAIN/$LABEL"
            echo "restarted $LABEL"
        fi
        exit 0
    fi
    mv "$NEW" "$PLIST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    for _ in $(seq 1 20); do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed $PLIST"
    ;;
uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$DATA/state/messages.sock" "$DATA/state/messages_token"
    echo "removed $LABEL"
    ;;
restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
status) launchctl print "$DOMAIN/$LABEL" | grep -E '^\s+(state|pid|last exit code) =' ;;
*)
    echo "usage: $0 install [--restart] | uninstall | restart | status" >&2
    exit 2
    ;;
esac
