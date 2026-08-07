#!/bin/bash
# Update MyAgent from GitHub — without ever overwriting local work.
#
# Fetches this checkout's upstream (origin/<branch>) and compares by git
# ANCESTRY, never by date:
#   * GitHub strictly ahead                  -> fast-forward, then redeploy
#   * local commits GitHub doesn't have, or
#     uncommitted edits to tracked files     -> NOTHING is touched (exit 2)
#   * histories diverged                     -> nothing is touched (exit 2)
#
# Usage:
#   ./update.sh              # update this checkout (+ redeploy if installed)
#   ./update.sh --check      # report what would happen, change nothing
#   ./update.sh --no-deploy  # update the checkout but skip the deploy step
#
# Exit codes: 0 = updated or already up to date, 2 = skipped to protect
# local changes, 1 = error (no network, detached HEAD, not a git clone, ...).
set -e

CHECK=""
NO_DEPLOY=""
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--check)  CHECK=1 ;;
        --no-deploy) NO_DEPLOY=1 ;;
        -h|--help)   sed -n '2,17p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)           echo "Unknown option: $1 (see $0 --help)" >&2; exit 1 ;;
    esac
    shift
done

cd "$(cd "$(dirname "$0")" && pwd)"

fail() { echo "ERROR: $*" >&2; exit 1; }
skip() { echo ""; echo "NOT UPDATED: $*"; exit 2; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "$(pwd) is not a git checkout — updates need a clone of the repo (git clone https://github.com/speleoalex/myagent.git)"

# Running git as root inside another user's checkout leaves root-owned files
# in .git and trips git's ownership check. deploy.sh asks for sudo on its own.
OWNER="$(stat -c %U . 2>/dev/null || stat -f %Su .)"
if [ "${EUID:-$(id -u)}" = "0" ] && [ "$OWNER" != "root" ]; then
    fail "run this as '$OWNER' (the checkout's owner); the deploy step will use sudo by itself"
fi

BRANCH="$(git symbolic-ref --short -q HEAD)" \
    || fail "detached HEAD — check out a branch first (git checkout main)"
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "origin/$BRANCH")"
REMOTE="${UPSTREAM%%/*}"

echo "=== MyAgent update ==="
echo "Checkout: $(pwd)"
echo "Branch:   $BRANCH (upstream: $UPSTREAM)"

git fetch --quiet "$REMOTE" \
    || fail "cannot fetch from '$REMOTE' — is the network up?"
git rev-parse -q --verify "$UPSTREAM^{commit}" >/dev/null \
    || fail "upstream '$UPSTREAM' does not exist on remote '$REMOTE'"

LOCAL="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "$UPSTREAM^{commit}")"
BASE="$(git merge-base HEAD "$UPSTREAM")" \
    || fail "no common history with $UPSTREAM"
DIRTY="$(git status --porcelain --untracked-files=no)"

if [ "$LOCAL" = "$REMOTE_SHA" ]; then
    echo "Already up to date: $(git log -1 --format='%h %s')."
    [ -n "$DIRTY" ] && echo "(There are uncommitted local edits; they are untouched.)"
    exit 0
fi

# Local ahead: every remote commit is already here -> local is NEWER.
if [ "$BASE" = "$REMOTE_SHA" ]; then
    echo ""
    echo "Local commits not on $UPSTREAM:"
    git log --oneline "$UPSTREAM..HEAD" | head -10
    skip "the local version is NEWER than GitHub — not overwriting. Push the commits, or discard them with: git reset --hard $UPSTREAM"
fi

# Diverged: both sides have commits the other lacks.
if [ "$BASE" != "$LOCAL" ]; then
    echo ""
    echo "Local-only commits:"
    git log --oneline "$UPSTREAM..HEAD" | head -5
    echo "GitHub-only commits:"
    git log --oneline "HEAD..$UPSTREAM" | head -5
    skip "local and GitHub histories have DIVERGED — not overwriting. Reconcile by hand (merge/rebase, or git reset --hard $UPSTREAM to take GitHub's version)."
fi

# From here on GitHub is strictly ahead.
if [ -n "$DIRTY" ]; then
    echo ""
    echo "Uncommitted local changes:"
    echo "$DIRTY" | head -10
    skip "there are local edits to tracked files — commit or stash them, then rerun."
fi

echo ""
echo "New on $UPSTREAM ($(git rev-list --count "HEAD..$UPSTREAM") commit(s)):"
git log --oneline "HEAD..$UPSTREAM" | head -15

if [ -n "$CHECK" ]; then
    echo ""
    echo "(--check: would fast-forward and redeploy; nothing was changed)"
    exit 0
fi

git merge --ff-only --quiet "$UPSTREAM"
echo "Checkout updated to $(git log -1 --format='%h %s')."

# Redeploy the installed copy, if there is one.
if [ -n "$NO_DEPLOY" ]; then
    echo "Deploy step skipped (--no-deploy)."
    exit 0
fi

if [ "$(uname)" = "Darwin" ]; then
    if [ -f "$HOME/Library/LaunchAgents/com.myagent.agent.plist" ]; then
        echo ""
        bash deploy-macos.sh
    else
        echo "No installed service found — checkout updated only. To install: bash deploy-macos.sh"
    fi
else
    # WHERE the service runs is systemd's to answer, not ours to guess: a
    # deploy may have used MYAGENT_INSTALL_DIR, and the default itself has
    # changed (it was /opt/applications/myagent). Asking keeps an update from
    # either skipping the redeploy or silently relocating a working install.
    INSTALL_DIR="${MYAGENT_INSTALL_DIR:-}"
    if [ -z "$INSTALL_DIR" ]; then
        INSTALL_DIR="$(systemctl show -p WorkingDirectory --value myagent 2>/dev/null || true)"
    fi
    if [ -n "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ]; then
        echo ""
        echo "Redeploying to $INSTALL_DIR..."
        MYAGENT_INSTALL_DIR="$INSTALL_DIR" bash deploy.sh
    else
        echo "No installed service found — checkout updated only. To install as a service: sudo bash deploy.sh"
    fi
fi
