#!/bin/sh
# Install/uninstall a user LaunchAgent that starts scout at login and restarts it if it dies.
#   scripts/launchd.sh install | uninstall | restart | status
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
# The path goes into XML: escape it so a folder like "R&D" can't break the plist.
ROOT_XML=$(printf '%s' "$ROOT" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
LABEL=com.local.scout
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
install)
  mkdir -p "$HOME/Library/LaunchAgents" logs
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT_XML/scripts/run.sh</string></array>
  <key>WorkingDirectory</key><string>$ROOT_XML</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>15</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$ROOT_XML/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT_XML/logs/launchd.err.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "installed $PLIST; logs in $ROOT/logs/"
  ;;
uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  ;;
restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
status) launchctl print "$DOMAIN/$LABEL" | grep -E "state|pid|last exit" ;;
*) echo "usage: $0 install|uninstall|restart|status" >&2; exit 2 ;;
esac
