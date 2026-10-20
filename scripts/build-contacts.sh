#!/usr/bin/env bash
# Build the Contacts helper (native/contacts) used by the contacts tools.
# Output: bin/scout-contacts in Scout's data folder (macOS ties the Contacts permission to
# this exact binary, so the installer rebuilds only when the source changed). Works from
# any directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/native/contacts"
. "$ROOT/scripts/sign.sh"
OUT="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}/bin"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "scout-contacts needs macOS" >&2
    exit 1
fi
if ! command -v swiftc >/dev/null; then
    echo "swiftc not found (install the Xcode command line tools: xcode-select --install)" >&2
    exit 1
fi

mkdir -p "$OUT"
# The embedded Info.plist carries the Contacts usage text macOS shows in its prompt; the
# signing identifier is what Privacy & Security lists the grant under. The hardened
# runtime makes dyld ignore DYLD_* variables, so nothing can be injected into the process
# that holds the grant; reading Contacts under it needs the entitlement.
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos14.0" -module-name scout_contacts \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$SRC/Info.plist" \
    -o "$OUT/scout-contacts.part" "$SRC/main.swift"
scout_codesign "$OUT/scout-contacts.part" com.local.scout.contacts -o runtime --entitlements "$SRC/entitlements.plist"
mv "$OUT/scout-contacts.part" "$OUT/scout-contacts"
echo "built $OUT/scout-contacts"
