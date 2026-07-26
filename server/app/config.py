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

# Tools are runtime data too: users (and the AI itself) create/edit tools via
# the API, so they must live outside the install dir or `deploy.sh`'s
# rsync --delete would wipe them on every redeploy. Defaults to
# ~/myagent/tools, seeded on first run from the app's bundled tools/;
# override with the MYAGENT_TOOLS env var.
TOOLS_DIR = Path(
    os.environ.get("MYAGENT_TOOLS") or (Path.home() / "myagent" / "tools")
).expanduser()

# Bundled tool templates shipped with the app (seed source, like DEFAULT_CONFIG_DIR).
DEFAULT_TOOLS_DIR = APP_DIR / "tools"

# Set to True by ensure_tools_dir() when it seeds TOOLS_DIR from the defaults.
TOOLS_SEEDED = False


def ensure_tools_dir() -> Path:
    """On first run, initialize TOOLS_DIR from the app's bundled tools.

    If TOOLS_DIR already exists it is left untouched (user/AI-created tools are
    never overwritten). If the bundled tools are missing, creates an empty dir.
    """
    global TOOLS_SEEDED
    if not TOOLS_DIR.exists():
        if DEFAULT_TOOLS_DIR.is_dir() and DEFAULT_TOOLS_DIR.resolve() != TOOLS_DIR.resolve():
            shutil.copytree(
                DEFAULT_TOOLS_DIR,
                TOOLS_DIR,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            TOOLS_SEEDED = True
        else:
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
