#!/usr/bin/env bash
# Install the connectors plugin into a running myagent.
#
# The plugin is code only: it goes to ~/myagent/plugins/connectors/, while its
# state (bot tokens, grants, address book) stays in ~/myagent/connectors/ and is
# never touched by this script — installing, reinstalling and uninstalling all
# leave your bots configured.
#
# Run it from the git checkout (deploy.sh deliberately does not ship connectors/
# with the core: myagent standalone must not carry the code of an online
# service).
#
#   bash connectors/install.sh
#
set -euo pipefail

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Same precedence as server/app/config.py: specific override, then the common
# root, then the default under the user's home.
PLUGINS_DIR="${MYAGENT_PLUGINS:-${MYAGENT_HOME:-$HOME/myagent}/plugins}"
TARGET_DIR="$PLUGINS_DIR/connectors"

if [ "$(id -u)" = "0" ]; then
    echo "Do not run this as root: the plugin installs under your own home." >&2
    echo "(Only the final service restart needs sudo, and it is asked for.)" >&2
    exit 1
fi

# The single most likely way to break this migration: the old standalone service
# still polling the same bot token. Telegram hands each update to exactly one
# getUpdates caller, so both halves look healthy while messages go missing.
if command -v systemctl >/dev/null 2>&1 && \
   systemctl is-active --quiet myagent-connectors 2>/dev/null; then
    cat >&2 <<'EOM'
The old standalone connectors service is still running.

Two pollers on the same bot token means Telegram delivers each message to only
one of them, at random, with no error on either side. Stop it first:

    sudo systemctl stop myagent-connectors
    sudo systemctl disable myagent-connectors

Then run this script again.
EOM
    exit 1
fi

# Which venv? The one the RUNNING service uses, not necessarily this checkout:
# on Linux the service runs from the install dir, on macOS in place.
find_install_dir() {
    if [ -n "${MYAGENT_INSTALL_DIR:-}" ]; then
        echo "$MYAGENT_INSTALL_DIR"; return
    fi
    if command -v systemctl >/dev/null 2>&1; then
        local wd
        wd=$(systemctl show -p WorkingDirectory --value myagent 2>/dev/null || true)
        if [ -n "$wd" ] && [ -d "$wd" ]; then echo "$wd"; return; fi
    fi
    for candidate in /opt/myagent /opt/applications/myagent "$(dirname -- "$SOURCE_DIR")"; do
        if [ -x "$candidate/server/.venv/bin/python" ]; then echo "$candidate"; return; fi
    done
    echo ""
}

INSTALL_DIR="$(find_install_dir)"
VENV_PY="$INSTALL_DIR/server/.venv/bin/python"
if [ -z "$INSTALL_DIR" ] || [ ! -x "$VENV_PY" ]; then
    echo "Could not find myagent's virtualenv." >&2
    echo "Install myagent first (./deploy.sh, or ./setup.sh for a dev checkout)," >&2
    echo "or set MYAGENT_INSTALL_DIR to its directory." >&2
    exit 1
fi
echo "myagent install: $INSTALL_DIR"

echo "Installing plugin code to $TARGET_DIR"
mkdir -p "$TARGET_DIR"
rsync -a --delete --exclude __pycache__ --exclude '*.pyc' \
    "$SOURCE_DIR/plugin/" "$TARGET_DIR/"

# The plugin's shared requirements, then each channel's own. Never --upgrade: the
# core's dependencies must not move underneath a working install just because a
# plugin was added. A channel whose deps fail to install is still copied — it
# reports itself as failed at startup instead of breaking the others.
PIP="$INSTALL_DIR/server/.venv/bin/pip"
for req in "$SOURCE_DIR/plugin/requirements.txt" \
           "$SOURCE_DIR"/plugin/myagent_connectors/channels/*/requirements.txt; do
    [ -f "$req" ] || continue
    # Skip files that are only comments (the shared one currently is).
    grep -qvE '^\s*(#|$)' "$req" || continue
    name=$(basename "$(dirname "$req")")
    echo "Installing dependencies for '$name' (first time: this can be ~380 MB)…"
    if ! "$PIP" install -q -r "$req"; then
        echo "WARNING: dependencies for '$name' could not be installed." >&2
        echo "         Everything else works; that channel degrades." >&2
    fi
done

# Prove the core still imports: a half-finished pip run must not leave a server
# that cannot start.
if ! "$VENV_PY" -c "import fastapi, httpx, pydantic" 2>/dev/null; then
    echo "ERROR: myagent's virtualenv is broken after the install. Do NOT restart" >&2
    echo "       the service; reinstall with ./setup.sh." >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "NOTE: ffmpeg is not on the PATH — voice notes need it."
    echo "      Debian/Ubuntu: sudo apt install ffmpeg | macOS: brew install ffmpeg"
fi

echo
echo "Restarting myagent so it picks the plugin up…"
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files myagent.service >/dev/null 2>&1; then
    sudo systemctl restart myagent
elif command -v launchctl >/dev/null 2>&1 && \
     launchctl print "gui/$(id -u)/com.myagent.agent" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/com.myagent.agent"
else
    echo "No managed service found — restart myagent yourself."
fi

cat <<EOM

Done. Configure your bots at http://127.0.0.1:8888/#/connectors
Logs:  journalctl -u myagent -f | grep connectors

The first voice note downloads a Whisper model (~464 MB) into
~/.cache/huggingface. To use a much smaller one, set in the service
environment: MYAGENT_WHISPER_MODEL=base

To uninstall (your bots and tokens stay in ~/myagent/connectors/):
    rm -rf $TARGET_DIR
    sudo systemctl restart myagent
EOM
