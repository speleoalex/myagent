import json
import logging
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
            # copytree preserves the repo's file modes; settings.json may
            # later hold secrets (model API keys), so normalize it to the
            # same 0600 every save_settings() write uses.
            try:
                os.chmod(CONFIG_DIR / "settings.json", 0o600)
            except OSError:
                pass
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


# Per-agent long-term memory (memory.md + Markdown summary chunks). Written
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


# Optional plugins: one directory per plugin, each with a plugin.py exposing
# register(app) (see docs/PLUGINS.md and app/plugins.py). Defaults to
# ~/myagent/plugins; override with the MYAGENT_PLUGINS env var.
#
# There is deliberately NO bundled underlay here, unlike TOOLS_DIR: a plugin
# shipped inside the app would be code for an optional online service sitting
# in an install that is meant to work offline. Plugins are installed
# separately, and an install without this directory is the normal case — the
# loader treats it as "no plugins", not as an error, so nothing creates it.
PLUGINS_DIR = Path(
    os.environ.get("MYAGENT_PLUGINS") or (Path.home() / "myagent" / "plugins")
).expanduser()


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
#
# TWO sources, because they answer different needs: MYAGENT_API_KEY is the
# deployment-level pin (systemd drop-in, container env, read-only install) and
# CONFIG_DIR/api_key is the runtime one, so the key can be created, rotated or
# removed from the UI — no sudo, no restart (a restart would kill in-flight
# turns), and it survives redeploys like the rest of CONFIG_DIR. The env var
# WINS when set: whoever launched the process must be able to fix the
# credential, and an API call that could overwrite it would be a way around
# that decision — hence api_key_status()["editable"], which the UI honors by
# showing the field read-only instead of offering a button that would 409.
API_KEY_ENV = os.environ.get("MYAGENT_API_KEY", "")
API_KEY_FILE = CONFIG_DIR / "api_key"

# (mtime_ns, key) of the last read file, so the gate can resolve the key on
# EVERY request without a read per request. Invalidated explicitly by
# set_api_key(): mtime has finite resolution and two rotations inside one tick
# would otherwise keep serving the old key.
_api_key_cache: tuple[int, str] | None = None


def get_api_key() -> str:
    """The key the gate must compare against, right now ("" = no auth)."""
    global _api_key_cache
    if API_KEY_ENV:
        return API_KEY_ENV
    try:
        mtime = API_KEY_FILE.stat().st_mtime_ns
    except OSError:
        _api_key_cache = None
        return ""
    cached = _api_key_cache
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        # Present but unreadable: refusing every request would lock the user out
        # of their own server with no way back through the UI, so fail open and
        # say why (the same choice load_settings() makes for a corrupt file).
        logging.getLogger(__name__).warning(
            "api_key file unreadable (%s): API left unauthenticated", e)
        return ""
    _api_key_cache = (mtime, key)
    return key


def set_api_key(key: str) -> str:
    """Store (or, with an empty key, remove) the runtime API key. Returns it."""
    global _api_key_cache
    key = (key or "").strip()
    ensure_config_dir()
    if key:
        # 0600 from the moment it exists: O_CREAT's mode only applies when the
        # file is created, so chmod covers a pre-existing looser one too.
        fd = os.open(API_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        os.chmod(API_KEY_FILE, 0o600)
    else:
        API_KEY_FILE.unlink(missing_ok=True)
    _api_key_cache = None
    return key


def api_key_status() -> dict:
    """What the UI needs to render the key box: value, origin, editability."""
    key = get_api_key()
    return {
        "key": key,
        "configured": bool(key),
        "source": "env" if API_KEY_ENV else ("file" if key else None),
        "editable": not API_KEY_ENV,
        "env_var": "MYAGENT_API_KEY",
    }

# Browser origins allowed to call the API cross-origin (CORS), for when the
# static UI is hosted by another web server (Apache, nginx — it is plain HTML)
# and pointed back at this API via Settings → "MyAgent server". Comma-separated
# origins, e.g. "https://intranet.example.com,http://pc2:8080"; "*" allows any.
# Empty (default) adds no CORS layer at all: same-origin only, today's
# behavior. Non-browser clients (curl, connectors) are never affected — CORS
# is a browser rule, not authentication; that is what MYAGENT_API_KEY is for.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("MYAGENT_CORS_ORIGINS", "").split(",")
    if o.strip()
]

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
    """Load settings, degrading to defaults on ANY problem. This runs at
    import time: a truncated/corrupt settings.json must never keep the whole
    server from booting (every other JSON reader in the app is tolerant)."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                return Settings(**json.load(f))
        except Exception as e:
            logging.getLogger(__name__).warning(
                "settings.json unreadable (%s): starting with defaults", e)
    return Settings()


def save_settings(s: Settings) -> None:
    # Atomic + 0600, like JsonStore: settings may hold model API keys,
    # and a crash mid-write must not leave a truncated file behind.
    # Imported here so config keeps zero app imports at module-load time
    # (everything imports config first; a future storage->config import
    # must not become a cycle).
    from app.storage.sessions import write_json
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(SETTINGS_FILE, s.model_dump(), mode=0o600)


# Seed the config dir from defaults on first run before loading settings.
ensure_config_dir()
settings = load_settings()
