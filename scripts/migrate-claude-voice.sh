#!/usr/bin/env bash
# One-time move from a claude-voice checkout to Scout's data folder.
#   scripts/migrate-claude-voice.sh [~/src/claude-voice]
# Copies config.toml, pronounce.txt and state/ (saved rules, the web token, change
# requests) over what's there, since the old app is the source until it is retired;
# models/ only where missing. Safe to re-run. The old checkout is left as it is.
set -euo pipefail
OLD="${1:-$HOME/src/claude-voice}"
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
if [ ! -d "$OLD" ]; then
    echo "no claude-voice checkout at $OLD" >&2
    exit 1
fi
mkdir -p "$DATA/state" "$DATA/models" "$DATA/logs"
for f in config.toml pronounce.txt; do
    if [ -f "$OLD/$f" ]; then
        cp -p "$OLD/$f" "$DATA/$f"
        echo "copied $f"
    fi
done
if [ -d "$OLD/state" ]; then
    # rsync keeps file modes (the web token is private) and copies utterance recordings too.
    rsync -a "$OLD/state/" "$DATA/state/"
    echo "copied state/"
fi
if [ -d "$OLD/models" ]; then
    rsync -a --ignore-existing "$OLD/models/" "$DATA/models/"
    echo "copied models/ (existing files kept)"
fi
echo "Scout data folder: $DATA"
