#!/bin/bash
# Install MyAgent on macOS as a per-user launchd service (LaunchAgent).
#
# macOS has no systemd/getent/opt, so this is the macOS counterpart of
# deploy.sh: it runs the app in place from this source directory, sets up the
# venv, and registers a LaunchAgent that starts MyAgent at login and restarts
# it on failure. No sudo needed — everything lives under the user's home.
#
# Runtime state still lives outside the app tree (~/myagent/{config,tools,...}),
# exactly like on Linux, so it survives re-running this script.
set -e

LABEL="com.myagent.agent"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
LOG_OUT="$LOG_DIR/myagent.log"
LOG_ERR="$LOG_DIR/myagent.err.log"

echo "=== MyAgent macOS Deploy ==="
echo "Source:  $SOURCE_DIR"
echo "Service: $LABEL (LaunchAgent)"
echo "Python:  $PYTHON"
echo ""

if [ "$(uname)" != "Darwin" ]; then
    echo "This installer is for macOS. On Linux use ./deploy.sh instead." >&2
    exit 1
fi

# [1/3] venv, web-tool deps, permissions — shared with the dev setup
echo "[1/3] Running setup..."
PYTHON="$PYTHON" bash "$SOURCE_DIR/setup.sh" "$SOURCE_DIR"

# [2/3] write the LaunchAgent plist
echo "[2/3] Installing LaunchAgent..."
mkdir -p "$PLIST_DIR" "$LOG_DIR"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SOURCE_DIR}/server/.venv/bin/python</string>
        <string>${SOURCE_DIR}/server/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SOURCE_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <!-- The API has no authentication and ships shell-executing tools:
             keep it on localhost unless you know what you are doing. -->
        <key>MYAGENT_HOST</key>
        <string>127.0.0.1</string>
        <key>MYAGENT_PORT</key>
        <string>8888</string>
        <!-- Serve HTTPS directly, without a reverse proxy. Needed to install
             the UI as an app from another device (browsers require a secure
             context; localhost already is one). The certificate must be
             TRUSTED. Omit MYAGENT_SSL_KEYFILE for a combined PEM.
        <key>MYAGENT_SSL_CERTFILE</key>
        <string>/usr/local/etc/myagent/fullchain.pem</string>
        <key>MYAGENT_SSL_KEYFILE</key>
        <string>/usr/local/etc/myagent/privkey.pem</string>
        -->
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>
</dict>
</plist>
EOF

# [3/3] (re)load the service
echo "[3/3] Starting service..."
# Newer launchctl (bootstrap/kickstart) with a fallback to legacy load/unload.
UID_NUM="$(id -u)"
if launchctl bootstrap "gui/${UID_NUM}" "$PLIST" 2>/dev/null; then
    launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
else
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
fi

echo ""
echo "=== Deploy complete ==="
echo "Status:  launchctl print gui/${UID_NUM}/${LABEL} | grep state"
echo "Logs:    tail -f \"$LOG_OUT\""
echo "Stop:    launchctl bootout gui/${UID_NUM}/${LABEL}   (or: launchctl unload \"$PLIST\")"
echo "URL:     http://127.0.0.1:8888"
