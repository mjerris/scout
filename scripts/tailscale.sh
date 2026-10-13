#!/bin/sh
# Publish the web page on your tailnet over HTTPS (tailnet-only: not the public internet).
# HTTPS is what lets the page use your phone's microphone for push-to-talk.
#   scripts/tailscale.sh enable | disable | status
# The phone service (scout-phone) is the one thing published to the internet, through
# Funnel on port 8443, so SignalWire's signed webhooks can reach it:
#   scripts/tailscale.sh funnel | funnel-off
set -eu
cd "$(dirname "$0")/.."
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"  # config, state, logs, models, helpers
TS=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
PORT=$(uv run --frozen python -c "from scout.config import load; print(load().web.port)")
case "${1:-status}" in
enable)
  "$TS" serve --bg --https=443 "http://127.0.0.1:$PORT"
  NAME=$("$TS" status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
  echo "open: https://$NAME/?token=$(cat "$DATA/state/web_token")"
  ;;
disable) "$TS" serve --https=443 off ;;
funnel)
  PHONE=$(uv run --frozen python -c "from scout.config import load; print(load().phone.port)")
  "$TS" funnel --bg --https=8443 "http://127.0.0.1:$PHONE"
  NAME=$("$TS" status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
  echo "public: https://$NAME:8443 (set [phone] public_url to this)"
  ;;
funnel-off) "$TS" funnel --https=8443 off ;;
status) "$TS" serve status; "$TS" funnel status ;;
*) echo "usage: $0 enable|disable|status|funnel|funnel-off" >&2; exit 2 ;;
esac
