#!/bin/bash
# Install MyAgent — the single installer for every setup.
#
# Usage:
#   ./install.sh                  # install + register the service (mode below)
#   ./install.sh --dev            # venv + deps in this checkout only, no service
#   ./install.sh --port N         # bind port (default: 8888, or the first free one)
#   ./install.sh --yes            # answer yes to every optional install
#   ./install.sh --no-optional    # never install optional system packages
#   sudo ./install.sh --service-user [NAME]   # root: create/use a service account
#   sudo ./install.sh --as-root               # root: run the service as root
#
# The mode follows who runs it, and where — no sudo is ever taken on its own:
#
#   plain user (Linux)   code in ~/myagent/bin, a systemd USER unit, the service
#                        runs as you on a free port. Without a systemd user
#                        session (su, ssh without lingering) it installs a SYSTEM
#                        unit myagent-<you> instead, User=you, via sudo.
#                        Several users on one machine
#                        get several independent instances, kept apart by plain
#                        POSIX permissions: nobody else can read your ~/myagent.
#   root (Linux)         asks: [1] create the service account `myagent` (default;
#                        code in /home/myagent/myagent/bin, state in
#                        /home/myagent/myagent, unit User=myagent) or [2] run as
#                        root (code in /opt/myagent, state in /root/myagent).
#   macOS                in place from this checkout, a per-user LaunchAgent.
#   --dev                in place, no service: run server/main.py by hand.
#
# Runtime state (config, tools, sessions, library, ...) always lives under the
# service user's ~/myagent (MYAGENT_HOME), outside the code, so re-running this
# script is safe. Optional dependencies are handled differently ON PURPOSE, and
# the rule is what the install COSTS the user, not where it lands: things that
# merely complete our own installation (libzim, numpy) are just installed; things
# that change the machine (poppler, tesseract, ffmpeg, Node — root) or pull
# hundreds of MB over the network (fastembed and its model) are OFFERED with the
# exact command printed; library CONTENT (the .zim archives) is never downloaded
# here at all.
set -e

SERVICE_NAME="myagent"
LABEL="com.myagent.agent"                       # launchd label (macOS)
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

MODE=""                    # user | service | root | macos | dev
SERVICE_USER="myagent"
ASSUME_YES="${MYAGENT_ASSUME_YES:-}"
SKIP_OPTIONAL=""
PORT="${MYAGENT_PORT:-}"
WANT_SERVICE_USER=""
WANT_ROOT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dev)            MODE=dev ;;
        --as-root)        WANT_ROOT=1 ;;
        --service-user)   WANT_SERVICE_USER=1
                          if [ -n "$2" ] && [ "${2#-}" = "$2" ]; then SERVICE_USER="$2"; shift; fi ;;
        --port)           PORT="$2"; shift ;;
        --port=*)         PORT="${1#--port=}" ;;
        -y|--yes)         ASSUME_YES=1 ;;
        --no-optional)    SKIP_OPTIONAL=1 ;;
        -h|--help)        sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)                echo "Unknown option: $1 (see $0 --help)" >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$PORT" ] && ! [ "$PORT" -ge 1 ] 2>/dev/null; then
    echo "--port needs a number, got '$PORT'" >&2; exit 2
fi

has() { command -v "$1" >/dev/null 2>&1; }

# Ask only when there is someone to answer: no TTY (a container build, a pipe,
# a cron-driven install) behaves exactly like answering no, so automation never
# hangs on a prompt. Same wording as library/fetch.py, Italian yes included.
ask() {
    [ -n "$ASSUME_YES" ] && return 0
    [ -t 0 ] || return 1
    printf '  %s [y/N] ' "$1"
    read -r reply || return 1
    case "$reply" in [yY]|[yY][eE][sS]|[sS]|[sS][iI]) return 0 ;; *) return 1 ;; esac
}

# ============================================================ 0. pick the mode
IS_ROOT=""; [ "$(id -u)" -eq 0 ] && IS_ROOT=1

if [ "$(uname)" = "Darwin" ]; then
    if [ -n "$IS_ROOT" ]; then
        echo "On macOS run this as your own user: it installs a per-user LaunchAgent." >&2
        exit 1
    fi
    [ "$MODE" = dev ] || MODE=macos
elif [ "$MODE" = dev ]; then
    :
elif [ -n "$IS_ROOT" ]; then
    if [ -n "$WANT_ROOT" ]; then
        MODE=root
    elif [ -n "$WANT_SERVICE_USER" ] || [ -n "$ASSUME_YES" ] || ! [ -t 0 ]; then
        # No terminal to ask on: the service account is the safe default.
        MODE=service
    else
        echo "You are root. How should MyAgent run?"
        echo "  [1] as a dedicated service account '$SERVICE_USER' (recommended)"
        echo "      code in /home/$SERVICE_USER/myagent/bin, state in /home/$SERVICE_USER/myagent;"
        echo "      the agents' shell and file tools cannot touch other users' files."
        echo "  [2] as root"
        echo "      code in /opt/myagent, state in /root/myagent; the agents can do"
        echo "      ANYTHING on this machine. Only for a box that is MyAgent's alone."
        printf '  Choice [1/2] (default 1): '
        read -r reply || reply=""
        case "$reply" in 2) MODE=root ;; *) MODE=service ;; esac
    fi
