#!/bin/bash
# Set up MyAgent for local use: Python venv, web-tool Node deps, permissions.
#
# Usage:
#   ./setup.sh                 # set up this checkout
#   ./setup.sh /some/dir       # set up an installed copy (used by deploy.sh)
#   ./setup.sh --yes           # answer yes to every optional install
#   ./setup.sh --no-optional   # never install optional system packages
#
# Only Python is required. Everything else is optional, and the three kinds are
# handled differently ON PURPOSE:
#
#   * Inside our own virtualenv (libzim) we just install it, like every other
#     pip dependency. It is a few MB and the offline library is what MyAgent is
#     for; asking would be asking permission about our own install.
#   * System packages (poppler, tesseract, ffmpeg, Node) need root and change
#     the machine outside MyAgent, so they are OFFERED, default no, and the
#     exact command is always printed so it can be run later by hand.
#   * Library CONTENT (the .zim archives) is never downloaded here: gigabytes,
#     onto a disk only the user can pick. This script names the command instead.
set -e

TARGET_DIR=""
ASSUME_YES="${MYAGENT_ASSUME_YES:-}"
SKIP_OPTIONAL=""
while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)      ASSUME_YES=1 ;;
        --no-optional) SKIP_OPTIONAL=1 ;;
        -h|--help)     sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        -*)            echo "Unknown option: $1 (see $0 --help)" >&2; exit 2 ;;
        *)             TARGET_DIR="$1" ;;
    esac
    shift
done
TARGET_DIR="${TARGET_DIR:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="${PYTHON:-python3}"
VENV="$TARGET_DIR/server/.venv"

has() { command -v "$1" >/dev/null 2>&1; }

# Ask only when there is someone to answer: no TTY (a container build, a pipe,
# a cron-driven deploy) behaves exactly like answering no, so automation never
# hangs on a prompt. Same wording as library/fetch.py, Italian yes included.
ask() {
    [ -n "$ASSUME_YES" ] && return 0
    [ -t 0 ] || return 1
    printf '  %s [y/N] ' "$1"
    read -r reply || return 1
    case "$reply" in [yY]|[yY][eE][sS]|[sS]|[sS][iI]) return 0 ;; *) return 1 ;; esac
}

echo "=== MyAgent setup ==="
echo "Target: $TARGET_DIR"
echo ""

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

# --- [1/4] Python venv + dependencies (backend is self-contained in server/) --
echo "[1/4] Python venv..."
if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$TARGET_DIR/server/requirements.txt"

# libzim stays OUT of requirements.txt (the core list is deliberately four lines,
# and a platform with no wheel must not break the whole install) but it IS
# installed here: without it every library/* tool is dark, and that is the one
# feature which needs no internet at all. Best-effort — a failed build on an
# exotic platform is reported in the summary below, never fatal.
if ! "$VENV/bin/python" -c "import libzim" >/dev/null 2>&1; then
    echo "  Installing libzim (reads the offline .zim archives)..."
    if ! "$VENV/bin/pip" install -q libzim; then
        echo "  libzim could not be installed — the offline library stays disabled."
        echo "  Retry by hand: $VENV/bin/pip install libzim"
    fi
fi

# --- [2/4] tool scripts + Node deps for the web tools -------------------------
echo "[2/4] Tools..."
find "$TARGET_DIR/server/tools" -name "run" -exec chmod +x {} \;

# browse_web / web_search are Node scripts that need puppeteer-core.
# web_search shares browse_web's node_modules through a relative symlink
# (git does not preserve it because node_modules/ is ignored). A function
# because step 3 may install npm and has to come back here.
install_web_deps() {
    echo "  Installing web-tool dependencies (puppeteer-core)..."
    (cd "$TARGET_DIR/server/tools/browse_web" && npm install --omit=dev --no-fund --no-audit --loglevel=error)
    ln -sfn ../browse_web/node_modules "$TARGET_DIR/server/tools/web_search/node_modules"
}
if has npm; then
    install_web_deps
else
    echo "  npm not found — web tools (browse_web, web_search, web_research) disabled."
fi

# --- [3/4] optional system packages -------------------------------------------
# Everything here is a system binary that a TOOL shells out to, so installing it
# needs root and touches the machine outside MyAgent: we ask, and we print the
# command either way. Chromium is deliberately NOT offered — on Ubuntu the apt
# package is a snap shim that fails inside containers, and a headless server
# often does not want a browser at all; the report below keeps it visible.
echo "[3/4] Optional system dependencies..."
MISSING=""                       # human names, for the message
PKGS=""                          # package names, for the manager
PKG_MGR=""; PKG_CMD=""
if has apt-get;  then PKG_MGR=apt;    PKG_CMD="apt-get install -y"
elif has dnf;    then PKG_MGR=dnf;    PKG_CMD="dnf install -y"
elif has yum;    then PKG_MGR=dnf;    PKG_CMD="yum install -y"
elif has pacman; then PKG_MGR=pacman; PKG_CMD="pacman -S --needed --noconfirm"
elif has zypper; then PKG_MGR=zypper; PKG_CMD="zypper install -y"
elif has brew;   then PKG_MGR=brew;   PKG_CMD="brew install"
fi

