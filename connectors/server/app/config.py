"""Configuration for the standalone connectors server.

This server is independent of myagent's sources: it talks to myagent only over
its HTTP API. Everything here is overridable via environment variables so the
same code runs in dev and under a service manager.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- myagent API (the agent runtime this bridge drives) ---------------------
# Base URL of the myagent server. The bridge POSTs chat turns here.
MYAGENT_API_URL = os.environ.get("MYAGENT_API_URL", "http://localhost:8888").rstrip("/")
# Optional bearer token, if the myagent API is configured to require one.
MYAGENT_API_TOKEN = os.environ.get("MYAGENT_API_TOKEN", "")

# --- this server ------------------------------------------------------------
HOST = os.environ.get("MYAGENT_CONNECTORS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MYAGENT_CONNECTORS_PORT", "8899"))
# Bearer key required by POST /api/bindings/{id}/send (unsolicited outbound
# messages, used by myagent's notify_user tool). Empty = open — same
# convention as myagent's MYAGENT_API_KEY; fine on localhost.
SEND_API_KEY = os.environ.get("MYAGENT_CONNECTORS_API_KEY", "")

# --- runtime state (bindings + password grants) -----------------------------
# Kept under the user's home (not inside the source tree) so it survives
# redeploys — same principle as myagent's ~/myagent/config. Lives inside the
# shared ~/myagent root so all runtime state sits under one tree.
STATE_DIR = Path(
    os.environ.get("MYAGENT_CONNECTORS_DIR", str(Path.home() / "myagent" / "connectors"))
)
BINDINGS_DIR = STATE_DIR / "bindings"
GRANTS_DIR = STATE_DIR / "grants"  # password-mode authorized user ids per binding
CONTACTS_DIR = STATE_DIR / "contacts"  # address book (name + messaging ids)

# Where the admin UI static files live (shipped with the server).
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../connectors/server
UI_DIR = PROJECT_ROOT / "ui"

# --- connector tuning -------------------------------------------------------
# Telegram long-poll timeout (seconds). getUpdates blocks up to this long.
TELEGRAM_POLL_TIMEOUT = int(os.environ.get("MYAGENT_TELEGRAM_POLL_TIMEOUT", "30"))
# How long we allow a single agent turn to take (myagent may run local models).
CHAT_TIMEOUT = float(os.environ.get("MYAGENT_CHAT_TIMEOUT", "180"))


def ensure_dirs() -> None:
    BINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    GRANTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
