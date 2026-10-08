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

results=()
failed=0
record() {
    if [ "$2" -eq 0 ]; then results+=("$1 PASS"); else results+=("$1 FAIL"); failed=1; fi
}
echo "==> LINT";  lint_gate;  record LINT $?
echo "==> FMT";   fmt_gate;   record FMT $?
echo "==> TYPES"; types_gate; record TYPES $?
echo "==> SHELL"; shell_gate; record SHELL $?
echo "==> TEST";  test_gate;  record TEST $?
echo
printf '  %s\n' "${results[@]}"
exit $failed
