#!/bin/sh
# Publish the web page on your tailnet over HTTPS (tailnet-only: not the public internet).
# HTTPS is what lets the page use your phone's microphone for push-to-talk.
#   scripts/tailscale.sh enable | disable | status
set -eu
cd "$(dirname "$0")/.."
TS=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
PORT=$(uv run --frozen python -c "from scout.config import load; print(load().web.port)")
case "${1:-status}" in
enable)
  "$TS" serve --bg --https=443 "http://127.0.0.1:$PORT"
  NAME=$("$TS" status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
  echo "open: https://$NAME/?token=$(cat state/web_token)"
  ;;
disable) "$TS" serve --https=443 off ;;
status) "$TS" serve status ;;
*) echo "usage: $0 enable|disable|status" >&2; exit 2 ;;
esac
