import asyncio
import functools
import hmac
import logging
import os
from contextlib import asynccontextmanager

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
    MCP_CACHE_DIR,
    MCP_DIR,
    TOOLS_DIR,
    DEFAULT_TOOLS_DIR,
    WORKSPACE_DIR,
    SESSIONS_DIR,
    MEMORY_DIR,
    AUTONOMY_DIR,
    ensure_tools_dir,
    ensure_workspace,
    ensure_sessions,
    ensure_memory,
    ensure_autonomy,
)
from app.engine.autonomy import AutonomyService
from app.engine.executor import Stores
from app.engine.live import LiveRunManager
from app.mcp.manager import McpManager
from app.storage.store import JsonStore
from app.storage.sessions import SessionStore
from app.storage.channel_sessions import NamedSessionStore
from app.storage.memory import MemoryStore
from app.storage.tasks import TaskStore
from app.tools.registry import ToolRegistry
from app.tools.internal import (
    autonomy_control_handler,
    call_agent_handler,
    manage_tasks_handler,
    notify_targets,
    notify_user_handler,
)
from app.tools.memory_tools import (
    memory_search_handler,
    memory_read_handler,
    memory_note_handler,
)
from app.plugins import load_plugins, start_plugins, stop_plugins
from app.routers import (agents, tools, llm_models, chat, mcp, system, sessions,
                         autonomy, plugins, tasks)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("myagent")

