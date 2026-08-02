#!/usr/bin/env bash
# Start the device's own page fullscreen, already unlocked, on the local screen.
#
# A satellite with a display (the kitchen Pi with its 800x480 DSI panel) should
# show the agent, not a desktop: this launches Chromium in kiosk mode on
# http://127.0.0.1:<port>/?key=… so the page comes up authenticated with no
# keyboard in the room. The key is READ FROM config.json at launch, never
# copied here — config.json stays the single source of truth (same rule as
# satellite.py's own /config endpoint).
#
#   bash kiosk.sh --install     # add it to the desktop session's autostart
#   bash kiosk.sh               # run it now (needs a running X session)
#   bash kiosk.sh --uninstall   # remove the autostart entry
#
# Note: the key ends up in the browser's argv, so it is visible to `ps` on this
# device. That is the same trust boundary as config.json itself (a local user
# who can read one can read the other); it is NOT sent anywhere but localhost.
set -euo pipefail

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
AUTOSTART="$HOME/.config/autostart/myagent-kiosk.desktop"
# A profile of its own: the kiosk's localStorage holds the shared key, and
# wiping it must not mean wiping the user's normal browsing (and vice versa).
PROFILE="$HOME/.config/myagent-kiosk"

case "${1:-}" in
    --install)
        mkdir -p "$(dirname "$AUTOSTART")"
        cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=MyAgent kiosk
Comment=Satellite page fullscreen on the local screen
Exec=$DIR/kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
        echo "Installed $AUTOSTART — starts at the next graphical login."
        exit 0 ;;
    --uninstall)
        rm -f "$AUTOSTART"
        echo "Removed $AUTOSTART."
        exit 0 ;;
    "") ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
esac

# ------------------------------------------------------------------ config
read -r PORT KEY <<EOF
$(python3 -c 'import json,sys
c=json.load(open(sys.argv[1]))
print(c.get("listen_port",8899), c.get("key",""))' "$DIR/config.json")
EOF
if [ -z "${KEY:-}" ]; then
    echo "No key in $DIR/config.json — run install.sh first." >&2
    exit 1
fi

export DISPLAY="${DISPLAY:-:0}"

# ------------------------------------------------------------ wait for the device
# At login the user service may still be starting (venv import, mic probe), and
# Chromium caches the failure page: better to wait than to show an error to a
# room with no keyboard.
for _ in $(seq 60); do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
    sleep 1
done

# ------------------------------------------------------------------- screen
# A kiosk that blanks is a kiosk you have to touch before you can read it.
xset s off -dpms s noblank 2>/dev/null || true
command -v xscreensaver-command >/dev/null 2>&1 && xscreensaver-command -exit >/dev/null 2>&1 || true
command -v unclutter >/dev/null 2>&1 && (unclutter -idle 1 -root &) || true

# Power loss is the normal way this device is turned off, so the profile is
# almost always "crashed": clear the flags or every boot opens with a restore
# bubble over the page.
PREFS="$PROFILE/Default/Preferences"
[ -f "$PREFS" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/;s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS" || true

BROWSER="$(command -v chromium-browser || command -v chromium || true)"
if [ -z "$BROWSER" ]; then
    echo "No chromium found (apt install chromium-browser)." >&2
    exit 1
fi

exec "$BROWSER" \
    --user-data-dir="$PROFILE" \
    --kiosk "http://127.0.0.1:$PORT/?key=$KEY" \
    --noerrdialogs --disable-infobars --no-first-run --disable-translate \
    --disable-session-crashed-bubble --disable-features=TranslateUI \
    --overscroll-history-navigation=0 --disable-pinch \
    --password-store=basic \
    --check-for-update-interval=31536000
