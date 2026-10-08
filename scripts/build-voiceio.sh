#!/usr/bin/env bash
# Build the macOS voice-processing helper (native/voiceio) used by audio.backend = "apple".
# Output: native/voiceio/build/voiceio. Works from any directory.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/native/voiceio/Sources"
OUT="$ROOT/native/voiceio/build"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "voiceio needs macOS" >&2
    exit 1
fi
if ! command -v swiftc >/dev/null; then
    echo "swiftc not found (install the Xcode command line tools: xcode-select --install)" >&2
    exit 1
fi

mkdir -p "$OUT"
sources=("$SRC"/*.swift)
# Build to a temporary name and rename, so a failed build never leaves a
# half-written binary that helper_available() would consider fresh.
# macOS 14 is the oldest with the ducking control; targeting it keeps the
# binary portable and uses the long-standing (pre-27) AVAudioEngine API.
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos14.0" -module-name voiceio \
    -o "$OUT/voiceio.part" "${sources[@]}"
mv "$OUT/voiceio.part" "$OUT/voiceio"
echo "built $OUT/voiceio"
