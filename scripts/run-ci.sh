#!/usr/bin/env bash
# The one gate script: CI runs exactly this, so local and CI can't drift.
#   bash scripts/run-ci.sh          # locally: applies formatting, then checks
#   CI=1 bash scripts/run-ci.sh     # in CI: checks only
set -uo pipefail
cd "$(dirname "$0")/.."
PATHS=(src tests)

lint_gate() { uv run --frozen ruff check "${PATHS[@]}" scripts; }

fmt_gate() {
    if [ -z "${CI:-}" ]; then
        uv run --frozen ruff format -q "${PATHS[@]}"
        git diff --quiet -- "${PATHS[@]}" 2>/dev/null || echo "    (FMT reformatted files in your working tree; review and stage them)"
    fi
    uv run --frozen ruff format --check "${PATHS[@]}"
}

test_gate() { uv run --frozen pytest -q tests; }

results=()
failed=0
for gate in LINT FMT TEST; do
    echo "==> $gate"
    fn="$(echo "$gate" | tr '[:upper:]' '[:lower:]')_gate"
    if "$fn"; then results+=("$gate PASS"); else results+=("$gate FAIL"); failed=1; fi
done
echo
printf '  %s\n' "${results[@]}"
exit $failed
