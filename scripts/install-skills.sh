#!/usr/bin/env bash
# Link this repo's skills (skills/*) into ~/.claude/skills, so the room assistant
# (which loads user settings) and every other Claude Code session can use them.
# Links, not copies: the tracked files here stay the only source. Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.claude/skills"
mkdir -p "$DEST"
for dir in "$ROOT"/skills/*/; do
    name="$(basename "$dir")"
    target="$DEST/$name"
    if [ -L "$target" ]; then
        ln -sfn "${dir%/}" "$target"
    elif [ -e "$target" ]; then
        echo "skip $name: $target exists and isn't a link; move it aside first" >&2
        continue
    else
        ln -s "${dir%/}" "$target"
    fi
    echo "linked $target -> ${dir%/}"
done
