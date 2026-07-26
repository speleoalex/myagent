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

# --- [3/3] optional feature report --------------------------------------------
echo "[3/3] Optional features:"
has() { command -v "$1" >/dev/null 2>&1; }

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

echo ""
echo "=== Setup complete ==="
echo "Run:  $TARGET_DIR/server/.venv/bin/python $TARGET_DIR/server/main.py"
echo "Then open http://127.0.0.1:8888"
