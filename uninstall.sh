#!/bin/bash
# Uninstall MyAgent — the service and the installed code, NEVER your data.
#
# Usage:
#   ./uninstall.sh                # remove your own instance (user unit / macOS)
#   sudo ./uninstall.sh           # remove the system-wide instance(s) (root)
#   ./uninstall.sh --dry-run      # show what would be removed, change nothing
#   ./uninstall.sh --yes          # no confirmation prompt
#
# What goes:   the systemd unit (or the LaunchAgent) with its drop-ins, the
#              install directory named by that unit (the Python venv, the tools'
#              node_modules, the copied code), and any dev instance still running
#              from that directory.
# What stays:  the runtime state under MYAGENT_HOME (default ~/myagent of the
#              service user): config/ (agents, models, API key), sessions/,
#              memory/, library/, workspace/, connectors/, plugins/, tools/.
#              The `myagent` service account and its home stay too — the state
#              lives inside it. The script prints how to remove both by hand.
#
# Everything is derived from the INSTALLED unit, not guessed from a mode: the
# code directory is the unit's WorkingDirectory, the state directory is its
# MYAGENT_HOME (or the service user's ~/myagent). A directory that looks like a
# state tree (it holds config/ or sessions/) is never removed, whatever the unit
# says — in per-user installs the code sits INSIDE the state tree (~/myagent/bin)
# and the guard is what keeps `rm -rf` on the right side of that line.
set -e

SERVICE_NAME="myagent"
LABEL="com.myagent.agent"                       # launchd label (macOS)
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

DRY_RUN=""
ASSUME_YES="${MYAGENT_ASSUME_YES:-}"
while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        -h|--help)    sed -n '2,25p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)            echo "Unknown option: $1 (see $0 --help)" >&2; exit 2 ;;
    esac
    shift
done

has() { command -v "$1" >/dev/null 2>&1; }
run() {   # execute, or narrate under --dry-run
    if [ -n "$DRY_RUN" ]; then echo "  would: $*"; else "$@"; fi
}
ask() {
    [ -n "$ASSUME_YES" ] && return 0
    [ -t 0 ] || return 1
    printf '%s [y/N] ' "$1"
    read -r reply || return 1
    case "$reply" in [yY]|[yY][eE][sS]|[sS]|[sS][iI]) return 0 ;; *) return 1 ;; esac
}

IS_ROOT=""; [ "$(id -u)" -eq 0 ] && IS_ROOT=1

# ------------------------------------------------------------------ discovery
# One entry per installed instance: "kind|unit-or-plist|systemctl-prefix".
INSTANCES=()
if [ "$(uname)" = "Darwin" ]; then
    if [ -n "$IS_ROOT" ]; then
        echo "On macOS run this as your own user: the LaunchAgent is per-user." >&2
        exit 1
    fi
    PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
    [ -f "$PLIST" ] && INSTANCES+=("macos|$PLIST|")
elif [ -n "$IS_ROOT" ]; then
    # System units: the shared `myagent` (service account or root mode) and the
    # `myagent-<user>` fallbacks that install.sh writes when a user has no
    # systemd session.
    for f in /etc/systemd/system/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}-*.service; do
        [ -f "$f" ] && INSTANCES+=("system|$f|systemctl")
    done
else
    USER_UNIT="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
    [ -f "$USER_UNIT" ] && INSTANCES+=("user|$USER_UNIT|systemctl --user")
    # This user's no-session fallback is a SYSTEM unit: removable with sudo only.
    FALLBACK="/etc/systemd/system/${SERVICE_NAME}-$(id -un).service"
    if [ -f "$FALLBACK" ]; then
        if sudo -n true 2>/dev/null || { [ -t 0 ] && echo "The system unit $(basename "$FALLBACK") needs sudo." && sudo -v; }; then
            INSTANCES+=("system|$FALLBACK|sudo systemctl")
        else
            echo "NOTE: $FALLBACK exists but sudo is unavailable — left in place."
        fi
    fi
    if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
        echo "NOTE: a system-wide '$SERVICE_NAME' service exists on this machine. This run only"
        echo "      removes YOUR instance; for that one: sudo $0"
        echo ""
    fi
fi

