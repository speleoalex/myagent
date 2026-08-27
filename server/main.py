import asyncio
import functools
import hmac
import logging
import mimetypes
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.config import (
    APP_DIR,
    PROJECT_ROOT,
    CONFIG_DIR,
    DEFAULT_CONFIG_DIR,
    CACHE_DIR,
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
from app.engine.index_service import IndexService
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
    notify_agent_owner,
    notify_targets,
    notify_user_handler,
    recall_delegation_handler,
)
from app.tools.memory_tools import (
    memory_search_handler,
    memory_read_handler,
    memory_note_handler,
)
from app.plugins import load_plugins, start_plugins, stop_plugins
from app.routers import (agents, tools, llm_models, chat, mcp, system, sessions,
                         autonomy, files, index, plugins, tasks)

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
    # Builds the semantic indexes the search tools ask for, and only those:
    # it scans one directory for request files and does nothing when there are
    # none, so an install that never uses semantic search never starts a run.
    app.state.index.start()
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
    try:
        # An indexer is a child process: it must not outlive us. Its work is
        # committed one file at a time, so being cut off costs nothing.
        await app.state.index.aclose()
    except Exception as e:
        log.warning("Index service shutdown did not complete cleanly: %s", e)
    manager = getattr(app.state, "mcp", None)
    if manager is not None:
        try:
            await asyncio.wait_for(manager.aclose(), timeout=MCP_SHUTDOWN_TIMEOUT)
        except Exception as e:
            log.warning("MCP shutdown did not complete cleanly: %s", e)


app = FastAPI(title="MyAgent", version="0.1.0", lifespan=lifespan)

# API paths that carry their OWN authentication (e.g. the connectors plugin's
# device inbound, gated by a per-binding shared key): the global API-key
# middleware skips them. A plugin adds its prefix in register(); the set is
# read at request time, so registration order does not matter. INVARIANT: a
# prefix listed here MUST enforce its own credential — this is an auth
# handoff, never an auth exemption.
app.state.self_authenticated_prefixes = set()

# Optional API-key gate. When a key is set, it protects the API and the OpenAPI
# docs; the static UI stays public (it holds no data — it prompts for the key on
# the first 401). The key is accepted as a Bearer header or as an ?api_key=
# query parameter (for plain GET links and header-less clients).
#
# Registered UNCONDITIONALLY, and the key is resolved per request
# (config.get_api_key(), mtime-cached): the key can be created, rotated or
# removed at runtime from Settings, and middleware cannot be added once the app
# is serving. No key = every request passes, i.e. today's open localhost install.
@app.middleware("http")
async def require_api_key(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json"):
        self_auth = getattr(app.state, "self_authenticated_prefixes", ())
        if not any(path.startswith(p) for p in self_auth):
            key = config.get_api_key()
            if key:
                auth = request.headers.get("authorization", "")
                candidate = auth[7:] if auth.lower().startswith("bearer ") else \
                    request.query_params.get("api_key", "")
                if not hmac.compare_digest(candidate.encode(), key.encode()):
                    return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    return await call_next(request)

# Optional CORS consent (MYAGENT_CORS_ORIGINS) for a UI hosted off this server:
# the frontend is static HTML, so Apache/nginx can carry it and point back here
# (Settings → "MyAgent server"), but the browser only allows that cross-origin
# traffic if we say so. Unset = no CORS layer, same-origin only. Added AFTER the
# API-key middleware on purpose: Starlette runs the last-added middleware
# OUTERMOST, and the preflight OPTIONS carries no Authorization header — the
# key check would 401 it before CORS could answer. allow_headers covers the
# Bearer header, so key auth and CORS compose.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("CORS enabled for origins: %s", ", ".join(config.CORS_ORIGINS))

# Seed the config dir from the bundled defaults on first run, then re-read the
# settings the seed just wrote. Done HERE rather than as an import side effect
# of app.config: importing that module must not write to the user's home.
# CONFIG_SEEDED is read through the module (not imported by value) because it
# is only set by the call on the line above.
config.ensure_config_dir()
config.reload_settings()
if config.CONFIG_SEEDED:
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
                             bundled_dir=DEFAULT_TOOLS_DIR,
                             tool_env=config.tool_env())