else
    MODE=user
fi

# Per mode: where the code goes, who runs it, where its ~/myagent is, which
# systemctl talks to its unit. IN_PLACE = no copy, the checkout IS the install.
IN_PLACE=""
SYSTEMCTL=""
UNIT_FILE=""
IS_USER_UNIT=""            # set for a `systemctl --user` unit (plain user mode)
USER_FALLBACK=""           # user mode without a user session: system unit myagent-<user>
case "$MODE" in
    user)
        RUN_USER="$(id -un)"; RUN_HOME="$HOME"
        INSTALL_DIR="${MYAGENT_INSTALL_DIR:-${MYAGENT_HOME:-$HOME/myagent}/bin}"
        SYSTEMCTL="systemctl --user"
        UNIT_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
        IS_USER_UNIT=1
        ;;
    service)
        RUN_USER="$SERVICE_USER"
        if ! id "$SERVICE_USER" >/dev/null 2>&1; then
            echo "Creating service account '$SERVICE_USER'..."
            NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
            useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
                    --shell "$NOLOGIN" "$SERVICE_USER"
        fi
        RUN_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
        INSTALL_DIR="${MYAGENT_INSTALL_DIR:-${MYAGENT_HOME:-$RUN_HOME/myagent}/bin}"
        SYSTEMCTL="systemctl"
        UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
        ;;
    root)
        RUN_USER="root"; RUN_HOME="/root"
        INSTALL_DIR="${MYAGENT_INSTALL_DIR:-/opt/myagent}"
        SYSTEMCTL="systemctl"
        UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
        ;;
    macos)
        RUN_USER="$(id -un)"; RUN_HOME="$HOME"; INSTALL_DIR="$SOURCE_DIR"; IN_PLACE=1
        UNIT_FILE="$HOME/Library/LaunchAgents/${LABEL}.plist"
        ;;
    dev)
        RUN_USER="$(id -un)"; RUN_HOME="$HOME"; INSTALL_DIR="$SOURCE_DIR"; IN_PLACE=1
        ;;
esac
# Same precedence as server/app/config.py: MYAGENT_HOME wins over the default
# under the service user's home. When set explicitly it is also written into
# the unit, so the service and this report agree on where the state is.
STATE_HOME="${MYAGENT_HOME:-$RUN_HOME/myagent}"
VENV="$INSTALL_DIR/server/.venv"

echo "=== MyAgent install ==="
echo "Mode:    $MODE"
echo "Source:  $SOURCE_DIR"
echo "Install: $INSTALL_DIR"
echo "State:   $STATE_HOME"
echo "User:    $RUN_USER"
echo ""

