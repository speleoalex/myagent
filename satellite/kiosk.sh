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
#   bash kiosk.sh               # run it now (inside the graphical session)
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

# A kiosk that locks itself is a kiosk nobody can use: the panel in the
# kitchen has no keyboard to type a password on, and the desktop's locker is
# not something `xset s off` can reach (under Wayland xset does not even talk
# to the compositor). So switch it off for THIS user — the login the kiosk
# runs under, never system-wide — and only from --install, because running
# kiosk.sh to try it out must not silently unlock somebody's own desktop.
no_screen_lock() {
    KW="$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)"
    if [ -z "$KW" ]; then
        # Not KDE (or kwriteconfig missing): write the file, but never over an
        # existing one — that is somebody's configuration, not ours.
        if [ ! -e "$HOME/.config/kscreenlockerrc" ]; then
            mkdir -p "$HOME/.config"
            printf '[Daemon]\nAutolock=false\nLockOnResume=false\n' \
                > "$HOME/.config/kscreenlockerrc"
        fi
        return 0
    fi
    "$KW" --file kscreenlockerrc --group Daemon --key Autolock false
    "$KW" --file kscreenlockerrc --group Daemon --key LockOnResume false
    # And the screen itself: a day is "never" for a panel that is read at a
    # glance, and leaves the setting visible in System Settings instead of
    # disabled in a way nobody can find later.
    for prof in AC Battery; do
        "$KW" --file powermanagementprofilesrc --group "$prof" --group DPMSControl --key idleTime 86400 || true
        "$KW" --file powermanagementprofilesrc --group "$prof" --group DimDisplay --key idleTime 86400 || true
    done
}

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
        no_screen_lock
        echo "Installed $AUTOSTART — starts at the next graphical login."
        echo "Screen lock and blanking switched off for ${USER:-$(id -un)} (applies at that login)."
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

# ------------------------------------------------------------ which backend
# Chromium picks its Ozone backend from the environment, and the environment
# of a GRAPHICAL LOGIN is not the one an ssh shell has: KDE and GNOME export
# WAYLAND_DISPLAY, so the autostarted browser takes the Wayland backend while
# the same script run by hand over ssh takes X11 — the one difference behind
# "it works when I start it, it is dead when it starts itself". Where the
# Wayland backend cannot get a usable DRM render node (it says "Preferred
# drm_render_node not found, picking nouveau") the result is a window that is
# mapped, fullscreen, titled and COMPLETELY UNPAINTED: the page loaded, the
# screen is blank, and nothing in the browser says so.
#
# So the backend is pinned here instead of inherited: X11 wherever an X server
# answers — native, or the Xwayland every Wayland desktop runs anyway — and
# Wayland only when there is no X at all.
PLATFORM=""
XDISPLAY="${DISPLAY:-:0}"
case "$XDISPLAY" in
    :*) N="${XDISPLAY#:}"; N="${N%%.*}"
        if [ -S "/tmp/.X11-unix/X$N" ]; then PLATFORM=x11; fi ;;
    ?*) PLATFORM=x11 ;;          # host:0 — a remote X server, take its word
esac
if [ -z "$PLATFORM" ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then PLATFORM=wayland; fi
if [ -z "$PLATFORM" ]; then
    # Chromium's own message for this is "Missing X server or $DISPLAY", which
    # on a Wayland seat sends you looking for the wrong thing.
    echo "No display: neither an X server on ${XDISPLAY} nor WAYLAND_DISPLAY." >&2
    echo "Run this inside the graphical session (kiosk.sh --install does)." >&2
    exit 1
fi
if [ "$PLATFORM" = x11 ]; then export DISPLAY="$XDISPLAY"; fi

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
# X only: under Wayland this is an Xwayland setting that the real compositor
# never reads, which is why the locker is handled at --install instead.
if [ "$PLATFORM" = x11 ]; then xset s off -dpms s noblank 2>/dev/null || true; fi
command -v xscreensaver-command >/dev/null 2>&1 && xscreensaver-command -exit >/dev/null 2>&1 || true
command -v unclutter >/dev/null 2>&1 && (unclutter -idle 1 -root &) || true

# Power loss is the normal way this device is turned off, so the profile is
# almost always "crashed": clear the flags or every boot opens with a restore
# bubble over the page.
PREFS="$PROFILE/Default/Preferences"
[ -f "$PREFS" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/;s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS" || true

# Chromium by preference, Chrome if that is what the machine has: the flags
# below are the same engine's, and what identifies "our" browser to
# /kiosk/close is the profile directory, not the binary's name.
BROWSER=""
for b in chromium-browser chromium google-chrome-stable google-chrome; do
    BROWSER="$(command -v "$b" || true)"
    [ -n "$BROWSER" ] && break
done
if [ -z "$BROWSER" ]; then
    echo "No Chromium-based browser found (apt install chromium-browser)." >&2
    exit 1
fi

exec "$BROWSER" \
    --ozone-platform="$PLATFORM" \
    --user-data-dir="$PROFILE" \
    --kiosk "http://127.0.0.1:$PORT/?key=$KEY" \
    --noerrdialogs --disable-infobars --no-first-run --disable-translate \
    --disable-session-crashed-bubble --disable-features=TranslateUI \
    --overscroll-history-navigation=0 --disable-pinch \
    --password-store=basic \
    --check-for-update-interval=31536000