tool_registry.register_internal("call_agent", call_agent_handler)
# The other half of delegation: what a sub-agent reported in a PAST turn is
# nowhere in conversation[] (a `tool` message never survives the scaffolding
# filter), so it is read back from the session on demand.
tool_registry.register_internal("recall_delegation", recall_delegation_handler)
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
# The semantic-index builder. Constructed here, started in the lifespan, and
# deliberately given the registry rather than the tool path: semindex.py ships
# INSIDE local_search, which the user may have overridden copy-on-write, and
# the registry is what already resolves that.
index_service = IndexService(CACHE_DIR, tool_registry, stores.models)

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
# The scheduler's own voice. An agent whose wakes keep failing cannot report it
# (it never runs), so the SERVICE says it, through that agent's configured notify
# target. Same late, lazy app.state closure as notify_user above: the connectors
# plugin that delivers is loaded further down.
autonomy_service.send_notification = functools.partial(
    notify_agent_owner, named=named_sessions, state=app.state)

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
app.state.index = index_service

# Include API routers
app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
app.include_router(llm_models.router, prefix="/api/models", tags=["models"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(mcp.router, prefix="/api/mcp", tags=["mcp"])
app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(autonomy.router, prefix="/api/autonomy", tags=["autonomy"])
app.include_router(index.router, prefix="/api/index", tags=["index"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
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
# The web-app manifest makes the UI installable; Python's mimetypes table does
# not know the extension, so StaticFiles would guess and browsers reject a
# manifest served as text/plain.
mimetypes.add_type("application/manifest+json", ".webmanifest")
STATIC_DIR = PROJECT_ROOT / "ui"
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:
    # Deliberately NOT mkdir'd. An empty directory mounts happily and then
    # answers 404 for every page, which reads as "the UI is broken" when what
    # actually happened is that the install is incomplete. The API is fully
    # usable without it (connectors, satellite, a UI hosted elsewhere), so this
    # is a loud line in the log rather than a refusal to start.
    log.error("UI directory not found: %s — the API is up, but this server "
              "serves no web interface. Re-run the installer, or point a "
              "browser at a UI hosted elsewhere (Settings → MyAgent server).",
              STATIC_DIR)

def _tls_files():
    """(certfile, keyfile) for uvicorn, validated — or (None, None) for plain http.

    Serving TLS here removes the need for a reverse proxy, and the reason to
    want it is usually the browser rather than the wire: an installable UI
    (see ui/js/pwa.js) requires a secure context, which localhost satisfies but
    a LAN or VPN address in plain http does not.

    The key is optional: ssl.load_cert_chain reads it from the certificate file
    when that file is a combined PEM, which is what several ACME clients emit.
    """
    certfile = os.environ.get("MYAGENT_SSL_CERTFILE", "").strip() or None
    keyfile = os.environ.get("MYAGENT_SSL_KEYFILE", "").strip() or None
    if keyfile and not certfile:
        raise SystemExit("MYAGENT_SSL_KEYFILE is set without MYAGENT_SSL_CERTFILE")
    # Checked here rather than left to uvicorn: a typo in a path surfaces as a
    # bare FileNotFoundError from inside the ssl module, at which point it is
    # not obvious which of the two settings is wrong.
    for var, path in (("MYAGENT_SSL_CERTFILE", certfile), ("MYAGENT_SSL_KEYFILE", keyfile)):
        if path and not os.path.isfile(path):
            raise SystemExit(f"{var}: no such file: {path}")
    return certfile, keyfile


if __name__ == "__main__":
    # Bind to localhost by default: the API has no authentication and ships
    # tools that execute shell commands. Set MYAGENT_HOST=0.0.0.0 only on a
    # trusted network (or put an authenticating reverse proxy in front).
    host = os.environ.get("MYAGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("MYAGENT_PORT", "8888"))
    ssl_certfile, ssl_keyfile = _tls_files()
    log.info("Starting MyAgent on %s://%s:%d",
             "https" if ssl_certfile else "http", host, port)
    uvicorn.run(app, host=host, port=port,
                ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
