# shellcheck shell=bash
# Signing for the native helpers; sourced by scripts/build-*.sh and scripts/install.sh.
# macOS keeps a privacy grant (Full Disk Access, Contacts, Calendars, Reminders) for as
# long as the binary still meets the grant's designated requirement. An ad hoc signature's
# requirement is the binary's hash, so every rebuild loses the grants; a certificate's
# requirement is "this identifier, signed by this certificate", which a rebuild keeps.
# The certificate comes from SCOUT_SIGN_IDENTITY, else [helpers] sign_identity in
# config.toml (docs/setup.md); without one the helpers are signed ad hoc.

scout_sign_identity() {  # prints the identity to sign with, "-" for ad hoc
    local id="${SCOUT_SIGN_IDENTITY:-}" data py
    if [ -z "$id" ]; then
        data="${SCOUT_HOME:-$HOME/Library/Application Support/Scout}"
        if [ -f "$data/config.toml" ]; then
            # tomllib needs Python 3.11+: Scout's own environment, else python3 if new enough.
            for py in "$data/venv/bin/python" python3; do
                if id="$("$py" -I -c 'import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh).get("helpers", {}).get("sign_identity", ""))' "$data/config.toml" 2>/dev/null)"; then
                    break
                fi
                id=""
            done
        fi
    fi
    printf '%s\n' "${id:--}"
}

# scout_codesign FILE IDENTIFIER [codesign options...]: sign FILE as IDENTIFIER with the
# configured certificate. A configured certificate that isn't in the keychain is an error,
# not a quiet fall back to ad hoc, which would drop the grants.
scout_codesign() {
    local file="$1" ident="$2" id
    shift 2
    id="$(scout_sign_identity)"
    if [ "$id" = "-" ]; then
        echo "warning: signing $ident ad hoc; its privacy grants won't survive a rebuild (set [helpers] sign_identity, docs/setup.md)" >&2
    elif ! security find-identity -v -p codesigning | grep -qF -e "\"$id\"" -e " $id "; then
        echo "signing identity \"$id\" is not a valid code signing identity in the keychain (security find-identity -v -p codesigning)" >&2
        return 1
    fi
    codesign --force -s "$id" --timestamp=none -i "$ident" "$@" "$file"
}