if [ "$MODE" = user ] && [ -e "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    echo "NOTE: a system-wide '$SERVICE_NAME' service exists on this machine (installed with"
    echo "      sudo). This install is separate: your own instance, under your own user."
    echo "      To update THAT one instead: sudo $0"
    echo ""
fi

# Fail on an old Python here rather than three steps later: the models use
# PEP-604 annotations (`bool | None`), so 3.9 creates the venv and installs the
# deps happily and then dies with a pydantic traceback at first import.
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    FOUND=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "none")
    echo "MyAgent needs Python 3.10+ (found: $FOUND)." >&2
    echo "Install it, or point this script at another interpreter:" >&2
    echo "  PYTHON=/usr/bin/python3.12 $0" >&2
    exit 1
fi

# ============================================================ 1. copy the code
if [ -z "$IN_PLACE" ]; then
    echo "[1/5] Copying files..."
    # Refuse to install onto itself. This script is checkout tooling and is NOT
    # shipped into the install dir (see the exclude list), but MYAGENT_INSTALL_DIR
    # pointing at the checkout would make this a no-op that still rewrites the
    # unit and restarts the service: it LOOKS like an install while shipping
    # nothing. pwd -P so symlinks are seen through.
    src_real="$(cd "$SOURCE_DIR" && pwd -P)"
    dst_real="$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P || echo "$INSTALL_DIR")"
    if [ "$src_real" = "$dst_real" ]; then
        echo "Source and install directory are the same: $src_real" >&2
        echo "Run install.sh from the git checkout — it copies FROM there INTO the" >&2
        echo "install dir (use --dev to set the checkout itself up)." >&2
        exit 1
    fi
    mkdir -p "$INSTALL_DIR"

    # Exclude list: everything git ignores, derived from the checkout itself, on
    # top of a static baseline that also covers a non-git source (tarball). The
    # static entries double as protection for build artifacts on the DESTINATION
    # (server/.venv and node_modules are created there below, and rsync --delete
    # never removes excluded paths).
    EXCLUDES="$(mktemp)"
    trap 'rm -f "$EXCLUDES"' EXIT
    {
        echo '.git'
        echo 'connectors'   # has its own installer (connectors/install.sh)
        echo 'satellite'    # installs on ANOTHER device (see satellite/README.md)
        # Checkout-side tooling: it operates ON a git clone, so in the install
        # dir it can only mislead (install.sh would install the install onto
        # itself — the guard above —, update.sh needs a .git that is not there,
        # tests/ is for developing this repo). Anchored: top level only.
        printf '%s\n' '/install.sh' '/uninstall.sh' '/update.sh' '/.gitignore' '/tests'
        # What DOES stay: library/ (fetch.py is what the report below tells the
        # user to run), docs/, README*.md and LICENSE.
        printf '%s\n' '.venv' '__pycache__' '*.pyc' 'node_modules' '.env' \
            '.claude' 'CLAUDE.md' '.playwright-mcp' '.ruff_cache' 'docs_local' \
            'TODO-internal.md'
        # safe.directory: as root inside a checkout owned by someone else git
        # refuses to run; -c writes nothing to any gitconfig. A non-git source
        # yields nothing and leaves the baseline in charge.
        git -C "$SOURCE_DIR" -c safe.directory="$SOURCE_DIR" -c core.quotePath=false \
            ls-files --others --ignored --exclude-standard --directory 2>/dev/null || true
    } > "$EXCLUDES"
    rsync -a --delete --exclude-from="$EXCLUDES" "$SOURCE_DIR/" "$INSTALL_DIR/"

    # Sweep what older installs left behind: --delete skips excluded paths, so
    # anything copied before its exclude existed would sit there forever. Only
    # names that are junk BY DEFINITION in the install dir — never server/.venv.
    # Safe: the guard above guarantees INSTALL_DIR is not the checkout.
    rm -rf "$INSTALL_DIR/.venv" "$INSTALL_DIR/.playwright-mcp" "$INSTALL_DIR/.ruff_cache" \
           "$INSTALL_DIR/docs_local" "$INSTALL_DIR/.claude" "$INSTALL_DIR/tests" \
           "$INSTALL_DIR/install.sh" "$INSTALL_DIR/uninstall.sh" "$INSTALL_DIR/update.sh" "$INSTALL_DIR/.gitignore" \
           "$INSTALL_DIR/setup.sh" "$INSTALL_DIR/deploy.sh" "$INSTALL_DIR/deploy-macos.sh"
else
    echo "[1/5] In place: $INSTALL_DIR"
fi

# ============================================================ 2. python venv
echo "[2/5] Python venv..."
if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$INSTALL_DIR/server/requirements.txt"

# libzim stays OUT of requirements.txt (the core list is deliberately four
# lines, and a platform with no wheel must not break the whole install) but it
# IS installed here: without it every library/* tool is dark, and that is the
# one feature which needs no internet at all. Best-effort, never fatal.
if ! "$VENV/bin/python" -c "import libzim" >/dev/null 2>&1; then
    echo "  Installing libzim (reads the offline .zim archives)..."
    if ! "$VENV/bin/pip" install -q libzim; then
        echo "  libzim could not be installed — the offline library stays disabled."
        echo "  Retry by hand: $VENV/bin/pip install libzim"
    fi
fi

# numpy, same rule and same reason: it is what the optional semantic index
# scores with. It usually arrives anyway as a dependency of faster-whisper, and
# that is exactly why it is asked for explicitly — an install without voice
# support would otherwise silently have no semantic search either.
if ! "$VENV/bin/python" -c "import numpy" >/dev/null 2>&1; then
    echo "  Installing numpy (semantic search over your documents)..."
    if ! "$VENV/bin/pip" install -q numpy; then
        echo "  numpy could not be installed — semantic search stays disabled"
        echo "  (keyword search is unaffected). Retry: $VENV/bin/pip install numpy"
    fi
fi

# fastembed: embeddings computed IN THIS PROCESS, so semantic search needs no
# endpoint, no pulled model and no registered config — one dropdown entry in
# Settings and it works. OFFERED rather than installed, unlike libzim and numpy
# above, for one reason: the model is a 241 MB download from Hugging Face on
# first use. Deciding to spend that (and the disk, and the CPU that indexing
# costs) is the user's, not ours — and an install that reaches out to a model
# host unasked would be a strange thing on an app that sells itself on working
# offline.
#
# 8 packages, no torch and no CUDA. The obvious alternative,
# sentence-transformers (what the doc_indexer project uses), resolves to 82
# packages including 15 nvidia wheels: measured, not assumed.
FASTEMBED_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
if "$VENV/bin/python" -c "import fastembed" >/dev/null 2>&1; then
    echo "  fastembed already present (in-process embeddings)."
else
    echo "  Semantic search can embed in-process, with nothing to configure."
    echo "  It needs the optional 'fastembed' package (8 packages, no torch)."
    if ask "Install fastembed?"; then
        if "$VENV/bin/pip" install -q fastembed; then
            echo "  Installed. Choose 'In this process' under Settings -> Embedding model."
        else
            echo "  fastembed could not be installed — semantic search can still use"
            echo "  a local embedding endpoint. Retry: $VENV/bin/pip install fastembed"
        fi
    else
        echo "  Skipped. Later: $VENV/bin/pip install fastembed"
    fi
fi

# The model download, asked SEPARATELY from the package: someone may well want
# the code without 241 MB right now, and getting it out of the way here is only
# a convenience — the first index run downloads it anyway, in the background,
# under a service that throttles, nices and can be stopped. Runs as the service
# user so the cache lands in the right home, and never as a hard failure.
if "$VENV/bin/python" -c "import fastembed" >/dev/null 2>&1; then
    SEMINDEX="$INSTALL_DIR/server/tools/library/local_search/semindex.py"
    if ask "Download the embedding model now (241 MB)?"; then
        echo "  Fetching $FASTEMBED_MODEL ..."
        # As the SERVICE user and with its MYAGENT_HOME: under `sudo ./install.sh`
        # $HOME is /root, and a cache written there is one the service cannot
        # read — the same trap the library report already works around. --root
        # is required by the CLI but unused by --prefetch.
        PREFETCH_OK=1
        if [ "$RUN_USER" != "$(id -un)" ]; then
            sudo -u "$RUN_USER" MYAGENT_HOME="$STATE_HOME" \
                "$VENV/bin/python" "$SEMINDEX" --root "$INSTALL_DIR" \
                --prefetch --embed-local "$FASTEMBED_MODEL" || PREFETCH_OK=""
        else
            MYAGENT_HOME="$STATE_HOME" \
                "$VENV/bin/python" "$SEMINDEX" --root "$INSTALL_DIR" \
                --prefetch --embed-local "$FASTEMBED_MODEL" || PREFETCH_OK=""
        fi
        [ -n "$PREFETCH_OK" ] || \
            echo "  Could not fetch it now — the first index run will try again."
    else
        echo "  Skipped: the first semantic search downloads it in the background."
    fi
fi

# ============================================================ 3. tools + deps
echo "[3/5] Tools..."
find "$INSTALL_DIR/server/tools" -name "run" -exec chmod +x {} \;

# browse_web / web_search are Node scripts that need puppeteer-core. web_search
# shares browse_web's node_modules through a relative symlink (git does not
# preserve it: node_modules/ is ignored). A function because the optional
# package step may install npm and has to come back here.
install_web_deps() {
    echo "  Installing web-tool dependencies (puppeteer-core)..."
    (cd "$INSTALL_DIR/server/tools/browse_web" && npm install --omit=dev --no-fund --no-audit --loglevel=error)
    ln -sfn ../browse_web/node_modules "$INSTALL_DIR/server/tools/web_search/node_modules"
}
if has npm; then
    install_web_deps
else
    echo "  npm not found — web tools (browse_web, web_search, web_research) disabled."
fi

# Everything here is a system binary that a TOOL shells out to, so installing it
# needs root and touches the machine outside MyAgent: we ask, and we print the
# command either way. Chromium is deliberately NOT offered — on Ubuntu the apt
# package is a snap shim that fails inside containers, and a headless server
# often does not want a browser at all; the report below keeps it visible.
echo "  Optional system dependencies:"
MISSING=""; PKGS=""; PKG_MGR=""; PKG_CMD=""
if has apt-get;  then PKG_MGR=apt;    PKG_CMD="apt-get install -y"
elif has dnf;    then PKG_MGR=dnf;    PKG_CMD="dnf install -y"
elif has yum;    then PKG_MGR=dnf;    PKG_CMD="yum install -y"
elif has pacman; then PKG_MGR=pacman; PKG_CMD="pacman -S --needed --noconfirm"
elif has zypper; then PKG_MGR=zypper; PKG_CMD="zypper install -y"
elif has brew;   then PKG_MGR=brew;   PKG_CMD="brew install"
fi
pkg_name() {   # one package name per (binary, manager); unknown manager → nothing
    case "$1:$PKG_MGR" in
        pdftotext:apt|pdftotext:dnf)     echo poppler-utils ;;
        pdftotext:pacman|pdftotext:brew) echo poppler ;;
        pdftotext:zypper)                echo poppler-tools ;;
        tesseract:apt|tesseract:zypper)  echo tesseract-ocr ;;
        tesseract:dnf|tesseract:pacman|tesseract:brew) echo tesseract ;;
        pandoc:*)                        echo pandoc ;;
        ffmpeg:*)                        echo ffmpeg ;;
        npm:apt|npm:pacman)              echo "nodejs npm" ;;
        npm:brew)                        echo node ;;
        npm:dnf|npm:zypper)              echo nodejs ;;
    esac
}
want() {  # want <binary> <feature name>
    has "$1" && return 0
    MISSING="${MISSING:+$MISSING, }$2"
    [ -n "$PKG_MGR" ] && PKGS="${PKGS:+$PKGS }$(pkg_name "$1")"
}
want pdftotext "PDF text extraction and search"
want tesseract "OCR"
want pandoc    "document conversion"
want ffmpeg    "audio transcription"
want npm       "web tools"

