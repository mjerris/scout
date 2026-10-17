#!/bin/sh
# Download offline reference archives (Kiwix ZIM files) into Scout's data folder
# (reference/), the newest copy of each, checked against Kiwix's published sha256.
# Large: see docs/reference.md for the sizes before choosing.
#   fetch-reference.sh wikipedia_en_all_nopic wikipedia_en_medicine_maxi ifixit_en_all
# A download that stops can be run again: it resumes. Older copies are left for you to
# delete (Scout uses the newest). It installs kiwix-serve too (install-kiwix.sh).
set -eu
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
DEST="$DATA/reference"
MIRROR="${KIWIX_MIRROR:-https://download.kiwix.org/zim}"
[ $# -gt 0 ] || { sed -n '2,7p' "$0" >&2; exit 2; }
mkdir -p "$DEST"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

for name in "$@"; do
  case "$name" in
    *[!a-z0-9_.-]*) echo "not an archive name: $name" >&2; exit 2 ;;
  esac
  file=""
  for dir in "${name%%_*}" other; do  # wikipedia_en_all_nopic is in wikipedia/; the rest in other/
    file=$(curl -fsSL "$MIRROR/$dir/" | grep -o "href=\"${name}_[0-9][0-9][0-9][0-9]-[0-9][0-9][a-z]*\.zim\"" \
      | sed 's/^href="//; s/"$//' | sort | tail -n 1) || file=""
    [ -z "$file" ] || break
  done
  if [ -z "$file" ]; then
    echo "no archive called $name at $MIRROR" >&2
    exit 1
  fi
  if [ -s "$DEST/$file" ]; then
    echo "have $file"
    continue
  fi
  echo "fetching $file"
  curl -fL -C - --progress-bar -o "$DEST/$file.part" "$MIRROR/$dir/$file"
  want=$(curl -fsSL "$MIRROR/$dir/$file.sha256" | cut -d' ' -f1)
  got=$(sha256 "$DEST/$file.part")
  if [ "$got" != "$want" ]; then
    echo "checksum mismatch for $file: got $got, want $want (kept as $file.part)" >&2
    exit 1
  fi
  mv "$DEST/$file.part" "$DEST/$file"
done
# kiwix-serve reads them; Scout starts it (or restarts it with the new archives) within a minute.
"$(dirname "$0")/install-kiwix.sh"
