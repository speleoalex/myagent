#!/bin/bash
# Set up MyAgent for local use: Python venv, web-tool Node deps, permissions.
#
# Usage:
#   ./setup.sh              # set up this checkout
#   ./setup.sh /some/dir    # set up an installed copy (used by deploy.sh)
#
# Only Python is required. Everything else (Node.js for the web tools, the
# system binaries for document extraction, ...) is optional: this script
# detects what is available and reports which features it enables.
set -e

TARGET_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"
PYTHON="${PYTHON:-python3}"

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

# --- [1/3] Python venv + dependencies (backend is self-contained in server/) --
echo "[1/3] Python venv..."
if [ ! -d "$TARGET_DIR/server/.venv" ]; then
    "$PYTHON" -m venv "$TARGET_DIR/server/.venv"
fi
"$TARGET_DIR/server/.venv/bin/pip" install -q -r "$TARGET_DIR/server/requirements.txt"

# --- [2/3] tool scripts + Node deps for the web tools -------------------------
echo "[2/3] Tools..."
find "$TARGET_DIR/server/tools" -name "run" -exec chmod +x {} \;

# browse_web / web_search are Node scripts that need puppeteer-core.
# web_search shares browse_web's node_modules through a relative symlink
# (git does not preserve it because node_modules/ is ignored).
if command -v npm >/dev/null 2>&1; then
    echo "  Installing web-tool dependencies (puppeteer-core)..."
    (cd "$TARGET_DIR/server/tools/browse_web" && npm install --omit=dev --no-fund --no-audit --loglevel=error)
    ln -sfn ../browse_web/node_modules "$TARGET_DIR/server/tools/web_search/node_modules"
else
    echo "  npm not found — web tools (browse_web, web_search, web_research) disabled."
    echo "  Install Node.js and re-run ./setup.sh to enable them."
fi

# --- [3/3] LLM backend + optional feature report ------------------------------
echo "[3/3] LLM backend and optional features:"
has() { command -v "$1" >/dev/null 2>&1; }

# A backend is the one NON-optional dependency, so it is reported first and not
# under the "optional" heading. Probed with the venv's Python and urllib (no
# curl, which can be absent; no httpx import, to stay independent of the deps
# that were just installed). Read-only, localhost, ~3s worst case — this is not
# a download, so the "setup.sh never fetches content" rule is intact. The `||
# true` matters: `set -e` is on and would abort on a failed substitution.
LLM_INFO=$("$TARGET_DIR/server/.venv/bin/python" - <<'PYEOF' 2>/dev/null || true
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
    echo "  [--] web browsing/search  (needs Node.js + Chrome/Chromium)"
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
LIB_DIR="${MYAGENT_LIBRARY:-$HOME/myagent/library}"
ZIM_COUNT=$(find "$LIB_DIR" -maxdepth 2 -name '*.zim' 2>/dev/null | wc -l)
if ! "$TARGET_DIR/server/.venv/bin/python" -c "import libzim" >/dev/null 2>&1; then
    echo "  [--] offline library      (pip install libzim to read .zim archives)"
elif [ "$ZIM_COUNT" -gt 0 ]; then
    echo "  [ok] offline library      ($ZIM_COUNT archive(s) in $LIB_DIR)"
else
    echo "  [--] offline library      (no archives yet)"
fi

echo ""
echo "=== Setup complete ==="
echo "Run:  $TARGET_DIR/server/.venv/bin/python $TARGET_DIR/server/main.py"
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
    echo "  $TARGET_DIR/library/fetch.py --list"
    echo "  $TARGET_DIR/library/fetch.py --preset base [--dest /path/on/another/disk]"
fi
