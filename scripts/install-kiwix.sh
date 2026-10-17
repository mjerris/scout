#!/bin/sh
# Install kiwix-serve, the program that reads the offline reference's archives
# (docs/reference.md), into Scout's data folder: bin/kiwix-serve. Never system-wide.
# From Kiwix's official kiwix-tools release for macOS on Apple silicon, pinned to one
# version and checked against the sha256 below and the md5 Kiwix publishes next to it.
# Idempotent: an installed copy that matches is left alone.
#   install-kiwix.sh
set -eu
VERSION=3.8.2
NAME="kiwix-tools_macos-arm64-$VERSION"
URL="${KIWIX_TOOLS_MIRROR:-https://download.kiwix.org/release/kiwix-tools}/$NAME.tar.gz"
TAR_SHA256=5c64d43176627e558a117146b02ea36b7da2b0cd3a332cbff075cde05d9585f9
BIN_SHA256=33faa74863fc23765ed88eaeda2096d968ea771e437cfbbb1a1cdc670f42b277  # kiwix-serve inside it
DATA="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
BIN="$DATA/bin"
WORK="$BIN/.kiwix-tools-$VERSION"  # the download, in Scout's own folder

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

if [ -x "$BIN/kiwix-serve" ] && [ "$(sha256 "$BIN/kiwix-serve")" = "$BIN_SHA256" ]; then
  echo "have kiwix-serve $VERSION"
  exit 0
fi
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo "kiwix-serve: this installs the macOS arm64 build only (this is $(uname -s) $(uname -m))" >&2
  exit 1
fi

mkdir -p "$BIN"
rm -rf "$WORK"
mkdir -p "$WORK/x"
trap 'rm -rf "$WORK"' EXIT
echo "fetching $NAME"
curl -fsSL -o "$WORK/$NAME.tar.gz" "$URL"
got=$(sha256 "$WORK/$NAME.tar.gz")
if [ "$got" != "$TAR_SHA256" ]; then
  echo "checksum mismatch for $NAME.tar.gz: got $got, want $TAR_SHA256" >&2
  exit 1
fi
want_md5=$(curl -fsSL "$URL.md5" | cut -d' ' -f1)
got_md5=$(md5 -q "$WORK/$NAME.tar.gz")
if [ "$got_md5" != "$want_md5" ]; then
  echo "md5 mismatch for $NAME.tar.gz: got $got_md5, Kiwix publishes ${want_md5:-nothing}" >&2
  exit 1
fi
tar -xzf "$WORK/$NAME.tar.gz" -C "$WORK/x" "$NAME/kiwix-serve"
got=$(sha256 "$WORK/x/$NAME/kiwix-serve")
if [ "$got" != "$BIN_SHA256" ]; then
  echo "checksum mismatch for kiwix-serve: got $got, want $BIN_SHA256" >&2
  exit 1
fi
chmod 755 "$WORK/x/$NAME/kiwix-serve"
mv -f "$WORK/x/$NAME/kiwix-serve" "$BIN/kiwix-serve"
echo "installed kiwix-serve $VERSION in $BIN"
