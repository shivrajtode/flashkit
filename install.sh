#!/usr/bin/env bash
# FlashKit installer - run it inside Termux:   bash install.sh
# Safe to run again any time. Flags: --update (quick refresh)  --no-plugins  --deps-only
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=full; PLUGINS=1
for a in "$@"; do case "$a" in
  --update) MODE=update ;; --no-plugins) PLUGINS=0 ;; --deps-only) MODE=deps; PLUGINS=0 ;; esac; done
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

case "${PREFIX:-}" in *com.termux*) ;; *) die "This installs FlashKit into Termux - run it inside the Termux app." ;; esac
VERSION="$(cat "$DIR/VERSION" 2>/dev/null || echo '?')"
printf '\033[1mFlashKit %s\033[0m\n' "$VERSION"

step "Packages"
need=(python android-tools git curl termux-api)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ "$MODE" = full ] || [ ${#missing[@]} -gt 0 ]; then
  pkg update -y >/dev/null 2>&1 || warn "pkg update had a problem (continuing - try termux-change-repo if installs fail)"
  if [ ${#missing[@]} -gt 0 ]; then
    pkg install -y "${missing[@]}" || die "Could not install: ${missing[*]}"
  fi
fi
ok "python, adb/fastboot, git, curl, termux-api"
dpkg -s termux-gui-package >/dev/null 2>&1 || pkg install -y termux-gui-package >/dev/null 2>&1 || true

step "Python library (termuxgui)"
if python -c "import termuxgui" 2>/dev/null; then
  [ "$MODE" = full ] && pip install --upgrade termuxgui >/dev/null 2>&1
  ok "termuxgui"
else
  pip install --upgrade termuxgui || die "pip could not install termuxgui"
  ok "termuxgui installed"
fi

if [ "$MODE" != deps ]; then
  step "Storage access"
  if [ -d "$HOME/storage/shared" ]; then ok "already allowed"; else
    termux-setup-storage
    for _ in $(seq 1 30); do [ -d "$HOME/storage/shared" ] && break; sleep 1; done
    [ -d "$HOME/storage/shared" ] && ok "allowed" || warn "Allow storage when Android asks, then run: flashkit setup"
  fi

  step "The flashkit command"
  chmod +x "$DIR/bin/flashkit" "$DIR/install.sh" "$DIR/tools/"*.sh 2>/dev/null
  git -C "$DIR" config core.fileMode false 2>/dev/null
  ln -sf "$DIR/bin/flashkit" "$PREFIX/bin/flashkit" && ok "type  flashkit  to open the app"
fi

if [ "$PLUGINS" = 1 ]; then
  step "Termux:GUI and Termux:API (matched to your Termux's signature)"
  python "$DIR/tools/termux_plugins.py" || warn "Plugins are not all set up - see the messages above, then run: flashkit plugins"
fi

printf '\n\033[1;32mDone.\033[0m  Open the app with:  \033[1mflashkit\033[0m     Update later with:  \033[1mflashkit update\033[0m\n'
