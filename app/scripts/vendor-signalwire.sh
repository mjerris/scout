#!/usr/bin/env bash
# Vendor @signalwire/react and @signalwire/react-native from a checkout of
# github.com/lpradovera/signalwire-react-native-unofficial: build both packages and put
# the tarballs `npm pack` makes (what npm will publish: built dist, the iOS and Android
# native code, the podspec and the Expo config plugin) in app/vendor/, with the commit
# in vendor/UPSTREAM_COMMIT. package.json installs them as file: tarballs, so they land
# in node_modules as ordinary copies, the same as the eventual npm packages.
#   bash app/scripts/vendor-signalwire.sh <checkout> [commit]          # vendor
#   bash app/scripts/vendor-signalwire.sh --check <checkout>           # rebuild, compare
# The build is reproducible (npm pack fixes file times), so --check proves the tarballs
# are that commit's packages. After a re-vendor: `npm install` (package-lock records
# the new integrity), compare the wrapper's patches/@signalwire+js+*.patch and
# docs/patches/react-native-incall-manager+*.patch with app/patches/, then rebuild
# the app (native code).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
check=0
if [ "${1:-}" = "--check" ]; then check=1; shift; fi
src="${1:?usage: vendor-signalwire.sh [--check] <checkout of signalwire-react-native-unofficial> [commit]}"
if [ $check = 1 ]; then
    commit="$(cat "$here/vendor/UPSTREAM_COMMIT")"
else
    commit="$(git -C "$src" rev-parse "${2:-HEAD}^{commit}")"
fi
[ "$(git -C "$src" rev-parse HEAD)" = "$(git -C "$src" rev-parse "$commit^{commit}")" ] \
    || { echo "checkout is not at $commit; check it out first" >&2; exit 1; }
[ -z "$(git -C "$src" status --porcelain -- packages package.json package-lock.json)" ] \
    || { echo "checkout has local changes under packages/; vendor a clean commit" >&2; exit 1; }
[ -d "$src/node_modules" ] || (cd "$src" && npm ci --no-audit --no-fund)
(cd "$src" && npm run build -w @signalwire/react -w @signalwire/react-native >/dev/null)
mkdir -p "$here/../.tmp"
out="$(mktemp -d "$here/../.tmp/vendor-pack.XXXXXX")"
trap 'rm -rf "$out"' EXIT
(cd "$src" && npm pack -w @signalwire/react -w @signalwire/react-native --pack-destination "$out" >/dev/null 2>&1)
if [ $check = 1 ]; then
    bad=0
    for f in "$out"/*.tgz; do
        cmp -s "$f" "$here/vendor/$(basename "$f")" || { echo "differs: $(basename "$f")" >&2; bad=1; }
    done
    [ $bad = 0 ] && echo "vendor/ matches $commit"
    exit $bad
fi
rm -f "$here"/vendor/signalwire-react-*.tgz
cp "$out"/*.tgz "$here/vendor/"
echo "$commit" > "$here/vendor/UPSTREAM_COMMIT"
ls "$here/vendor"/*.tgz
echo "vendored $commit"
