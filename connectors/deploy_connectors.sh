#!/bin/bash
# Deploy MyAgent Connectors to /opt/applications/myagent-connectors and set up a
# systemd service. Linux counterpart of myagent's deploy.sh — same conventions.
#
# The connectors server is independent of myagent and talks to it only over
# HTTP, so you can point it at any myagent instance at deploy time:
#
#   sudo bash deploy_connectors.sh
#   MYAGENT_API_URL=http://192.168.1.10:8888 sudo -E bash deploy_connectors.sh
#
# Runtime data (bot bindings + password grants) lives OUTSIDE the install dir,
# under ~/myagent/connectors, so it survives redeploys (rsync --delete is safe).
set -e

INSTALL_DIR="/opt/applications/myagent-connectors"
SERVICE_NAME="myagent-connectors"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="$(whoami)"
PYTHON_VERSION="python3"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- configurable (override via env when invoking the script) ---------------
MYAGENT_API_URL="${MYAGENT_API_URL:-http://localhost:8888}"
MYAGENT_API_TOKEN="${MYAGENT_API_TOKEN:-}"
CONN_HOST="${MYAGENT_CONNECTORS_HOST:-127.0.0.1}"
CONN_PORT="${MYAGENT_CONNECTORS_PORT:-8899}"

echo "=== MyAgent Connectors Deploy ==="
echo "Source:      $SOURCE_DIR"
echo "Target:      $INSTALL_DIR"
echo "User:        $RUN_USER"
echo "myagent API: $MYAGENT_API_URL"
echo "Listen:      http://$CONN_HOST:$CONN_PORT"
echo ""

# Needs root for systemd and /opt. Re-run under sudo, preserving the invoking
# user and the config env vars (so they survive the privilege bump).
if [ "$EUID" -ne 0 ]; then
    echo "This script needs root privileges. Re-running with sudo..."
    exec sudo MYAGENT_USER="$RUN_USER" \
        MYAGENT_API_URL="$MYAGENT_API_URL" \
        MYAGENT_API_TOKEN="$MYAGENT_API_TOKEN" \
        MYAGENT_CONNECTORS_HOST="$CONN_HOST" \
        MYAGENT_CONNECTORS_PORT="$CONN_PORT" \
        bash "$0" "$@"
fi

# Preserve the original user when running under sudo
if [ -n "$MYAGENT_USER" ]; then
    RUN_USER="$MYAGENT_USER"
elif [ -n "$SUDO_USER" ]; then
    RUN_USER="$SUDO_USER"
fi

echo "[1/5] Creating install directory..."
mkdir -p "$INSTALL_DIR"

echo "[2/5] Copying files..."
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='.claude' \
    --exclude='.playwright-mcp' \
    "$SOURCE_DIR/" "$INSTALL_DIR/"

# venv lives at the project root (matches README: `python3 -m venv .venv`);
# requirements.txt is at the root, main entry point is server/main.py.
echo "[3/5] Setting up Python venv..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    $PYTHON_VERSION -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# Hand the tree to the run user (the service runs as them, and its runtime data
# lives under their home).
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

echo "[4/5] Installing systemd service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MyAgent Connectors - messaging bridge
# Start after myagent if it is a service on this host (soft ordering only).
After=network-online.target myagent.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python server/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=MYAGENT_API_URL=$MYAGENT_API_URL
Environment=MYAGENT_API_TOKEN=$MYAGENT_API_TOKEN
Environment=MYAGENT_CONNECTORS_HOST=$CONN_HOST
Environment=MYAGENT_CONNECTORS_PORT=$CONN_PORT

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] Starting service..."
# Stop any instance running from the install or source dir (dev runs).
pkill -f "$INSTALL_DIR/.venv/bin/python server/main.py" 2>/dev/null || true
pkill -f "$SOURCE_DIR/.venv/bin/python server/main.py" 2>/dev/null || true
sleep 1

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Deploy complete ==="
echo "Service: systemctl status $SERVICE_NAME"
echo "Logs:    journalctl -u $SERVICE_NAME -f"
echo "Admin UI: http://$CONN_HOST:$CONN_PORT"
