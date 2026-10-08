#!/bin/sh
# Install a git pre-commit hook that blocks commits failing lint or format.
set -eu
cd "$(dirname "$0")/.."
cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/sh
# claude-voice: lint + format check before every commit (scripts/install-hooks.sh).
cd "$(git rev-parse --show-toplevel)"
if ! uv run --frozen ruff check src tests scripts -q || ! uv run --frozen ruff format --check -q src tests \
    || ! uv run --frozen mypy --no-error-summary; then
    echo "pre-commit: fix lint/format/type errors first (bash scripts/run-ci.sh applies formatting)." >&2
    exit 1
fi
HOOK
chmod +x .git/hooks/pre-commit
echo "installed .git/hooks/pre-commit"
