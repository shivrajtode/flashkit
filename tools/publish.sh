#!/usr/bin/env bash
# Publish this folder to GitHub: creates the repo if needed, commits, pushes.
#   cd ~/flashkit-upload && unzip -o "$(ls -t ~/storage/downloads/flashkit-repo*.zip | head -n 1)" && cd flashkit && bash tools/publish.sh
# Optional: GH_USER=you  REPO_NAME=flashkit  BRANCH=main  FORCE=1 (force-push)  FLASHKIT_REMOTE=<url>
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
REPO_NAME="${REPO_NAME:-flashkit}"; BRANCH="${BRANCH:-main}"
say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }
first() { for v in "$@"; do [ -n "$v" ] && { printf '%s' "$v"; return; }; done; }

detect_user() {
  local u=""
  [ -n "${GH_USER:-}" ] && { printf '%s' "$GH_USER"; return; }
  [ -d .git ] && u="$(git remote get-url origin 2>/dev/null | sed -n 's#.*github\.com[:/]\([^/]*\)/.*#\1#p')"
  [ -z "$u" ] && command -v gh >/dev/null 2>&1 && u="$(gh api user -q .login 2>/dev/null)"
  [ -z "$u" ] && u="$(git config --global github.user 2>/dev/null)"
  [ -z "$u" ] && [ -f "$HOME/.git-credentials" ] && u="$(sed -n 's#^https://\([^:@/]*\)[:@].*github\.com.*#\1#p' "$HOME/.git-credentials" | head -n1)"
  [ -z "$u" ] && [ -f "$HOME/.config/gh/hosts.yml" ] && u="$(sed -n 's/^ *user: *//p' "$HOME/.config/gh/hosts.yml" | head -n1)"
  [ -z "$u" ] && [ -t 0 ] && { read -r -p "Your GitHub username: " u; }
  printf '%s' "$u"
}
USER_NAME="$(detect_user)"
[ -n "$USER_NAME" ] || die "Couldn't tell your GitHub username. Run again with:  GH_USER=yourname bash tools/publish.sh"
say "Publishing as $USER_NAME/$REPO_NAME"

for f in README.md docs/GUIDE.md get.sh tools/repo.conf .github/ISSUE_TEMPLATE/config.yml; do [ -f "$f" ] && sed -i "s#__GH_REPO__#$USER_NAME/$REPO_NAME#g" "$f"; done

[ -d .git ] || { git init -q -b "$BRANCH" 2>/dev/null || { git init -q && git checkout -q -b "$BRANCH"; }; }
git checkout -q "$BRANCH" 2>/dev/null || git checkout -q -b "$BRANCH"
[ -n "$(git config user.email)" ] || git config user.email "$USER_NAME@users.noreply.github.com"
[ -n "$(git config user.name)" ] || git config user.name "$USER_NAME"
REMOTE="${FLASHKIT_REMOTE:-https://github.com/$USER_NAME/$REPO_NAME.git}"
if git remote get-url origin >/dev/null 2>&1; then git remote set-url origin "$REMOTE"; else git remote add origin "$REMOTE"; fi

ensure_repo() {
  git ls-remote origin >/dev/null 2>&1 && return 0
  say "Creating the repository on GitHub"
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh repo create "$USER_NAME/$REPO_NAME" --public --description "Flash a second Android device over OTG from a rooted phone (Termux:GUI)" >/dev/null 2>&1
  elif [ -f "$HOME/.git-credentials" ]; then
    tok="$(sed -n 's#^https://[^:]*:\([^@]*\)@github\.com.*#\1#p' "$HOME/.git-credentials" | head -n1)"
    [ -n "$tok" ] && curl -fsS -H "Authorization: token $tok" https://api.github.com/user/repos \
      -d "{\"name\":\"$REPO_NAME\",\"description\":\"Flash a second Android device over OTG from a rooted phone (Termux:GUI)\"}" >/dev/null 2>&1
  fi
  git ls-remote origin >/dev/null 2>&1 || die "The repository doesn't exist yet and I couldn't create it. Create an empty PUBLIC repo named '$REPO_NAME' on github.com, then run this again."
}
[ -n "${FLASHKIT_REMOTE:-}" ] || ensure_repo

VERSION="$(cat VERSION 2>/dev/null || echo dev)"
git add -A
git update-index --chmod=+x bin/flashkit install.sh get.sh tools/publish.sh tools/termux_plugins.py 2>/dev/null
if git diff --cached --quiet; then say "Nothing changed since the last publish."; else git commit -q -m "FlashKit $VERSION" && say "Committed FlashKit $VERSION"; fi
if [ -n "${FORCE:-}" ]; then
  git push -u -f origin "$BRANCH" || die "Push failed."
else
  if git fetch -q origin "$BRANCH" 2>/dev/null && ! git merge-base --is-ancestor "origin/$BRANCH" HEAD 2>/dev/null; then
    git pull --rebase -q origin "$BRANCH" || { git rebase --abort 2>/dev/null; die "GitHub has changes this folder doesn't. To overwrite them run:  FORCE=1 bash tools/publish.sh"; }
  fi
  git push -u origin "$BRANCH" || die "Push failed - check that Termux is logged in to GitHub."
fi
git tag -f "v$VERSION" -m "FlashKit $VERSION" >/dev/null 2>&1 && git push -f -q origin "v$VERSION" >/dev/null 2>&1
DESC="Flash, sideload and rescue a second Android device over OTG from a rooted phone - Termux:GUI app"
TOPICS="termux,android,adb,fastboot,flasher,rom,gsi,sideload,otg,kernelsu,magisk,termux-gui,python"
if [ -z "${FLASHKIT_REMOTE:-}" ]; then   # best effort: repo description + topics
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    gh repo edit "$USER_NAME/$REPO_NAME" --description "$DESC" --homepage "https://t.me/Shivraj_editx" --add-topic "$TOPICS" >/dev/null 2>&1 && say "Set the repo description and topics"
  elif [ -f "$HOME/.git-credentials" ]; then
    tok="$(sed -n 's#^https://[^:]*:\([^@]*\)@github\.com.*#\1#p' "$HOME/.git-credentials" | head -n1)"
    if [ -n "$tok" ]; then
      curl -fsS -X PATCH -H "Authorization: token $tok" "https://api.github.com/repos/$USER_NAME/$REPO_NAME" -d "{\"description\":\"$DESC\",\"homepage\":\"https://t.me/Shivraj_editx\"}" >/dev/null 2>&1
      curl -fsS -X PUT -H "Authorization: token $tok" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$USER_NAME/$REPO_NAME/topics" -d "{\"names\":[\"${TOPICS//,/\",\"}\"]}" >/dev/null 2>&1 && say "Set the repo description and topics"
    fi
  fi
fi
say "Done: https://github.com/$USER_NAME/$REPO_NAME"
printf '\nInstall on any phone (Termux):\n  pkg install -y git && git clone https://github.com/%s/%s && bash %s/install.sh\n\nUpdate an installed copy:\n  flashkit update\n' "$USER_NAME" "$REPO_NAME" "$REPO_NAME"