# One package name per (binary, manager). Unknown manager → nothing to offer.
pkg_name() {
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
want pdftotext "PDF text extraction"
want tesseract "OCR"
want pandoc    "document conversion"
want ffmpeg    "audio transcription"
want npm       "web tools"

# brew must never run as root (it refuses, loudly and correctly); apt & co. need
# root, so borrow sudo when we are not root and it exists.
SUDO=""
INSTALLABLE=yes
if [ -z "$PKGS" ]; then
    INSTALLABLE=no
elif [ "$PKG_MGR" = brew ]; then
    [ "$(id -u)" -eq 0 ] && INSTALLABLE=no
elif [ "$(id -u)" -ne 0 ]; then
    if has sudo; then SUDO="sudo "; else INSTALLABLE=no; fi
fi

if [ -z "$MISSING" ]; then
    echo "  Nothing missing."
elif [ "$INSTALLABLE" = no ]; then
    echo "  Missing: $MISSING"
    echo "  Install them with your package manager to enable those tools."
else
    echo "  Missing: $MISSING"
    echo "  Command: ${SUDO}${PKG_CMD} ${PKGS}"
    if [ -n "$SKIP_OPTIONAL" ]; then
        echo "  Skipped (--no-optional)."
    elif ask "Install them now?"; then
        # Unquoted on purpose: the package list is passed by word splitting.
        if ${SUDO}${PKG_CMD} ${PKGS}; then
            hash -r          # bash caches command lookups; refresh has()
            # npm may have just appeared, and step 2 skipped puppeteer-core.
            if has npm && [ ! -e "$TARGET_DIR/server/tools/browse_web/node_modules" ]; then
                install_web_deps
            fi
        else
            echo "  Installation failed — run the command above by hand."
        fi
    fi
fi

# --- [4/4] LLM backend + optional feature report ------------------------------
echo "[4/4] LLM backend and optional features:"

# A backend is the one NON-optional dependency, so it is reported first and not
# under the "optional" heading. Probed with the venv's Python and urllib (no
# curl, which can be absent; no httpx import, to stay independent of the deps
# that were just installed). Read-only, localhost, ~3s worst case — this is not
# a download, so the "setup.sh never fetches content" rule is intact. The `||
# true` matters: `set -e` is on and would abort on a failed substitution.
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
if has pdftotext; then
    echo "  [ok] PDF text extraction  (poppler-utils)"
else
    echo "  [--] PDF text extraction  (install poppler-utils)"
fi
if has tesseract; then
    echo "  [ok] OCR                  (tesseract)"
else
    echo "  [--] OCR                  (install tesseract)"
fi
if has pandoc; then
    echo "  [ok] document conversion  (pandoc)"
else
    echo "  [--] document conversion  (install pandoc)"
fi
if has ffmpeg; then
    echo "  [ok] audio transcription  (ffmpeg)"
else
    echo "  [--] audio transcription  (install ffmpeg)"
fi

# The offline library is content, not code: setup never downloads it (this
# script also runs as root from deploy.sh, and the archives are gigabytes on
# a disk only the user can pick). It reports the state and names the command.
#
# Under `sudo bash deploy.sh` $HOME is /root while the library belongs to the
# user the service runs as: resolve THAT home, or a well-stocked library reads
# as empty here and the command printed below would fill root's home instead.
LIB_HOME="$HOME"
CMD_AS=""
if [ -z "$MYAGENT_LIBRARY" ] && [ -n "$SUDO_USER" ] && [ "$(id -u)" -eq 0 ]; then
    SU_HOME=$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)
    if [ -n "$SU_HOME" ]; then LIB_HOME="$SU_HOME"; CMD_AS="sudo -u $SUDO_USER "; fi
fi
LIB_DIR="${MYAGENT_LIBRARY:-$LIB_HOME/myagent/library}"
# -L: an assembled library links its big archives in from another disk, exactly
# as the library tools' own walk does.
ZIM_COUNT=$(find -L "$LIB_DIR" -maxdepth 2 -name '*.zim' 2>/dev/null | wc -l)
if ! "$VENV/bin/python" -c "import libzim" >/dev/null 2>&1; then
    echo "  [--] offline library      (libzim missing: $VENV/bin/pip install libzim)"
elif [ "$ZIM_COUNT" -gt 0 ]; then
    echo "  [ok] offline library      ($ZIM_COUNT archive(s) in $LIB_DIR)"
else
    echo "  [--] offline library      (libzim ready, no archives yet)"
fi

echo ""
echo "=== Setup complete ==="
echo "Run:  $VENV/bin/python $TARGET_DIR/server/main.py"
echo "Then open http://127.0.0.1:8888"
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
    echo "  ${CMD_AS}$TARGET_DIR/library/fetch.py --list"
    echo "  ${CMD_AS}$TARGET_DIR/library/fetch.py --preset base [--dest /path/on/another/disk]"
fi
