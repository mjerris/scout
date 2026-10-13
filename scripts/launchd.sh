#!/bin/sh
# Install/uninstall a user LaunchAgent that starts scout at login and restarts it if it dies.
#   scripts/launchd.sh install | uninstall | restart | status [memory|phone]
# With "memory": the scout-memory service (memories and local search) instead.
# With "phone": scout-phone (calls from the paired phone app); it stays down after a clean
# exit, which is what it does when [phone] is off or incomplete.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
# The path goes into XML: escape it so a folder like "R&D" can't break the plist.
ROOT_XML=$(printf '%s' "$ROOT" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"  # config, state, logs, models, helpers
# The paths go into XML: escape them so a folder like "R&D" can't break the plist.
DATA_XML=$(printf '%s' "$DATA" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
LABEL=com.local.scout
PROGRAM=run.sh
PROCESS=Interactive
LOG=launchd
KEEPALIVE='<true/>'
if [ "${2:-}" = memory ]; then
  LABEL=com.local.scout.memory
  PROGRAM=run-memory.sh
  PROCESS=Background  # embedding gives way to listening and speaking
  LOG=memory
elif [ "${2:-}" = phone ]; then
  LABEL=com.local.scout.phone
  PROGRAM=run-phone.sh
  PROCESS=Background
  LOG=phone
  KEEPALIVE='<dict><key>SuccessfulExit</key><false/></dict>'
fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
install)
  mkdir -p "$HOME/Library/LaunchAgents" "$DATA/logs"
  NEW="$PLIST.new"
  cat > "$NEW" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT_XML/scripts/$PROGRAM</string></array>
  <key>WorkingDirectory</key><string>$ROOT_XML</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>$KEEPALIVE
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ExitTimeOut</key><integer>15</integer>
  <key>ProcessType</key><string>$PROCESS</string>
  <key>StandardOutPath</key><string>$DATA_XML/logs/$LOG.out.log</string>
  <key>StandardErrorPath</key><string>$DATA_XML/logs/$LOG.err.log</string>
</dict>
</plist>
PLIST
  if cmp -s "$NEW" "$PLIST" && launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    # Same login item (it runs the stable DATA/current link): just restart in place.
    rm -f "$NEW"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "restarted $LABEL; logs in $DATA/logs/"
    exit 0
  fi
  mv "$NEW" "$PLIST"
  # bootout returns before the old process has exited (up to ExitTimeOut); loading
  # again before it's gone fails with "Bootstrap failed: 5: Input/output error".
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 40); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "installed $PLIST; logs in $DATA/logs/"
  ;;
uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  ;;
restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
status) launchctl print "$DOMAIN/$LABEL" | grep -E "state|pid|last exit" ;;
*) echo "usage: $0 install|uninstall|restart|status [memory|phone]" >&2; exit 2 ;;
esac