# brew must never run as root (it refuses, loudly and correctly); apt & co. need
# root, so borrow sudo when we are not root and it exists.
SUDO=""; INSTALLABLE=yes
if [ -z "$PKGS" ]; then INSTALLABLE=no
elif [ "$PKG_MGR" = brew ]; then [ -n "$IS_ROOT" ] && INSTALLABLE=no
elif [ -z "$IS_ROOT" ]; then
    if has sudo; then SUDO="sudo "; else INSTALLABLE=no; fi
fi
if [ -z "$MISSING" ]; then
    echo "    Nothing missing."
elif [ "$INSTALLABLE" = no ]; then
    echo "    Missing: $MISSING"
    echo "    Install them with your package manager to enable those tools."
else
    echo "    Missing: $MISSING"
    echo "    Command: ${SUDO}${PKG_CMD} ${PKGS}"
    if [ -n "$SKIP_OPTIONAL" ]; then
        echo "    Skipped (--no-optional)."
    elif ask "Install them now?"; then
        # Unquoted on purpose: the package list is passed by word splitting.
        if ${SUDO}${PKG_CMD} ${PKGS}; then
            hash -r          # bash caches command lookups; refresh has()
            if has npm && [ ! -e "$INSTALL_DIR/server/tools/browse_web/node_modules" ]; then
                install_web_deps
            fi
        else
            echo "    Installation failed — run the command above by hand."
        fi
    fi
