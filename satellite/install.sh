#!/usr/bin/env bash
# Install the MyAgent voice satellite ON THIS DEVICE (PC or Raspberry Pi).
#
# Unlike connectors/install.sh (which installs a plugin INSIDE myagent), this
# sets up a standalone client in place: its own venv, the Piper voice, a
# config.json with a freshly generated shared key, and a systemd user unit.
# Copy this folder to the device first (scp -r satellite/ pi@device:), then:
#
#   bash install.sh [--voice it_IT-paola-medium] [--name Cucina]
#
set -euo pipefail

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VOICE="it_IT-paola-medium"
NAME="$(hostname -s 2>/dev/null || echo Satellite)"

while [ $# -gt 0 ]; do
    case "$1" in
        --voice) VOICE="$2"; shift 2 ;;
        --name)  NAME="$2";  shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" = "0" ]; then
    echo "Do not run this as root: the satellite installs under your own home" >&2
    echo "and needs access to YOUR audio devices." >&2
    exit 1
fi

# ------------------------------------------------------------------ venv
echo "Creating virtualenv in $DIR/.venv…"
python3 -m venv "$DIR/.venv"
if ! "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"; then
    cat >&2 <<'EOM'
WARNING: some Python dependencies could not be installed.
  - sounddevice needs PortAudio:  sudo apt install libportaudio2
  - piper-tts may lack a wheel for this platform: install a binary release
    from https://github.com/rhasspy/piper and put `piper` on the PATH.
The satellite still runs in degraded mode (no mic / no voice) with what it has.
EOM
fi

# --------------------------------------------------------- system deps check
for tool in aplay arecord; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "NOTE: '$tool' not found — install ALSA utils: sudo apt install alsa-utils"
        break
    fi
done

# ------------------------------------------------------------- Piper voice
# Voice name <lang>_<REGION>-<speaker>-<quality> maps to the huggingface path
# <lang>/<lang>_<REGION>/<speaker>/<quality>/<name>.onnx (+ .json).
mkdir -p "$DIR/voices"
ONNX="$DIR/voices/$VOICE.onnx"
if [ ! -f "$ONNX" ]; then
    locale="${VOICE%%-*}"                      # it_IT
    lang="${locale%%_*}"                       # it
    rest="${VOICE#*-}"                         # paola-medium
    speaker="${rest%%-*}"                      # paola
    quality="${rest#*-}"                       # medium
    base="https://huggingface.co/rhasspy/piper-voices/resolve/main/$lang/$locale/$speaker/$quality/$VOICE"
    echo "Downloading Piper voice $VOICE…"
    if command -v curl >/dev/null 2>&1; then GET="curl -fsSL -o"; else GET="wget -qO"; fi
    if ! $GET "$ONNX" "$base.onnx" || ! $GET "$ONNX.json" "$base.onnx.json"; then
        rm -f "$ONNX" "$ONNX.json"
        echo "WARNING: could not download the voice. TTS stays off until you" >&2
        echo "         put a voice in $DIR/voices/ and point config.json at it." >&2
    fi
fi

# ------------------------------------------------------------------ config
if [ ! -f "$DIR/config.json" ]; then
    KEY="$("$DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(24))')"
    "$DIR/.venv/bin/python" - "$DIR" "$NAME" "$KEY" "$VOICE" <<'EOF'
import json, sys
d, name, key, voice = sys.argv[1:5]
cfg = json.loads(open(f"{d}/config.example.json").read())
cfg.update({"name": name, "key": key,
            "piper_voice": f"voices/{voice}.onnx",
            "binding_id": name.lower()})
open(f"{d}/config.json", "w").write(json.dumps(cfg, indent=2) + "\n")
EOF
    chmod 600 "$DIR/config.json"
    echo "Generated config.json (key included, kept 0600)."
else
    echo "config.json already exists: left untouched."
fi

# ----------------------------------------------------------- systemd unit
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    sed "s|@DIR@|$DIR|g" "$DIR/myagent-satellite.service" \
        > "$HOME/.config/systemd/user/myagent-satellite.service"
    systemctl --user daemon-reload
    echo "Installed user unit. Speaker mode (announcements only):"
    echo "    systemctl --user enable --now myagent-satellite"
    echo "On a headless Pi, keep it running after logout:"
    echo "    sudo loginctl enable-linger $USER"
else
    echo "systemd user session not available — run it by hand (see below)."
fi

# ------------------------------------------------------------------ pairing
KEY_NOW="$("$DIR/.venv/bin/python" -c 'import json;print(json.load(open("'"$DIR"'/config.json"))["key"])' 2>/dev/null || echo '<your key>')"
BID="$("$DIR/.venv/bin/python" -c 'import json;print(json.load(open("'"$DIR"'/config.json"))["binding_id"])' 2>/dev/null || echo '<binding id>')"
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<device ip>')"
cat <<EOM

Done. Next steps:

1. Edit $DIR/config.json: set "myagent_url" to your MyAgent server.
2. In MyAgent (#/connectors → New bot) create a binding:
      type:   Satellite
      id:     $BID
      URL:    http://$IP:8899
      token:  $KEY_NOW
   and pick the agent that answers.
3. Optional: add an address-book contact with satellite handle "$BID"
   so agents can notify this device by name.

Talk (push-to-talk, interactive):   $DIR/.venv/bin/python $DIR/satellite.py
Speaker only (service):             systemctl --user enable --now myagent-satellite
Test from the server:               curl http://$IP:8899/health
EOM
