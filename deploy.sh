#!/bin/bash
# Deploy MyAgent as a systemd service on Linux.
#
# Copies this checkout to the install dir (default /opt/applications/myagent,
# override with MYAGENT_INSTALL_DIR), sets up its venv and web-tool deps via
# setup.sh, and installs/restarts the systemd unit. Runtime state lives under
# ~/myagent/ (config, tools, sessions, workspace, ...), so redeploys are safe.
set -e

INSTALL_DIR="${MYAGENT_INSTALL_DIR:-/opt/applications/myagent}"
SERVICE_NAME="myagent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="$(whoami)"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== MyAgent Deploy ==="
echo "Source:  $SOURCE_DIR"
echo "Target:  $INSTALL_DIR"
echo "User:    $RUN_USER"
echo ""

# Check if running as root (needed for systemd and /opt)
if [ "$EUID" -ne 0 ]; then
    echo "This script needs root privileges. Re-running with sudo..."
    exec sudo MYAGENT_USER="$RUN_USER" MYAGENT_INSTALL_DIR="$INSTALL_DIR" bash "$0" "$@"
fi

# Preserve the original user when running under sudo
if [ -n "$MYAGENT_USER" ]; then
    RUN_USER="$MYAGENT_USER"
elif [ -n "$SUDO_USER" ]; then
    RUN_USER="$SUDO_USER"
fi

# Create install directory
echo "[1/4] Copying files..."
mkdir -p "$INSTALL_DIR"

# Sync files (exclude dev/local stuff; node_modules is reinstalled by setup.sh;
# connectors/ has its own deploy script)
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='.claude' \
    --exclude='.playwright-mcp' \
    --exclude='node_modules' \
    --exclude='CLAUDE.md' \
    --exclude='TODO-internal.md' \
    --exclude='connectors' \
    "$SOURCE_DIR/" "$INSTALL_DIR/"

# Venv, web-tool deps, permissions — one code path shared with the dev setup
echo "[2/4] Running setup..."
bash "$INSTALL_DIR/setup.sh" "$INSTALL_DIR"

# Fix ownership (setup ran as root)
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

# Create systemd service
echo "[3/4] Installing systemd service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MyAgent - AI Agent Platform
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/server/.venv/bin/python server/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# The API has no authentication and ships shell-executing tools: keep it on
# localhost unless you know what you are doing (or front it with an
# authenticating reverse proxy).
Environment=MYAGENT_HOST=127.0.0.1
Environment=MYAGENT_PORT=8888
# Require an API key on every /api request (Bearer header or ?api_key=).
# Recommended if you expose the port beyond localhost:
#Environment=MYAGENT_API_KEY=change-me
# Trace every executor turn (messages sent to the LLM, raw replies, parsed tool
# calls) to ~/myagent/logs/debug.log. Logs full chat content — enable it while
# debugging an agent, not permanently:
#Environment=MYAGENT_DEBUG=1

[Install]
WantedBy=multi-user.target
EOF

# Stop running dev instance (if any)
echo "[4/4] Starting service..."
pkill -f "$INSTALL_DIR/server/.venv/bin/python server/main.py" 2>/dev/null || true
# Also kill dev instances running from source dir
pkill -f "$SOURCE_DIR/server/.venv/bin/python server/main.py" 2>/dev/null || true
sleep 1

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Deploy complete ==="
echo "Service: systemctl status $SERVICE_NAME"
echo "Logs:    journalctl -u $SERVICE_NAME -f"
echo "URL:     http://127.0.0.1:8888"
