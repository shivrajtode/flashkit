#!/usr/bin/env bash
# One-line bootstrap:  curl -fsSL https://raw.githubusercontent.com/shivrajtode/flashkit/main/get.sh | bash
set -e
REPO="shivrajtode/flashkit"
DEST="${FLASHKIT_DIR:-$HOME/flashkit}"
command -v git >/dev/null 2>&1 || pkg install -y git
if [ -d "$DEST/.git" ]; then git -C "$DEST" pull --ff-only; else git clone --depth 1 "https://github.com/$REPO.git" "$DEST"; fi
exec bash "$DEST/install.sh" "$@"