fi

# Ownership: the venv and npm deps were just created by root. "$RUN_USER:" is
# that user's login group — a hardcoded group of the same name exists on Debian
# but not everywhere, and with set -e a missing group would kill the install.
if [ -n "$IS_ROOT" ] && [ -z "$IN_PLACE" ]; then
    chown -R "$RUN_USER:" "$INSTALL_DIR"
fi
if [ "$MODE" = service ]; then
    # The admin who ran this keeps a way in without sudo: member of the service
    # group, group-writable state tree with setgid so new directories inherit
    # the group, and UMask=0002 in the unit so new files are group-writable too.
    # Other users get nothing (o-rwx). Secrets stay 0600 by the server's own
    # doing, so they still take sudo — that is the point of them.
    mkdir -p "$STATE_HOME"
    chown -R "$RUN_USER:" "$STATE_HOME"
    chmod -R g+rwX,o-rwx "$STATE_HOME"
    find "$STATE_HOME" -type d -exec chmod g+s {} +
    # A login shell needs to traverse /home/myagent to reach ~/myagent inside.
    chmod g+x "$RUN_HOME" 2>/dev/null || true
    if [ -n "$SUDO_USER" ] && [ "$SUDO_USER" != root ]; then
        usermod -aG "$RUN_USER" "$SUDO_USER"
        ADMIN_IN_GROUP="$SUDO_USER"
    fi
fi

# ============================================================ 4. the service
# Port: explicit > what the existing unit already uses > first free from 8888.
# The probe binds for real (no SO_REUSEADDR, on all interfaces so a listener on
# any address counts), which is why our own unit is stopped FIRST — otherwise
# the instance we are about to restart would make every install hop a port.
unit_port() {   # port recorded in the existing unit/plist, if any
    case "$MODE" in
        macos) [ -f "$UNIT_FILE" ] && grep -A1 '<key>MYAGENT_PORT</key>' "$UNIT_FILE" \
                   | sed -n 's:.*<string>\([0-9]*\)</string>.*:\1:p' ;;
        *)     $SYSTEMCTL show -p Environment --value "$SERVICE_NAME" 2>/dev/null \
                   | tr ' ' '\n' | sed -n 's/^MYAGENT_PORT=//p' ;;
    esac
}
port_free() {
    "$VENV/bin/python" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)
PYEOF
}
stop_service() {
    case "$MODE" in
        macos) launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$UNIT_FILE" 2>/dev/null || true ;;
        *)     $SYSTEMCTL stop "$SERVICE_NAME" 2>/dev/null || true ;;
    esac
}
pick_port() {
    stop_service
    if [ -z "$PORT" ]; then PORT="$(unit_port | head -1)"; fi
    if [ -n "$PORT" ]; then
        port_free "$PORT" || echo "  WARNING: port $PORT is in use by another process; the service will fail to bind until it is free."
        return
    fi
    PORT=8888
    while ! port_free "$PORT"; do PORT=$((PORT + 1)); done
    if [ "$PORT" -ne 8888 ]; then echo "  Port 8888 is taken — using $PORT."; fi
}

HAVE_SYSTEMD=""
case "$MODE" in
    user)  systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1 ;;
    service|root) has systemctl && HAVE_SYSTEMD=1 ;;
esac

# Written into the unit only when the admin set it: the default is derived by
# the server from $HOME, which systemd sets from User=.
STATE_ENV=""
[ -n "$MYAGENT_HOME" ] && STATE_ENV="Environment=MYAGENT_HOME=$MYAGENT_HOME"

# User mode without a user session (typical of `su - user` or a plain ssh login
# without lingering: no XDG_RUNTIME_DIR, `systemctl --user` cannot talk to a
# manager). First retry with the standard runtime dir; if there is still no
# session, fall back to a SYSTEM unit named myagent-<user> that runs as this
# user (User=). It needs sudo once to write the unit, survives logout and
# starts at boot — which a user unit without lingering would not anyway.
if [ "$MODE" = user ] && [ -z "$HAVE_SYSTEMD" ]; then
    if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
        systemctl --user show-environment >/dev/null 2>&1 && HAVE_SYSTEMD=1
    fi
fi
if [ "$MODE" = user ] && [ -z "$HAVE_SYSTEMD" ] && has systemctl && [ -d /etc/systemd/system ]; then
    if sudo -n true 2>/dev/null || { [ -t 0 ] && echo "  No systemd user session: a system unit needs sudo." && sudo -v; }; then
        USER_FALLBACK=1; HAVE_SYSTEMD=1; IS_USER_UNIT=""
        SERVICE_NAME="myagent-$RUN_USER"
        SYSTEMCTL="sudo systemctl"
        UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
        # HOME is derived from User= by systemd, but say it anyway: explicit
        # beats a surprise when the account's home is moved later.
        [ -n "$STATE_ENV" ] || STATE_ENV="Environment=MYAGENT_HOME=$RUN_HOME/myagent"
        echo "  No systemd user session — installing system unit '$SERVICE_NAME' (runs as $RUN_USER)."
    fi
