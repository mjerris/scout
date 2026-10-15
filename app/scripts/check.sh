#!/usr/bin/env bash
# The app's gate: lint, typecheck, unit tests. Run from anywhere:
#   bash app/scripts/check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
if [ ! -d node_modules ]; then
    echo "app/node_modules is missing: run (cd app && npm ci) first" >&2
    exit 2
fi
failed=0
echo "==> APP LINT";  npx --no-install eslint . --max-warnings 0 || failed=1
echo "==> APP TYPES"; npx --no-install tsc --noEmit || failed=1
echo "==> APP TEST";  npx --no-install jest --ci --silent || failed=1
exit $failed
