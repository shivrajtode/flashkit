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
need=(python android-tools git curl termux-api dialog)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ "$MODE" = full ] || [ ${#missing[@]} -gt 0 ]; then
  pkg update -y >/dev/null 2>&1 || warn "pkg update had a problem (continuing - try termux-change-repo if installs fail)"
  if [ ${#missing[@]} -gt 0 ]; then
    pkg install -y "${missing[@]}" || die "Could not install: ${missing[*]}"
  fi
fi
ok "python, adb/fastboot, git, curl, termux-api, dialog"
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
  ln -sf "$DIR/bin/flashkit" "$PREFIX/bin/flashkit" && ok "type  flashkit  to open the FlashKit menu"

  step "Shortcuts"
  if [ ! -e "$PREFIX/bin/start" ] || grep -qs 'flashkit start' "$PREFIX/bin/start"; then
    cat > "$PREFIX/bin/start" <<'START'
#!/data/data/com.termux/files/usr/bin/bash
# `start flashkit` -> opens the FlashKit app (written by the FlashKit installer)
case "${1:-}" in flashkit|FlashKit) shift; exec flashkit start "$@" ;; *) echo "usage: start flashkit"; exit 2 ;; esac
START
    chmod +x "$PREFIX/bin/start"; ok "start flashkit"
  else
    warn "another 'start' command already exists - use:  flashkit start"
  fi
  mkdir -p "$HOME/.shortcuts" && printf '#!/data/data/com.termux/files/usr/bin/bash\nexec flashkit start\n' > "$HOME/.shortcuts/FlashKit" \
    && chmod +x "$HOME/.shortcuts/FlashKit" && ok "'FlashKit' entry for the Termux:Widget home-screen widget"
  bash "$DIR/bin/flashkit" hook && ok "after a Termux restart the FlashKit menu opens by itself (once)"
fi

if [ "$PLUGINS" = 1 ]; then
  step "Termux:GUI and Termux:API (matched to your Termux's signature)"
  mkdir -p "$HOME/.flashkit"; touch "$HOME/.flashkit/open-next"   # if Android restarts Termux while installing, the next session opens FlashKit
  python "$DIR/tools/termux_plugins.py" || warn "Plugins are not all set up - see the messages above, then run: flashkit plugins"
  rm -f "$HOME/.flashkit/open-next"
fi

printf '\n\033[1;32mDone.\033[0m  Open FlashKit with:  \033[1mflashkit\033[0m  (or  start flashkit)     Update later with:  \033[1mflashkit update\033[0m\n'
if [ "$MODE" = full ] && [ -t 0 ] && [ -t 1 ] && [ -z "${FLASHKIT_NO_OPEN:-}" ]; then
  printf 'Opening FlashKit...\n'; sleep 2; exec "$PREFIX/bin/flashkit"
fi