fi

unit_write() {   # stdin -> $UNIT_FILE; /etc needs sudo
    if [ -n "$USER_FALLBACK" ]; then sudo tee "$UNIT_FILE" >/dev/null; else cat > "$UNIT_FILE"; fi
}

write_systemd_unit() {
    local edit_cmd wanted extra=""
    if [ -n "$IS_USER_UNIT" ]; then
        edit_cmd="systemctl --user edit $SERVICE_NAME"; wanted="default.target"
    else
        # A user unit runs as its owner by definition; a system unit must say.
        edit_cmd="sudo systemctl edit $SERVICE_NAME"; wanted="multi-user.target"
        extra="User=$RUN_USER"
    fi
    # Group-writable files, so the admin in the service group can edit them.
    if [ "$MODE" = service ]; then extra="$extra
UMask=0002"; fi
    if [ -n "$STATE_ENV" ]; then extra="$extra
$STATE_ENV"; fi
    [ -n "$USER_FALLBACK" ] || mkdir -p "$(dirname "$UNIT_FILE")"
    unit_write <<EOF
# GENERATED BY install.sh — REWRITTEN ON EVERY INSTALL (update.sh reinstalls
# too), so edits made here are silently lost. Customize with a drop-in instead,
# which every install leaves untouched:
#     $edit_cmd
# then add what you need, for example:
#     [Service]
#     Environment=MYAGENT_HOST=0.0.0.0            # expose on the LAN — set an API key FIRST
#     Environment=MYAGENT_API_KEY=change-me       # pin the key (Settings turns read-only);
#                                                 # normally set it in the UI instead: no restart
#     Environment=MYAGENT_SSL_CERTFILE=/etc/myagent/fullchain.pem
#     Environment=MYAGENT_SSL_KEYFILE=/etc/myagent/privkey.pem   # omit for a combined PEM
# and restart the service.
[Unit]
Description=MyAgent - AI Agent Platform
# network-online.target waits for actual connectivity (network.target does
# not), so the messaging connectors don't start before DNS is resolvable.
# They retry on their own anyway; this just avoids the noise at boot.
Wants=network-online.target
After=network.target network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/server/.venv/bin/python $INSTALL_DIR/server/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
$extra
# The API has no authentication by default and ships shell-executing tools: keep
# it on localhost unless you know what you are doing (or front it with an
# authenticating reverse proxy). Override in a drop-in (see header), not here.
Environment=MYAGENT_HOST=127.0.0.1
Environment=MYAGENT_PORT=$PORT

[Install]
WantedBy=$wanted
EOF
}

write_plist() {
    local state_block=""
    [ -n "$MYAGENT_HOME" ] && state_block="        <key>MYAGENT_HOME</key>
        <string>$MYAGENT_HOME</string>"
    mkdir -p "$(dirname "$UNIT_FILE")" "$HOME/Library/Logs"
    cat > "$UNIT_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/server/.venv/bin/python</string>
        <string>${INSTALL_DIR}/server/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
$state_block
        <!-- The API has no authentication and ships shell-executing tools:
             keep it on localhost unless you know what you are doing. -->
        <key>MYAGENT_HOST</key>
        <string>127.0.0.1</string>
        <key>MYAGENT_PORT</key>
        <string>${PORT}</string>
        <!-- Serve HTTPS directly, without a reverse proxy (needed to install
             the UI as an app from another device; the certificate must be
             TRUSTED; omit MYAGENT_SSL_KEYFILE for a combined PEM):
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
    <string>$HOME/Library/Logs/myagent.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/myagent.err.log</string>
</dict>
</plist>
EOF
}

# Stop a dev instance started by hand, from either the install dir or the
# checkout: the pattern matches the script path whether launched relative
# ("server/main.py") or absolute (what ExecStart uses) — matching only one form
# leaves a process holding the port, and the restart then fails for a reason
# nobody can see from here.
kill_dev_instances() {
    pkill -f "$INSTALL_DIR/server/.venv/bin/python .*main\.py" 2>/dev/null || true
    pkill -f "$SOURCE_DIR/server/.venv/bin/python .*main\.py" 2>/dev/null || true
    sleep 1
}

SERVICE_INSTALLED=""
case "$MODE" in
    dev)
        echo "[4/5] No service (--dev)."
        ;;
    macos)
        echo "[4/5] Installing LaunchAgent..."
        pick_port
        write_plist
        kill_dev_instances
        UID_NUM="$(id -u)"
        if launchctl bootstrap "gui/${UID_NUM}" "$UNIT_FILE" 2>/dev/null; then
            launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
        else
            launchctl unload "$UNIT_FILE" 2>/dev/null || true
            launchctl load "$UNIT_FILE"
        fi
        SERVICE_INSTALLED=1
        ;;
    *)
        if [ -z "$HAVE_SYSTEMD" ]; then
            echo "[4/5] No systemd$([ "$MODE" = user ] && echo ' user session') available — no service installed."
            [ -n "$PORT" ] || PORT=8888
        else
            echo "[4/5] Installing systemd service..."
            pick_port
            write_systemd_unit
            kill_dev_instances
            $SYSTEMCTL daemon-reload
            $SYSTEMCTL enable "$SERVICE_NAME"
            $SYSTEMCTL restart "$SERVICE_NAME"
            SERVICE_INSTALLED=1
        fi
        ;;
