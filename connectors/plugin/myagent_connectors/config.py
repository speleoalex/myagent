"""Configuration for the connectors plugin.

The plugin runs inside myagent's own process, so nothing here describes a
server: no host, no port, and no API url or token — it calls the engine
directly. What is left is where the state lives and how the channels are tuned.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- runtime state (bindings, grants, contacts) ------------------------------
# Kept under the user's home, NOT inside the plugin directory: the plugin dir
# holds code and is replaced wholesale on reinstall (rsync --delete), while this
# holds the bot tokens and the address book. It also predates the plugin layout,
# so leaving it here makes the migration a no-op.
STATE_DIR = Path(
    os.environ.get("MYAGENT_CONNECTORS_DIR") or (Path.home() / "myagent" / "connectors")
).expanduser()
BINDINGS_DIR = STATE_DIR / "bindings"
GRANTS_DIR = STATE_DIR / "grants"  # password-mode authorized user ids per binding
CONTACTS_DIR = STATE_DIR / "contacts"  # address book (name + messaging ids)
# Plugin-level runtime state (currently the persisted kill switch). A file, not
# a directory: it is a single small document.
STATE_FILE = STATE_DIR / "state.json"

# --- shared tuning ----------------------------------------------------------
# Only knobs that belong to EVERY channel live here; a transport's own settings
# (e.g. a long-poll timeout) belong to its channel module.
# How long one agent turn may take before the connector gives up on it. As a
# separate server this came for free from the HTTP request timeout; in-process
# there is no socket to time out, and without a wall a wedged turn would leave
# that chat permanently "busy" until the server restarts.
CHAT_TIMEOUT = float(os.environ.get("MYAGENT_CHAT_TIMEOUT") or 180)
# How many inbound turns may run at once, across all bots. Their turns share the
# process (and the local model) with the web UI: unbounded, a burst of messages
# would make the UI unusable.
MAX_CONCURRENT_TURNS = int(os.environ.get("MYAGENT_CONNECTORS_CONCURRENCY") or 2)
# Consecutive failures after which a connector pauses itself instead of
# retrying forever (mirrors the autonomy scheduler's auto-pause).
MAX_CONSECUTIVE_ERRORS = int(os.environ.get("MYAGENT_CONNECTORS_MAX_ERRORS") or 10)


def ensure_dirs() -> None:
    BINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    GRANTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
