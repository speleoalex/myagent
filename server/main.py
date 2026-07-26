import hmac
import logging
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.config import (
    APP_DIR,
    PROJECT_ROOT,
    CONFIG_DIR,
    DEFAULT_CONFIG_DIR,
    CONFIG_SEEDED,
    TOOLS_DIR,
    DEFAULT_TOOLS_DIR,
    WORKSPACE_DIR,
    SESSIONS_DIR,
    ensure_tools_dir,
    ensure_workspace,
    ensure_sessions,
)
from app.engine.executor import Stores
from app.engine.live import LiveRunManager
from app.storage.store import JsonStore
from app.storage.sessions import SessionStore
from app.storage.channel_sessions import NamedSessionStore
from app.tools.registry import ToolRegistry
from app.tools.internal import call_agent_handler
from app.routers import agents, tools, llm_models, chat, system, sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("myagent")

app = FastAPI(title="MyAgent", version="0.1.0")

# Optional API-key gate (MYAGENT_API_KEY). When set, it protects the API and
# the OpenAPI docs; the static UI stays public (it holds no data — it prompts
# for the key on the first 401). The key is accepted as a Bearer header or as
# an ?api_key= query parameter (for plain GET links and header-less clients).
if config.API_KEY:
    @app.middleware("http")
    async def require_api_key(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json"):
            auth = request.headers.get("authorization", "")
            candidate = auth[7:] if auth.lower().startswith("bearer ") else \
                request.query_params.get("api_key", "")
            if not hmac.compare_digest(candidate.encode(), config.API_KEY.encode()):
                return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
        return await call_next(request)

# Initialize stores (config lives under the user's home, see config.CONFIG_DIR)
if CONFIG_SEEDED:
    log.info("Initialized config directory %s from defaults (%s)", CONFIG_DIR, DEFAULT_CONFIG_DIR)
else:
    log.info("Config directory: %s", CONFIG_DIR)
stores = Stores(
    agents=JsonStore(CONFIG_DIR / "agents"),
    models=JsonStore(CONFIG_DIR / "models"),
)

# Working directory for agent file operations (created under the user's home)
ensure_workspace()
log.info("Agent working directory: %s", WORKSPACE_DIR)

# Initialize tool registry (folder-based). Tools are runtime data: they live
# under the user's home (seeded from the bundled tools/ on first run) so that
# user/AI-created tools survive redeploys.
ensure_tools_dir()
if config.TOOLS_SEEDED:
    log.info("Initialized tools directory %s from defaults (%s)", TOOLS_DIR, DEFAULT_TOOLS_DIR)
else:
    log.info("Tools directory: %s", TOOLS_DIR)
tool_registry = ToolRegistry(TOOLS_DIR, workdir=WORKSPACE_DIR, app_dir=APP_DIR)
tool_registry.register_internal("call_agent", call_agent_handler)

# Chat sessions on disk (one file per chat under the user's home)
ensure_sessions()
log.info("Chat sessions directory: %s", SESSIONS_DIR)
session_store = SessionStore(SESSIONS_DIR)

# Channel-scoped sessions: independent, persistent conversations addressed by
# an external key (one per messaging chat), used by external connectors.
# Namespaced under sessions/channels/, kept separate from the web UI's
# current/history flow.
named_sessions = NamedSessionStore(SESSIONS_DIR)

# Manages background (client-decoupled) chat generations for resume/stop
live_runs = LiveRunManager()

# Store in app state
app.state.stores = stores
app.state.tool_registry = tool_registry
app.state.sessions = session_store
app.state.named_sessions = named_sessions
app.state.live = live_runs

# Include API routers
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
app.include_router(llm_models.router, prefix="/api/models", tags=["models"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(system.router, prefix="/api/system", tags=["system"])

# Serve the frontend (static UI lives in <root>/ui, separate from the server).
STATIC_DIR = PROJECT_ROOT / "ui"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    # Bind to localhost by default: the API has no authentication and ships
    # tools that execute shell commands. Set MYAGENT_HOST=0.0.0.0 only on a
    # trusted network (or put an authenticating reverse proxy in front).
    host = os.environ.get("MYAGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("MYAGENT_PORT", "8888"))
    log.info("Starting MyAgent on http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port)