esac

# ============================================================ 5. report
echo "[5/5] LLM backend and optional features:"

# A backend is the one NON-optional dependency, so it is reported first. Probed
# with the venv's Python and urllib (no curl, which can be absent; no httpx, to
# stay independent of the deps just installed). Read-only, localhost, ~3s worst
# case — not a download, so "install.sh never fetches content" holds. The
# `|| true` matters: `set -e` would abort on a failed substitution.
LLM_INFO=$("$VENV/bin/python" - <<'PYEOF' 2>/dev/null || true
import json, urllib.request

def get(url, timeout=1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())

try:
    n = len(get("http://localhost:11434/api/tags").get("models") or [])
    print(f"ok|Ollama, {n} model(s)" if n else "empty|Ollama is running but has no models")
except Exception:
    try:
        urllib.request.urlopen("http://localhost:8080/health", timeout=1.5).read()
        print("ok|llama.cpp at localhost:8080")
    except Exception:
        print("")
PYEOF
)
LLM_STATE="${LLM_INFO%%|*}"
LLM_TEXT="${LLM_INFO#*|}"
if [ "$LLM_STATE" = "ok" ]; then
    echo "  [ok] LLM backend          ($LLM_TEXT)"
elif [ "$LLM_STATE" = "empty" ]; then
    echo "  [--] LLM backend          ($LLM_TEXT — run 'ollama pull qwen3')"
elif has ollama || [ -d "/Applications/Ollama.app" ]; then
    echo "  [--] LLM backend          (Ollama installed but not running — run 'ollama serve')"
else
    echo "  [--] LLM backend          (install Ollama, or start a llama.cpp server)"
fi

if has chromium || has chromium-browser || has google-chrome || has google-chrome-stable \
   || [ -n "$PUPPETEER_EXECUTABLE_PATH" ] \
   || [ -d "/Applications/Google Chrome.app" ] || [ -d "/Applications/Chromium.app" ]; then
    CHROME=yes; else CHROME=no; fi
if has npm && [ "$CHROME" = yes ]; then
    echo "  [ok] web browsing/search  (Node + Chrome/Chromium)"
else
    # Name only what is actually missing: a flat "needs Node.js + Chrome" reads
    # as two gaps when there is one, and Node is the half we can install.
    NEED=""
    has npm || NEED="Node.js"
    [ "$CHROME" = yes ] || NEED="${NEED:+$NEED + }Chrome/Chromium"
    echo "  [--] web browsing/search  (needs $NEED)"
fi
if has pdftotext; then echo "  [ok] PDF text + search    (poppler-utils)"
else echo "  [--] PDF text + search    (install poppler-utils)"; fi
if has tesseract; then echo "  [ok] OCR                  (tesseract)"
else echo "  [--] OCR                  (install tesseract)"; fi
if has pandoc; then echo "  [ok] document conversion  (pandoc)"
else echo "  [--] document conversion  (install pandoc)"; fi
if has ffmpeg; then echo "  [ok] audio transcription  (ffmpeg)"
else echo "  [--] audio transcription  (install ffmpeg)"; fi

# The offline library is content, not code: never downloaded here (gigabytes,
# onto a disk only the user can pick). Report the state and name the command,
# resolved against the SERVICE user's home — as root, $HOME is /root while the
# library belongs to whoever the service runs as. -L: an assembled library
# links its big archives in from another disk, as the library tools' walk does.
LIB_DIR="${MYAGENT_LIBRARY:-$STATE_HOME/library}"
CMD_AS=""
[ "$RUN_USER" != "$(id -un)" ] && CMD_AS="sudo -u $RUN_USER "
ZIM_COUNT=$(find -L "$LIB_DIR" -maxdepth 2 -name '*.zim' 2>/dev/null | wc -l)
if ! "$VENV/bin/python" -c "import libzim" >/dev/null 2>&1; then
    echo "  [--] offline library      (libzim missing: $VENV/bin/pip install libzim)"
elif [ "$ZIM_COUNT" -gt 0 ]; then
    echo "  [ok] offline library      ($ZIM_COUNT archive(s) in $LIB_DIR)"
else
    echo "  [--] offline library      (libzim ready, no archives yet)"
fi

# Semantic search needs numpy AND an embedding model the user has to choose;
# naming only what is actually missing keeps the line actionable — the flat
# "needs numpy + a model" version sent people to install numpy they already had.
# The CHOICE is read from settings.json, not assumed: reporting "ready, go pick
# it" to someone who picked it three days ago reads as "your setting was lost"
# and sends them to redo a step (observed on this machine, 2026-08-27).
CONF_JSON="${MYAGENT_CONFIG:-$STATE_HOME/config}/settings.json"
EMBED_ID=""
if [ -f "$CONF_JSON" ]; then
    EMBED_ID=$("$VENV/bin/python" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("embedding_model_id") or "")
except Exception: pass' "$CONF_JSON" 2>/dev/null || true)
fi
HAS_FASTEMBED=0
"$VENV/bin/python" -c "import fastembed" >/dev/null 2>&1 && HAS_FASTEMBED=1

