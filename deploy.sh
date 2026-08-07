#!/bin/bash
# Deploy MyAgent as a systemd service on Linux.
#
# Copies this checkout to the install dir (default /opt/myagent, override with
# MYAGENT_INSTALL_DIR), sets up its venv and web-tool deps via setup.sh, and
# installs/restarts the systemd unit. Runtime state lives under ~/myagent/
# (config, tools, sessions, workspace, ...), so redeploys are safe.
set -e

INSTALL_DIR="${MYAGENT_INSTALL_DIR:-/opt/myagent}"
LEGACY_INSTALL_DIR="/opt/applications/myagent"
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

# Refuse to deploy an install onto itself. This script is checkout tooling and
# is deliberately NOT shipped into the install dir (see the exclude list), but
# a hand-copied script — or MYAGENT_INSTALL_DIR pointing at the checkout —
# would make this a no-op that still re-runs setup, rewrites the unit and
# restarts the service: it LOOKS like a successful deploy while shipping none
# of the changes the user actually made.
src_real="$(cd "$SOURCE_DIR" && pwd -P)"
dst_real="$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P || echo "$INSTALL_DIR")"
if [ "$src_real" = "$dst_real" ]; then
    echo "Source and install directory are the same: $src_real" >&2
    echo "Run deploy.sh from the git checkout — it copies FROM there INTO the" >&2
    echo "install dir. To deploy elsewhere: sudo MYAGENT_INSTALL_DIR=/path bash deploy.sh" >&2
    exit 1
fi

# Create install directory
echo "[1/4] Copying files..."
mkdir -p "$INSTALL_DIR"

# Build the rsync exclude list. It used to be a hand-kept copy of .gitignore
# and drifted (docs_local/ shipped for a while): now everything git ignores is
# derived from the checkout itself, on top of a static baseline that also
# covers a non-git source (tarball). The static entries double as protection
# for build artifacts on the DESTINATION (server/.venv and node_modules are
# created there by setup.sh, and rsync --delete never removes excluded paths).
EXCLUDES="$(mktemp)"
trap 'rm -f "$EXCLUDES"' EXIT
{
    # Tracked in git but never deployed:
    echo '.git'
    echo 'connectors'   # has its own installer (connectors/install.sh)
    echo 'satellite'    # installs on ANOTHER device (see satellite/README.md)
    # Checkout-side tooling: it operates ON a git clone, so in the install dir
    # it can only mislead. deploy.sh/deploy-macos.sh would deploy the install
    # onto itself (the guard above stops that), update.sh needs a .git that is
    # not there, .gitignore means nothing without one, and tests/ is for
    # developing this repo. Anchored with a leading slash: these names are
    # excluded at the top level only, never deeper in the tree.
    printf '%s\n' '/deploy.sh' '/deploy-macos.sh' '/update.sh' '/.gitignore' '/tests'
    # What DOES stay, and why: setup.sh (deploy.sh runs $INSTALL_DIR/setup.sh)
    # and library/ (setup.sh prints $TARGET_DIR/library/fetch.py for the user
    # to run afterwards), plus docs/, README*.md and LICENSE.
    #
    # Static baseline, kept in sync with .gitignore as a fallback:
    printf '%s\n' '.venv' '__pycache__' '*.pyc' 'node_modules' '.env' \
        '.claude' 'CLAUDE.md' '.playwright-mcp' '.ruff_cache' 'docs_local' \
        'TODO-internal.md'
    # Everything .gitignore ignores, straight from git. safe.directory is
    # needed because we are root inside a checkout owned by someone else, and
    # git refuses that by default; passed with -c so nothing is written to any
    # gitconfig. A non-git source (tarball) just yields nothing and leaves the
    # baseline above in charge.
    git -C "$SOURCE_DIR" -c safe.directory="$SOURCE_DIR" -c core.quotePath=false \
        ls-files --others --ignored --exclude-standard --directory 2>/dev/null || true
} > "$EXCLUDES"

rsync -a --delete --exclude-from="$EXCLUDES" "$SOURCE_DIR/" "$INSTALL_DIR/"

# Sweep fossils older deploys left behind: --delete skips excluded paths, so
# anything copied before its exclude existed (docs_local, editor caches) or the
# pre-refactor root .venv would otherwise sit in the install dir forever. Only
# names that are junk BY DEFINITION in the install dir go here — never
# server/.venv, which is the live virtualenv.
# Safe: the guard at the top guarantees INSTALL_DIR is not the checkout, so
# this can never delete the scripts it is running from.
rm -rf "$INSTALL_DIR/.venv" "$INSTALL_DIR/.playwright-mcp" \
       "$INSTALL_DIR/.ruff_cache" "$INSTALL_DIR/docs_local" "$INSTALL_DIR/.claude" \
       "$INSTALL_DIR/deploy.sh" "$INSTALL_DIR/deploy-macos.sh" \
       "$INSTALL_DIR/update.sh" "$INSTALL_DIR/.gitignore" "$INSTALL_DIR/tests"

