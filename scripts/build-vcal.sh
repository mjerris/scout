#!/usr/bin/env bash
# Build the calendar and reminders helper (native/vcal) used by those tools.
# Output: bin/vcal in Scout's data folder (macOS ties permissions to this exact path). Works from any directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/native/vcal"
OUT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/bin"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "vcal needs macOS" >&2
    exit 1
fi
if ! command -v swiftc >/dev/null; then
    echo "swiftc not found (install the Xcode command line tools: xcode-select --install)" >&2
    exit 1
fi

mkdir -p "$OUT"
# The embedded Info.plist carries the calendar and reminders usage text macOS shows in its
# prompt; the signing identifier is what Privacy & Security lists the grant under.
# requestFullAccessToEvents/ToReminders need macOS 14.
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos14.0" -module-name vcal \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$SRC/Info.plist" \
    -o "$OUT/vcal.part" "$SRC/main.swift"
codesign --force -s - -i com.local.scout.vcal "$OUT/vcal.part"
mv "$OUT/vcal.part" "$OUT/vcal"
echo "built $OUT/vcal"