if ! "$VENV/bin/python" -c "import numpy" >/dev/null 2>&1; then
    echo "  [--] semantic search      (numpy missing: $VENV/bin/pip install numpy)"
elif [ "$EMBED_ID" = "local" ] && [ "$HAS_FASTEMBED" = 0 ]; then
    # Set to in-process but the package is gone (a rebuilt venv): name THAT,
    # or the user goes looking in Settings, where it already says the right thing.
    echo "  [--] semantic search      (set to in-process, but fastembed is missing:"
    echo "                             $VENV/bin/pip install fastembed)"
elif [ "$EMBED_ID" = "local" ]; then
    echo "  [ok] semantic search      (in this process, no endpoint)"
elif [ -n "$EMBED_ID" ]; then
    echo "  [ok] semantic search      (embedding model '$EMBED_ID')"
elif [ "$HAS_FASTEMBED" = 1 ]; then
    echo "  [--] semantic search      (ready: pick 'In this process' under"
    echo "                             Settings -> Embedding model)"
else
    echo "  [--] semantic search      (optional: $VENV/bin/pip install fastembed,"
    echo "                             or pull a local embedding model such as"
    echo "                             ollama pull embeddinggemma:300m, then"
    echo "                             pick it in Settings)"
fi

echo ""
echo "=== Install complete ==="
case "$MODE" in
    dev)
        echo "Run:  $VENV/bin/python $INSTALL_DIR/server/main.py"
        echo "Then open http://127.0.0.1:8888  (MYAGENT_PORT=N to change the port)"
        ;;
    macos)
        echo "URL:     http://127.0.0.1:$PORT"
        echo "Status:  launchctl print gui/$(id -u)/${LABEL} | grep state"
        echo "Logs:    tail -f \"$HOME/Library/Logs/myagent.log\""
        echo "Stop:    launchctl bootout gui/$(id -u)/${LABEL}"
        ;;
    *)
        if [ -z "$SERVICE_INSTALLED" ]; then
            echo "Run:  $VENV/bin/python $INSTALL_DIR/server/main.py"
            echo "Then open http://127.0.0.1:$PORT"
        else
            SC="$SYSTEMCTL"; JC="journalctl"; [ -n "$IS_USER_UNIT" ] && JC="journalctl --user"
            echo "URL:     http://127.0.0.1:$PORT"
            echo "Service: $SC status $SERVICE_NAME"
            echo "Logs:    $JC -u $SERVICE_NAME -f"
            # The unit was just rewritten: reassure that drop-ins (the supported
            # way to configure the service) are still there.
            DROPIN_DIR="${UNIT_FILE}.d"
            if ls "$DROPIN_DIR"/*.conf >/dev/null 2>&1; then
                echo "Drop-ins: $(cd "$DROPIN_DIR" && echo *.conf) (kept — your settings live there)"
            elif [ -n "$IS_USER_UNIT" ]; then
                echo "To customize (LAN, API key, TLS, debug): systemctl --user edit $SERVICE_NAME"
            else
                echo "To customize (LAN, API key, TLS, debug): sudo systemctl edit $SERVICE_NAME"
            fi
            if [ -n "$USER_FALLBACK" ]; then
                echo ""
                echo "Installed as a system unit running as $RUN_USER (no systemd user session was"
                echo "available): it keeps running after logout and starts at boot."
            fi
            if [ -n "$IS_USER_UNIT" ]; then
                # A user unit dies with the login session unless the user lingers.
                LINGER="$(loginctl show-user "$RUN_USER" -p Linger --value 2>/dev/null || echo unknown)"
                if [ "$LINGER" != yes ]; then
                    echo ""
                    echo "The service stops when you log out. To keep it running (boot included):"
                    echo "  loginctl enable-linger $RUN_USER      (may need sudo)"
                fi
            fi
            if [ "$MODE" = root ]; then
                echo ""
                echo "WARNING: the service runs as root. Every agent with shell_exec or a file"
                echo "         tool can do anything on this machine. Keep MYAGENT_HOST=127.0.0.1."
            fi
            if [ -n "$ADMIN_IN_GROUP" ]; then
                echo ""
                echo "'$ADMIN_IN_GROUP' was added to group '$RUN_USER' and can read/edit $STATE_HOME"
                echo "without sudo after the next login (secrets stay 0600, as they should)."
            fi
        fi
        ;;
esac

# Without a model MyAgent starts and looks healthy, and the first message is
# the thing that fails. Say so here, while the user is still in the terminal.
if [ "$LLM_STATE" != "ok" ]; then
    echo ""
    echo "No LLM backend yet — MyAgent needs one to answer. Either:"
    echo "  Ollama:    install from https://ollama.com, then 'ollama pull qwen3'"
    echo "  llama.cpp: llama-server -m <model.gguf> --port 8080 --jinja"
    echo "Or add a remote API key under Models once the UI is open."
fi
if [ "$ZIM_COUNT" -eq 0 ]; then
    echo ""
    echo "No offline knowledge yet. To give the agents a starting library"
    echo "(~1.4 GB: emergency medicine, water, food, repair):"
    echo "  ${CMD_AS}$INSTALL_DIR/library/fetch.py --list"
    echo "  ${CMD_AS}$INSTALL_DIR/library/fetch.py --preset base [--dest /path/on/another/disk]"
fi