# Venv, web-tool deps, permissions — one code path shared with the dev setup
echo "[2/4] Running setup..."
bash "$INSTALL_DIR/setup.sh" "$INSTALL_DIR"

# Fix ownership (setup ran as root). "$RUN_USER:" means "that user's login
# group": a hardcoded group of the same name exists on Debian/Ubuntu but not
# everywhere, and with set -e a missing group kills the deploy halfway.
chown -R "$RUN_USER:" "$INSTALL_DIR"

# Create systemd service
echo "[3/4] Installing systemd service..."
cat > "$SERVICE_FILE" <<EOF
# GENERATED BY deploy.sh — REWRITTEN ON EVERY DEPLOY (update.sh redeploys too),
# so edits made here are silently lost. Customize with a drop-in instead, which
# every deploy leaves untouched:
#     sudo systemctl edit myagent
# then add what you need, for example:
#     [Service]
#     Environment=MYAGENT_HOST=0.0.0.0            # expose on the LAN — set an API key FIRST
#     Environment=MYAGENT_API_KEY=change-me       # pin the key (Settings turns read-only);
#                                                 # normally set it in the UI instead: no restart
#     Environment=MYAGENT_SSL_CERTFILE=/etc/myagent/fullchain.pem
#     Environment=MYAGENT_SSL_KEYFILE=/etc/myagent/privkey.pem   # omit for a combined PEM
#     Environment=MYAGENT_DEBUG=1                 # executor trace, logs FULL chat content
# and restart: sudo systemctl restart myagent
[Unit]
Description=MyAgent - AI Agent Platform
# network-online.target waits for actual connectivity (network.target does
# not), so the messaging connectors don't start before DNS is resolvable.
# They retry on their own anyway; this just avoids the noise at boot.
Wants=network-online.target
After=network.target network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/server/.venv/bin/python $INSTALL_DIR/server/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# The API has no authentication by default and ships shell-executing tools: keep
# it on localhost unless you know what you are doing (or front it with an
# authenticating reverse proxy). Override in a drop-in (see header), not here.
Environment=MYAGENT_HOST=127.0.0.1
Environment=MYAGENT_PORT=8888

[Install]
WantedBy=multi-user.target
EOF

# Stop running dev instance (if any), from either the install dir or the
# source checkout. The pattern must match the script path whether it was
# launched relative ("server/main.py", the dev habit) or absolute (what
# ExecStart above now uses) — matching only one form silently leaves a process
# holding the port, and the restart below then fails for a reason nobody can
# see from here.
echo "[4/4] Starting service..."
pkill -f "$INSTALL_DIR/server/.venv/bin/python .*main\.py" 2>/dev/null || true
pkill -f "$SOURCE_DIR/server/.venv/bin/python .*main\.py" 2>/dev/null || true
sleep 1

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "=== Deploy complete ==="
echo "Service: systemctl status $SERVICE_NAME"
echo "Logs:    journalctl -u $SERVICE_NAME -f"
echo "URL:     http://127.0.0.1:8888"
# The unit file was just rewritten: reassure that drop-in customizations (the
# supported way to configure the service, see the unit header) are still there.
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
if ls "$DROPIN_DIR"/*.conf >/dev/null 2>&1; then
    echo "Drop-ins: $(cd "$DROPIN_DIR" && echo *.conf) (kept — your settings live there)"
else
    echo "To customize (LAN, API key, TLS, debug): sudo systemctl edit $SERVICE_NAME"
fi

# The default install dir used to be /opt/applications/myagent. The unit was
# just rewritten, so the service already runs from the new location — but the
# old tree stays on disk and is now dead weight. Say so instead of deleting it:
# it is not ours to remove, and runtime state was never in there anyway.
if [ "$INSTALL_DIR" != "$LEGACY_INSTALL_DIR" ] && [ -d "$LEGACY_INSTALL_DIR" ]; then
    echo ""
    echo "NOTE: an install from the old default is still on disk:"
    echo "        $LEGACY_INSTALL_DIR"
    echo "      The service now runs from $INSTALL_DIR. Nothing of yours is in"
    echo "      there (runtime state lives under ~/myagent), so once this deploy"
    echo "      looks healthy you can remove it:  sudo rm -rf $LEGACY_INSTALL_DIR"
    echo "      To keep using the old location instead, redeploy with:"
    echo "        sudo MYAGENT_INSTALL_DIR=$LEGACY_INSTALL_DIR bash deploy.sh"
fi