# How long shutdown waits for MCP connections to close. A hung server must not
# wedge `systemctl restart`.
MCP_SHUTDOWN_TIMEOUT = float(os.environ.get("MYAGENT_MCP_SHUTDOWN_TIMEOUT", "10"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: launch the autonomy scheduler (its loop must live inside the
    running event loop). Shutdown: stop in-flight wakes, then the MCP servers
    (stdio ones are child processes). Everything else is still wired at import
    time below; app.state is read here at call time, so startup ordering is
    unaffected.
    """
    app.state.autonomy.start()
    # Plugins start last and stop first: they are PRODUCERS of agent turns (an
    # inbound message drives the executor and may schedule a wake), so the
    # engines they feed must be up before them and still up while they drain.
    await start_plugins(app)
    yield
    await stop_plugins(app)
    try:
        await app.state.autonomy.aclose()
    except Exception as e:
        log.warning("Autonomy shutdown did not complete cleanly: %s", e)
    manager = getattr(app.state, "mcp", None)
    if manager is not None:
        try:
            await asyncio.wait_for(manager.aclose(), timeout=MCP_SHUTDOWN_TIMEOUT)
        except Exception as e:
            log.warning("MCP shutdown did not complete cleanly: %s", e)


app = FastAPI(title="MyAgent", version="0.1.0", lifespan=lifespan)

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
# Per-agent long-term memory (memory.md + Markdown chunks). Opt-in per agent
# via Agent.memory_enabled — nothing is written for agents that don't enable it.
ensure_memory()
log.info("Memory directory: %s", MEMORY_DIR)
memory_store = MemoryStore(MEMORY_DIR)

stores = Stores(
    agents=JsonStore(CONFIG_DIR / "agents"),
    models=JsonStore(CONFIG_DIR / "models"),
    memory=memory_store,
)

# Working directory for agent file operations (created under the user's home)
ensure_workspace()
log.info("Agent working directory: %s", WORKSPACE_DIR)

# Initialize tool registry: the bundled catalog (read-only, always present)
# overlaid by the user dir, which holds custom tools and copy-on-write
# overrides of edited native tools — those survive redeploys.
ensure_tools_dir()
log.info("Tools: user dir %s over bundled catalog %s", TOOLS_DIR, DEFAULT_TOOLS_DIR)
tool_registry = ToolRegistry(TOOLS_DIR, workdir=WORKSPACE_DIR, app_dir=APP_DIR,
                             bundled_dir=DEFAULT_TOOLS_DIR)
tool_registry.register_internal("call_agent", call_agent_handler)
tool_registry.register_internal("memory_search", memory_search_handler)
tool_registry.register_internal("memory_read", memory_read_handler)
tool_registry.register_internal("memory_note", memory_note_handler)

# External MCP servers: their tools join the registry as a second source. No
# connection is opened here — servers are started lazily, only for the agents
# that actually reference their tools (see ToolRegistry.ensure_mcp). The
# manager OWNS its config store (save_config/delete_config reload the cache),
# so the store is not exposed on app.state — the router goes through the manager.
mcp_manager = McpManager(JsonStore(MCP_DIR), JsonStore(MCP_CACHE_DIR), WORKSPACE_DIR)
tool_registry.mcp_manager = mcp_manager
if mcp_manager.server_ids():
    log.info("MCP servers configured: %s", ", ".join(sorted(mcp_manager.server_ids())))

# Chat sessions on disk (one file per chat under the user's home)
ensure_sessions()
log.info("Chat sessions directory: %s", SESSIONS_DIR)
session_store = SessionStore(SESSIONS_DIR)

# Channel-scoped sessions: independent, persistent conversations addressed by
# an external key (one per messaging chat), used by external connectors.
# Namespaced under sessions/channels/, kept separate from the web UI's
# current/history flow. The web store is handed over as the archive target:
# rotation (save_rotating) and /reset both park closed logs in the web history.
named_sessions = NamedSessionStore(SESSIONS_DIR, archive=session_store)

# Manages background (client-decoupled) chat generations for resume/stop
live_runs = LiveRunManager()

# Autonomy: the scheduled-task list + the scheduler that runs the due tasks of
# live agents. Tasks live in CONFIG_DIR (user intent, backed up with the rest);
# AUTONOMY_DIR holds only the scheduler's runtime state. Constructed here,
# started in the lifespan.
ensure_autonomy()
task_store = TaskStore(CONFIG_DIR / "tasks")
autonomy_service = AutonomyService(
    stores, tool_registry, named_sessions,
    live_runs, task_store, AUTONOMY_DIR,
)
# notify_user is registered here, not with the others above: besides sending, it
# appends the message to the target chat's OWN conversation, so it needs the named
# session store (which only exists further down). Without that append the user sees
# the notification in Telegram but the agent does not — ask it to repeat and it
# repeats the turn BEFORE the notification.
# _state is app.state itself, not a snapshot of it: a connectors plugin puts its
# services there further down (load_plugins), so the lookup has to happen when
# the tool runs, not when it is bound.
tool_registry.register_internal(
    "notify_user",
    functools.partial(notify_user_handler, _named=named_sessions, _state=app.state),
)
# The other half of notify_user: the executor pins the tool's 'to' parameter to the
# names in the address book, so it needs to READ that book while it builds a turn's
# tool definitions. Same late, lazy app.state closure as above — the plugin that
# owns the contacts is loaded further down.
tool_registry.notify_targets = lambda: notify_targets(app.state)
# manage_tasks needs the task store: bound here (underscore name so a model
# hallucinating a "_tasks" argument can't collide silently — it just errors).
tool_registry.register_internal(
    "manage_tasks", functools.partial(manage_tasks_handler, _tasks=task_store)
)
# autonomy_control lets an agent switch ITS OWN live mode on/off from a chat
# ("start yourself" / "stop yourself" / "are you active?").
tool_registry.register_internal(
    "autonomy_control",
    functools.partial(autonomy_control_handler, _autonomy=autonomy_service),
)

# Store in app state
app.state.stores = stores
app.state.tool_registry = tool_registry
app.state.mcp = mcp_manager
app.state.sessions = session_store
app.state.named_sessions = named_sessions
app.state.live = live_runs
app.state.memory = memory_store
app.state.tasks = task_store
app.state.autonomy = autonomy_service

# Include API routers
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
app.include_router(llm_models.router, prefix="/api/models", tags=["models"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(mcp.router, prefix="/api/mcp", tags=["mcp"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(autonomy.router, prefix="/api/autonomy", tags=["autonomy"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(plugins.router, prefix="/api/plugins", tags=["plugins"])

# Optional plugins installed under ~/myagent/plugins (see docs/PLUGINS.md).
# Mounted HERE, after the core routers and BEFORE the static catch-all below:
# Starlette matches routes in registration order, so anything registered after
# the "/" mount is unreachable — and it fails as a 404, indistinguishable from
# "this plugin is not installed".
app.state.plugins = load_plugins(app)
if app.state.plugins:
    log.info("Plugins: %s", ", ".join(
        f"{p.id}{'' if p.loaded else ' (FAILED)'}" for p in app.state.plugins.values()
    ))

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
