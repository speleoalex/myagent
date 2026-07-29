import json
import os
import shutil
from pathlib import Path

from app.models import Settings

# Directory layout (anchored by absolute paths, independent of the cwd):
#   <root>/server/            -> APP_DIR: the self-contained Python backend
#       app/  main.py  .venv/  requirements.txt  config/  tools/
#   <root>/ui/                -> the static frontend
# config.py is <root>/server/app/config.py, so APP_DIR is two levels up and the
# project root one more. The bundled seed templates (config/, tools/) live
# inside server/, so they are anchored to APP_DIR; the UI lives at the project
# root.
APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_DIR.parent

# User configuration (agents, models, settings) lives under the user's home so
# it is decoupled from the app install directory and survives redeploys. This is
# the small, precious directory to back up. Defaults to ~/myagent/config;
# override with the MYAGENT_CONFIG env var.
CONFIG_DIR = Path(
    os.environ.get("MYAGENT_CONFIG") or (Path.home() / "myagent" / "config")
).expanduser()
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Defaults shipped inside the app. On first run (when CONFIG_DIR doesn't exist
# yet) the runtime config dir is seeded from here.
DEFAULT_CONFIG_DIR = APP_DIR / "config"

# Set to True by ensure_config_dir() when it seeds CONFIG_DIR from the defaults,
# so the entrypoint can log a clear first-run message.
CONFIG_SEEDED = False


def ensure_config_dir() -> Path:
    """On first run, initialize CONFIG_DIR from the app's bundled defaults.

    If CONFIG_DIR already exists it is left untouched (never overwrites user
    data). If the bundled defaults are missing, just creates an empty CONFIG_DIR.
    """
    global CONFIG_SEEDED
    if not CONFIG_DIR.exists():
        if DEFAULT_CONFIG_DIR.is_dir() and DEFAULT_CONFIG_DIR.resolve() != CONFIG_DIR.resolve():
            shutil.copytree(DEFAULT_CONFIG_DIR, CONFIG_DIR)
            CONFIG_SEEDED = True
        else:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR

# MCP server definitions (one JSON file per server) and the cached tool
# catalogue discovered from them. Part of the config dir, so it rides
# MYAGENT_CONFIG and is covered by the same backup story.
MCP_DIR = CONFIG_DIR / "mcp"
MCP_CACHE_DIR = MCP_DIR / "cache"

# Tools the user (or the AI itself) creates or edits live outside the install
# dir — `deploy.sh`'s rsync --delete would wipe them on every redeploy.
# Defaults to ~/myagent/tools; override with the MYAGENT_TOOLS env var.
# This dir is the OVERLAY on the bundled catalog: native tools are served
# straight from DEFAULT_TOOLS_DIR with no install step, editing one copies it
# here first (copy-on-write), and deleting the copy restores the original.
TOOLS_DIR = Path(
    os.environ.get("MYAGENT_TOOLS") or (Path.home() / "myagent" / "tools")
).expanduser()

# Bundled native tool catalog shipped with the app (read-only underlay).
DEFAULT_TOOLS_DIR = APP_DIR / "tools"

def ensure_tools_dir() -> Path:
    """Make sure TOOLS_DIR exists. Nothing is seeded: the bundled catalog
    (DEFAULT_TOOLS_DIR) is served directly as the read-only underlay of the
    registry's overlay, and this dir only holds user/AI-created tools plus
    copy-on-write overrides of edited native tools."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    return TOOLS_DIR


# Working directory for the agents' file operations. Relative paths used by
# file/shell tools resolve here (see ToolRegistry / AgentExecutor). Defaults to
# ~/myagent/workspace; override with the MYAGENT_WORKSPACE env var.
WORKSPACE_DIR = Path(
    os.environ.get("MYAGENT_WORKSPACE") or (Path.home() / "myagent" / "workspace")
).expanduser()


def ensure_workspace() -> Path:
    """Create the working directory if it doesn't exist and return it."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_DIR


# Chat sessions live on disk under the user's home (decoupled from the browser).
# One file per chat: the active chat is sessions/current.json; starting a new
# chat archives it into sessions/history/. Override with MYAGENT_SESSIONS.
SESSIONS_DIR = Path(
    os.environ.get("MYAGENT_SESSIONS") or (Path.home() / "myagent" / "sessions")
).expanduser()


def ensure_sessions() -> Path:
    """Create the sessions directory (and its history subdir)."""
    (SESSIONS_DIR / "history").mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


# Per-agent deep memory (summary tree + archived conversation chunks). Written
# by the memory compactor and read by the memory_* tools; strictly one
# subdirectory per agent id. Defaults to ~/myagent/memory; override with the
# MYAGENT_MEMORY env var.
MEMORY_DIR = Path(
    os.environ.get("MYAGENT_MEMORY") or (Path.home() / "myagent" / "memory")
).expanduser()


def ensure_memory() -> Path:
    """Create the deep-memory directory if it doesn't exist and return it."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return MEMORY_DIR


# Autonomy runtime state (event queues + per-agent scheduler state), one
# subdirectory per live agent. Defaults to ~/myagent/autonomy; override with
# the MYAGENT_AUTONOMY env var.
AUTONOMY_DIR = Path(
    os.environ.get("MYAGENT_AUTONOMY") or (Path.home() / "myagent" / "autonomy")
).expanduser()


def ensure_autonomy() -> Path:
    """Create the autonomy state directory if it doesn't exist and return it."""
    AUTONOMY_DIR.mkdir(parents=True, exist_ok=True)
    return AUTONOMY_DIR


# A channel (connector) session is one perpetual file — unlike web chats it is
# never rotated by "new chat", and since it records the same full format
# (traces, attachments) it would grow without bound. Once the file exceeds
# this size, the session is archived into the web history and restarted with
# the same compact LLM conversation (the bot keeps its context).
CHANNEL_ROTATE_BYTES = int(
    os.environ.get("MYAGENT_CHANNEL_ROTATE_BYTES") or 2 * 1024 * 1024
)


# Optional API key protecting the whole /api surface. Empty (default) = no
# authentication — fine for a private localhost install. When set, every /api
# request must present it, either as "Authorization: Bearer <key>" or as an
# "api_key" query parameter (usable in plain GET links and by clients that
# cannot set headers). The web UI accepts ?api_key=... in the page URL once
# and stores it locally.
API_KEY = os.environ.get("MYAGENT_API_KEY", "")

# Verbose per-iteration executor tracing (messages sent to the LLM, tool
# results, dedup decisions). Off by default: it logs full chat content, so it
# is opt-in via MYAGENT_DEBUG=1 and written under the user's home (not /tmp,
# which is world-readable). Path overridable with MYAGENT_DEBUG_FILE.
DEBUG = os.environ.get("MYAGENT_DEBUG", "") not in ("", "0", "false")
DEBUG_LOG_FILE = Path(
    os.environ.get("MYAGENT_DEBUG_FILE")
    or (Path.home() / "myagent" / "logs" / "debug.log")
).expanduser()
if DEBUG:
    # Create the log's parent dir up front: the executor's debug writes are
    # wrapped in try/except and would otherwise fail silently.
    try:
        DEBUG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            return Settings(**json.load(f))
    return Settings()


def save_settings(s: Settings) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s.model_dump(), f, indent=2)


# Seed the config dir from defaults on first run before loading settings.
ensure_config_dir()
settings = load_settings()