# --- unit readers ---------------------------------------------------------
unit_value() {   # unit_value <unit-file> <Key>  (drop-ins may override: last wins)
    local f="$1" key="$2" v=""
    for src in "$f" "$f.d"/*.conf; do
        [ -f "$src" ] || continue
        local got
        got="$(sed -n "s/^${key}=//p" "$src" | tail -1)"
        [ -n "$got" ] && v="$got"
    done
    echo "$v"
}
unit_env() {     # unit_env <unit-file> <VAR>   (Environment=VAR=value lines)
    local f="$1" var="$2" v=""
    for src in "$f" "$f.d"/*.conf; do
        [ -f "$src" ] || continue
        local got
        got="$(sed -n "s/^Environment=${var}=//p" "$src" | tail -1)"
        [ -n "$got" ] && v="$got"
    done
    echo "$v"
}
plist_string() { # plist_string <plist> <Key> — the <string> right after <key>Key</key>
    grep -A1 "<key>$2</key>" "$1" | sed -n 's:.*<string>\(.*\)</string>.*:\1:p' | head -1
}
looks_like_state() {   # never rm a directory that holds runtime data
    [ -d "$1/config" ] || [ -d "$1/sessions" ] || [ -d "$1/memory" ] || [ -d "$1/workspace" ]
}

if [ ${#INSTANCES[@]} -eq 0 ]; then
    echo "No installed MyAgent service found for $(id -un)."
    # A --dev / in-place checkout has no unit: offer to strip its build artifacts.
    if [ -d "$SOURCE_DIR/server/.venv" ]; then
        echo "This checkout has a dev venv ($SOURCE_DIR/server/.venv)."
        if ask "Remove the venv and the web tools' node_modules from the checkout?"; then
            run rm -rf "$SOURCE_DIR/server/.venv" \
                       "$SOURCE_DIR/server/tools/browse_web/node_modules" \
                       "$SOURCE_DIR/server/tools/web_search/node_modules"
            echo "Done. Your data under ~/myagent was not touched."
        fi
    fi
    exit 0
fi

# ----------------------------------------------------------------------- plan
echo "=== MyAgent uninstall ==="
[ -n "$DRY_RUN" ] && echo "(dry run: nothing will be changed)"
PLAN_DIRS=()   # code directories to remove, one per instance (may repeat)
KEEP_STATE=()  # state directories that stay, for the report
for inst in "${INSTANCES[@]}"; do
    kind="${inst%%|*}"; rest="${inst#*|}"; unit="${rest%%|*}"; sc="${rest#*|}"
    if [ "$kind" = macos ]; then
        code="$(plist_string "$unit" WorkingDirectory)"
        state="$(plist_string "$unit" MYAGENT_HOME)"; [ -n "$state" ] || state="$HOME/myagent"
        user="$(id -un)"
    else
        code="$(unit_value "$unit" WorkingDirectory)"
        user="$(unit_value "$unit" User)"; [ -n "$user" ] || user="$(id -un)"
        state="$(unit_env "$unit" MYAGENT_HOME)"
        if [ -z "$state" ]; then
            home="$(getent passwd "$user" 2>/dev/null | cut -d: -f6)"; [ -n "$home" ] || home="$HOME"
            state="$home/myagent"
        fi
    fi
    echo ""
    echo "Instance: $(basename "$unit")   (runs as $user)"
    echo "  Service: $unit$(ls "$unit.d"/*.conf >/dev/null 2>&1 && echo "  + drop-ins in $unit.d/")"
    if [ -z "$code" ]; then
        echo "  Code:    (unit names no WorkingDirectory — nothing to remove)"
    elif looks_like_state "$code"; then
        echo "  Code:    $code — LOOKS LIKE A DATA DIRECTORY, will NOT be removed"
    elif [ "$(cd "$code" 2>/dev/null && pwd -P)" = "$(cd "$SOURCE_DIR" && pwd -P)" ]; then
        echo "  Code:    $code — this checkout (in-place install): only server/.venv and"
        echo "           the web tools' node_modules are removed, the sources stay"
        PLAN_DIRS+=("inplace:$code")
    else
        echo "  Code:    $code (removed)"
        PLAN_DIRS+=("dir:$code")
    fi
    echo "  Data:    $state (KEPT)"
    KEEP_STATE+=("$state")
done
echo ""
echo "Kept as well: the service account (if any) and its home, plugins and connector"
echo "state under the data directory, and the Ollama / llama.cpp models."
echo ""
if [ -z "$DRY_RUN" ] && ! ask "Proceed?"; then
    echo "Aborted — nothing was changed."
    exit 0
fi

# -------------------------------------------------------------------- remove
for inst in "${INSTANCES[@]}"; do
    kind="${inst%%|*}"; rest="${inst#*|}"; unit="${rest%%|*}"; sc="${rest#*|}"
    name="$(basename "$unit" .service)"
    echo ""
    echo "Removing $(basename "$unit")..."
    if [ "$kind" = macos ]; then
        run launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || run launchctl unload "$unit" 2>/dev/null || true
        run rm -f "$unit"
        # Service logs, not user data: written by launchd, named by install.sh.
        run rm -f "$HOME/Library/Logs/myagent.log" "$HOME/Library/Logs/myagent.err.log"
    else
        # Unquoted $sc on purpose: "sudo systemctl" / "systemctl --user" are words.
        run $sc stop "$name" 2>/dev/null || true
        run $sc disable "$name" 2>/dev/null || true
        if [ "${sc#sudo}" != "$sc" ]; then
            run sudo rm -rf "$unit" "$unit.d"
        else
            run rm -rf "$unit" "$unit.d"
        fi
        run $sc daemon-reload 2>/dev/null || true
        run $sc reset-failed "$name" 2>/dev/null || true
    fi
done

# Code directories: kill a dev instance still running from there (its process
# holds the venv's python open), then remove. The same pattern install.sh uses.
for entry in "${PLAN_DIRS[@]}"; do
    how="${entry%%:*}"; code="${entry#*:}"
    run pkill -f "$code/server/.venv/bin/python .*main\.py" 2>/dev/null || true
    if [ "$how" = inplace ]; then
        run rm -rf "$code/server/.venv" \
                   "$code/server/tools/browse_web/node_modules" \
                   "$code/server/tools/web_search/node_modules"
    else
        run rm -rf "$code"
    fi
done

echo ""
echo "=== Uninstall complete ==="
for s in "${KEEP_STATE[@]}"; do
    echo "Your data is still in $s — delete it yourself if you really want it gone:"
    echo "  rm -rf \"$s\""
done
if [ -n "$IS_ROOT" ] && id "$SERVICE_NAME" >/dev/null 2>&1; then
    echo "The service account '$SERVICE_NAME' still exists (its home holds the data above):"
    echo "  userdel -r $SERVICE_NAME        # removes the account AND /home/$SERVICE_NAME"
fi
echo "To reinstall later: ./install.sh from a git checkout — your agents, sessions"
echo "and settings are picked up again as they are."
