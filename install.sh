#!/bin/sh
# o2switch-cli installer.
#
#   curl -fsSL https://raw.githubusercontent.com/pyxl-dev/o2switch-cli/main/install.sh | sh
#   ./install.sh               # from a clone: installs that clone
#   ./install.sh --uninstall
#
# Puts the code in ~/.local/share/o2switch-cli (or uses this clone) and links
# the CLI into ~/.local/bin. Re-run it to update. Nothing outside your home
# folder is touched.
set -eu

REPO="${O2SWITCH_CLI_REPO:-https://github.com/pyxl-dev/o2switch-cli.git}"
DEST="${O2SWITCH_CLI_HOME:-$HOME/.local/share/o2switch-cli}"
BIN="$HOME/.local/bin"

if [ -t 1 ]; then B=$(printf '\033[1m'); G=$(printf '\033[32m'); Y=$(printf '\033[33m'); R=$(printf '\033[31m'); N=$(printf '\033[0m'); else B= G= Y= R= N=; fi
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '%s✗%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  if [ -L "$BIN/o2switch" ]; then rm -f "$BIN/o2switch"; ok "removed $BIN/o2switch"; else warn "$BIN/o2switch not found"; fi
  if [ -d "$DEST/.git" ]; then rm -rf "$DEST"; ok "removed $DEST"; fi
  echo "Left in place: ~/Library/Application Support/o2switch-cli (config and DNS backups) and the Keychain item o2switch-cli.cpanel."
  exit 0
fi

printf '%so2switch-cli%s installer\n\n' "$B" "$N"
[ "$(uname -s)" = "Darwin" ] || die "macOS only (the API token is stored in the macOS Keychain)."

# 1. Code: this clone, or a managed one in ~/.local/share.
HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [ -n "$HERE" ] && [ -f "$HERE/o2switch.py" ] && [ -f "$HERE/api-index.json" ]; then
  DEST="$HERE"
  ok "using this clone: $DEST"
elif [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only -q || die "could not update $DEST (local changes?)"
  ok "updated $DEST"
else
  command -v git >/dev/null || die "git is missing: run  xcode-select --install"
  mkdir -p "$(dirname "$DEST")"
  git clone -q --depth 1 "$REPO" "$DEST" || die "could not download $REPO"
  ok "downloaded to $DEST"
fi

# 2. Requirements.
command -v python3 >/dev/null || die "python3 is missing: run  xcode-select --install"
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
else die "Python 3.9 or newer is required."; fi
command -v curl >/dev/null || die "curl is missing (it carries the TLS 1.3 requests to cPanel)."
ok "curl found"
if command -v dig >/dev/null; then ok "dig found"
else warn "dig not found: the check against public DNS after a DNS write is skipped."; fi

# 3. CLI on the PATH.
mkdir -p "$BIN"
chmod +x "$DEST/o2switch.py"
ln -sf "$DEST/o2switch.py" "$BIN/o2switch"
ok "linked $BIN/o2switch"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not on your PATH: add  export PATH=\"\$HOME/.local/bin:\$PATH\"  to ~/.zshrc";; esac

printf '\n%sDone.%s Next:  o2switch configure   (hostname, cPanel user, API token), then  o2switch whoami\n' "$B" "$N"
echo "The token is created in cPanel: Security, then Manage API Tokens."
