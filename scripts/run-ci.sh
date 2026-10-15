#!/usr/bin/env bash
# The one gate script: CI runs exactly this, so local and CI can't drift.
#   bash scripts/run-ci.sh          # locally: applies formatting, then checks
#   CI=1 bash scripts/run-ci.sh     # in CI: checks only
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PATHS=(src tests)

lint_gate() { uv run --frozen ruff check "${PATHS[@]}"; }

fmt_gate() {
    if [ -z "${CI:-}" ]; then
        uv run --frozen ruff format -q "${PATHS[@]}"
        git diff --quiet -- "${PATHS[@]}" 2>/dev/null || echo "    (FMT reformatted files in your working tree; review and stage them)"
    fi
    uv run --frozen ruff format --check "${PATHS[@]}"
}

types_gate() { uv run --frozen mypy; }  # strict; config in pyproject.toml

shell_gate() {
    if ! command -v shellcheck >/dev/null; then
        echo "    shellcheck not installed (brew install shellcheck)"
        return 1
    fi
    shellcheck scripts/*.sh
}

test_gate() { uv run --frozen pytest -q tests; }

# The phone app (app/): lint, typecheck and unit tests, only when app/ differs from main
# (committed or not), and only where its node_modules is installed (cd app && npm ci).
# Returns 3 when skipped.
app_gate() {
    if [ ! -d app/node_modules ]; then
        echo "    APP skipped: app/node_modules is not installed (cd app && npm ci to run it)"
        return 3
    fi
    local base
    base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true)"
    if [ -n "$base" ] && git diff --quiet "$base" -- app && [ -z "$(git ls-files --others --exclude-standard app)" ]; then
        echo "    APP skipped: nothing in app/ changed from main"
        return 3
    fi
    bash app/scripts/check.sh && shellcheck app/scripts/*.sh
}

results=()
failed=0
record() {
    if [ "$2" -eq 0 ]; then results+=("$1 PASS")
    elif [ "$2" -eq 3 ] && [ "$1" = APP ]; then results+=("$1 SKIP")
    else results+=("$1 FAIL"); failed=1; fi
}
echo "==> LINT";  lint_gate;  record LINT $?
echo "==> FMT";   fmt_gate;   record FMT $?
echo "==> TYPES"; types_gate; record TYPES $?
echo "==> SHELL"; shell_gate; record SHELL $?
echo "==> TEST";  test_gate;  record TEST $?
echo "==> APP";   app_gate;   record APP $?
echo
printf '  %s\n' "${results[@]}"
exit $failed
