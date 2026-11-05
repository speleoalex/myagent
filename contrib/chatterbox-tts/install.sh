#!/usr/bin/env bash
# Installs Chatterbox Multilingual (Resemble AI) in a venv of its own, outside
# the repo and outside ~/myagent: it is a 5 GB experiment, not part of the app.
#
#   ./install.sh              CPU wheels (torch 2.6.0+cpu, the version the
#                             package actually pins) -- the safe route
#   ./install.sh --xpu        afterwards, force torch 2.7.1+xpu over the pin so
#                             the Intel Arc can be used (see README: the pin is
#                             violated on purpose, chatterbox is over-tight)
#   ./install.sh --prefetch   also download the model now instead of at the
#                             first synthesis (~1 GB, 4 min on the Vaio)
#
# Everything lands under $CHATTERBOX_HOME (venv + HF cache + tmp), so
# `rm -rf` on that one folder removes every trace.
set -euo pipefail

PREFIX="${CHATTERBOX_HOME:-$HOME/.local/share/chatterbox-tts}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
WITH_XPU=0
PREFETCH=0
for arg in "$@"; do
  case "$arg" in
    --xpu) WITH_XPU=1 ;;
    --prefetch) PREFETCH=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# pip unpacks multi-GB wheels in TMPDIR; /tmp here is a 16 GB tmpfs, i.e. RAM.
export TMPDIR="$PREFIX/tmp"
export HF_HOME="$PREFIX/hf"
mkdir -p "$TMPDIR" "$HF_HOME"

if [ ! -x "$PREFIX/venv/bin/python" ]; then
  echo "==> creating venv in $PREFIX/venv"
  python3 -m venv "$PREFIX/venv"
fi
PY="$PREFIX/venv/bin/python"
PIP="$PREFIX/venv/bin/pip"

echo "==> installing chatterbox-tts (CPU wheels)"
# setuptools<81 is not cosmetic: resemble-perth, the watermarker chatterbox
# calls on every generate(), still imports pkg_resources, dropped in 81.
"$PIP" install --upgrade pip >/dev/null
"$PIP" install \
  --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://pypi.org/simple \
  chatterbox-tts "setuptools<81"

if [ "$WITH_XPU" = 1 ]; then
  echo "==> overriding torch with the XPU build (breaks the ==2.6.0 pin on purpose)"
  "$PIP" install --index-url https://download.pytorch.org/whl/xpu \
    torch==2.7.1+xpu torchaudio==2.7.1+xpu
fi

echo "==> installed:"
"$PY" -c 'import torch, torchaudio; print("   torch", torch.__version__, "| torchaudio", torchaudio.__version__)'
"$PY" -c 'import torch; print("   xpu available:", torch.xpu.is_available() if hasattr(torch, "xpu") else False)'

if [ "$PREFETCH" = 1 ]; then
  echo "==> pre-downloading the multilingual model into $HF_HOME"
  "$PY" - <<'PYEOF'
from chatterbox.mtl_tts import ChatterboxMultilingualTTS
ChatterboxMultilingualTTS.from_pretrained(device="cpu")
print("   model cached")
PYEOF
fi

# The launcher must run the venv's python, not whatever `env python3` finds
# first on PATH: torch exists only inside $PREFIX/venv. Rewriting the shebang
# at install time keeps the checked-in script runnable with a plain python3.
sed "1s|.*|#!$PREFIX/venv/bin/python3|" "$HERE/chatterbox-say" > "$PREFIX/chatterbox-say"
chmod 755 "$PREFIX/chatterbox-say"
mkdir -p "$HOME/bin"
ln -sf "$PREFIX/chatterbox-say" "$HOME/bin/chatterbox-say"
echo
echo "Done. $PREFIX holds everything ($(du -sh "$PREFIX" 2>/dev/null | cut -f1))."
echo "Try:  chatterbox-say -o /tmp/prova.wav 'Ciao, sono il satellite della cucina.'"
