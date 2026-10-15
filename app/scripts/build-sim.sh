#!/usr/bin/env bash
# Build the app for the iOS Simulator (Release: the JS bundle is embedded, so no Metro
# is needed) and print the .app path. Signed ad hoc ("-"), which the Simulator's
# Keychain needs: an unsigned app has no entitlements and every Keychain call fails.
# Build products go in the repo's .tmp/app-build.
#   bash app/scripts/build-sim.sh [Debug|Release]
set -euo pipefail
app="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$app/../.tmp/app-build"
config="${1:-Release}"
[ -d "$app/node_modules" ] || (cd "$app" && npm ci)
[ -d "$app/ios/Pods" ] || (cd "$app" && npx expo prebuild --platform ios --no-install && cd ios && pod install)
mkdir -p "$out"
xcodebuild -workspace "$app/ios/Scout.xcworkspace" -scheme Scout -configuration "$config" \
    -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "$out" ARCHS="$(uname -m)" ONLY_ACTIVE_ARCH=YES CODE_SIGN_IDENTITY=- build > "$out/build.log" 2>&1 \
    || { tail -40 "$out/build.log"; grep -n "error:" "$out/build.log" | head -20; exit 1; }
echo "$out/Build/Products/$config-iphonesimulator/Scout.app"
