#!/usr/bin/env bash
# Copy the TypeScript sources of @signalwire/react and @signalwire/react-native from a
# checkout of github.com/lpradovera/signalwire-react-native-unofficial into app/vendor/,
# without the tests or the Expo config plugin (Metro and tsc compile the sources; the
# app declares the native peers itself). Then re-apply our patches (app/vendor/patches).
#   bash app/scripts/vendor-signalwire.sh <checkout> [commit]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${1:?usage: vendor-signalwire.sh <checkout of signalwire-react-native-unofficial> [commit]}"
commit="${2:-$(git -C "$src" rev-parse HEAD)}"
git -C "$src" diff --quiet "$commit" -- packages || { echo "checkout differs from $commit; check it out first" >&2; exit 1; }
for pkg in react react-native; do
    dst="$here/vendor/signalwire-$pkg"
    rm -rf "$dst/src"
    mkdir -p "$dst"
    (cd "$src/packages/$pkg" && find src -type f \( -name '*.ts' -o -name '*.tsx' \) \
        ! -name '*.test.ts' ! -name '*.test.tsx' ! -path 'src/plugin/*' -print0 \
        | xargs -0 -I{} rsync -R {} "$dst/")
    cp "$src/LICENSE" "$dst/LICENSE"
done
echo "$commit" > "$here/vendor/UPSTREAM_COMMIT"
for p in "$here"/vendor/patches/*.patch; do
    [ -e "$p" ] || continue
    # Paths in our patches are relative to app/vendor (a/signalwire-react-native/src/...).
    patch -s -d "$here/vendor" -p1 < "$p"
    echo "applied $(basename "$p")"
done
echo "vendored $commit"
