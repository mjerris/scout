#!/usr/bin/env bash
# Build the Messages helper (native/messages) used by the messages_* tools.
# Output: bin/scout-messages in Scout's data folder. Full Disk Access is granted to
# this exact binary, and an ad hoc signature changes with every build, so the
# installer rebuilds only when the source changed. Works from any directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/native/messages"
OUT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/bin"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "scout-messages needs macOS" >&2
    exit 1
fi
if ! command -v swiftc >/dev/null; then
    echo "swiftc not found (install the Xcode command line tools: xcode-select --install)" >&2
    exit 1
fi

mkdir -p "$OUT"
# The embedded Info.plist carries the Contacts usage text; the signing identifier
# is what Privacy & Security lists the grants under. The hardened runtime makes
# dyld ignore DYLD_* variables, so nothing can be injected into the process that
# holds Full Disk Access; reading Contacts under it needs the entitlement.
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos14.0" -module-name scout_messages \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$SRC/Info.plist" \
    -o "$OUT/scout-messages.part" "$SRC/main.swift"
codesign --force -s - -o runtime --entitlements "$SRC/entitlements.plist" \
    -i com.local.scout.messages "$OUT/scout-messages.part"
mv "$OUT/scout-messages.part" "$OUT/scout-messages"
echo "built $OUT/scout-messages"
