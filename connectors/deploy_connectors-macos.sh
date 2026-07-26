#!/bin/bash
# Install MyAgent Connectors on macOS as a per-user launchd service
# (LaunchAgent). macOS counterpart of deploy_connectors.sh.
#
# macOS has no systemd/opt, so this runs the server in place from this source
# directory, sets up the venv, and registers a LaunchAgent that starts the
# connectors server at login and restarts it on failure. No sudo needed.
#
# Point it at any myagent instance at install time:
#   bash deploy_connectors-macos.sh
#   MYAGENT_API_URL=http://192.168.1.10:8888 bash deploy_connectors-macos.sh
#
# Runtime state lives under ~/myagent/connectors, so it survives re-running this.
set -e

LABEL="com.myagent.connectors"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
LOG_OUT="$LOG_DIR/myagent-connectors.log"
LOG_ERR="$LOG_DIR/myagent-connectors.err.log"

# --- configurable (override via env when invoking) --------------------------
MYAGENT_API_URL="${MYAGENT_API_URL:-http://localhost:8888}"
MYAGENT_API_TOKEN="${MYAGENT_API_TOKEN:-}"
CONN_HOST="${MYAGENT_CONNECTORS_HOST:-127.0.0.1}"
CONN_PORT="${MYAGENT_CONNECTORS_PORT:-8899}"

echo "=== MyAgent Connectors macOS Deploy ==="
echo "Source:      $SOURCE_DIR"
echo "Service:     $LABEL (LaunchAgent)"
echo "Python:      $PYTHON"
echo "myagent API: $MYAGENT_API_URL"
echo "Listen:      http://$CONN_HOST:$CONN_PORT"
echo ""

if [ "$(uname)" != "Darwin" ]; then
    echo "This installer is for macOS. On Linux use ./deploy_connectors.sh instead." >&2
    exit 1
fi

# [1/3] venv + dependencies (venv at project root, matches README)
echo "[1/3] Setting up Python venv..."
if [ ! -d "$SOURCE_DIR/.venv" ]; then
    "$PYTHON" -m venv "$SOURCE_DIR/.venv"
fi
"$SOURCE_DIR/.venv/bin/pip" install -q --upgrade pip
"$SOURCE_DIR/.venv/bin/pip" install -q -r "$SOURCE_DIR/requirements.txt"

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
        <string>${SOURCE_DIR}/.venv/bin/python</string>
        <string>${SOURCE_DIR}/server/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SOURCE_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>MYAGENT_API_URL</key>
        <string>${MYAGENT_API_URL}</string>
        <key>MYAGENT_API_TOKEN</key>
        <string>${MYAGENT_API_TOKEN}</string>
        <key>MYAGENT_CONNECTORS_HOST</key>
        <string>${CONN_HOST}</string>
        <key>MYAGENT_CONNECTORS_PORT</key>
        <string>${CONN_PORT}</string>
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
UID_NUM="$(id -u)"
if launchctl bootstrap "gui/${UID_NUM}" "$PLIST" 2>/dev/null; then
    launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
else
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
fi

echo ""
echo "=== Deploy complete ==="
echo "Status:   launchctl print gui/${UID_NUM}/${LABEL} | grep state"
echo "Logs:     tail -f \"$LOG_OUT\""
echo "Stop:     launchctl bootout gui/${UID_NUM}/${LABEL}   (or: launchctl unload \"$PLIST\")"
echo "Admin UI: http://$CONN_HOST:$CONN_PORT"
