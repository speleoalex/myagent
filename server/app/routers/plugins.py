"""Optional plugins: what is installed, and installing them from Settings.

The listing is read-only and never fails: an install with no plugins is the
normal case, so it answers with an empty list rather than an error. The UI uses
it to decide whether to show a plugin's menu entry — a plugin that is installed
but failed to load is reported with its error, not hidden.

A plugin's own routes simply do not exist when it isn't loaded, so they 404.
The core does not shim them: knowing which prefixes to shim would mean naming
a specific plugin here.

Installing is app/plugin_installer.py's work; these routes only start it and
report. Plugins are loaded at startup (their routes must be registered before
the static catch-all), so every change here takes effect at the next restart:
the listing says so (`restart_needed`) and POST /api/system/restart does it —
a separate, explicit step, because it cuts the answers being generated and the
moment for that is the user's to choose.
"""
import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config, plugin_installer
from app.plugin_installer import JOB, InstallError
from app.plugins import is_enabled_name
from app.routers.system import restart_status

router = APIRouter()

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class InstallReq(BaseModel):
    """A catalog entry (`id`), or any GitHub repository (`repo` + optional
    `ref`/`subdir`) — the second is how a plugin that is not in the catalog is
    installed, and the UI asks twice before sending it."""
    id: str = ""
    repo: str = ""
    ref: str = ""
    subdir: str = ""


def require_protected() -> None:
    """Installing a plugin is running someone's code with every secret this
    server holds. That is the feature — but it must not be reachable by whoever
    can reach the port: off loopback, an API key is required."""
    host = os.environ.get("MYAGENT_HOST", "127.0.0.1")
    if host not in _LOOPBACK and not config.get_api_key():
        raise HTTPException(
            403, "Set an API key (Settings → API key) before installing: this "
                 "server is reachable from the network without one")


def _fail(e: InstallError, status: int = 400) -> HTTPException:
    return HTTPException(status, str(e))


@router.get("")
async def list_plugins(request: Request):
    registry = getattr(request.app.state, "plugins", {})
    plugins = []
    for p in registry.values():
        directory = config.PLUGINS_DIR / p.id
        plugins.append({**p.info(), **plugin_installer.installed_info(directory),
                        # Uninstalled, but still loaded until the restart.
                        "removed": not directory.is_dir()})
    # Installed since this process started: on disk, not loaded yet. Listed
    # apart so `plugins` keeps meaning "what this server is running".
    pending = []
    if config.PLUGINS_DIR.is_dir():
        for directory in sorted(config.PLUGINS_DIR.iterdir()):
            if (directory.name not in registry and is_enabled_name(directory.name)
                    and (directory / "plugin.py").is_file()):
                pending.append({"id": directory.name,
                                **plugin_installer.installed_info(directory)})
    return {
        "plugins": plugins,
        "pending": pending,
        "job": JOB.status(),
        "restart_needed": bool(getattr(request.app.state, "plugins_dirty", False)),
        **restart_status(request),
    }


@router.get("/catalog")
async def get_catalog():
    """Installable plugins. Fetches the index from GitHub (bundled copy when
    offline), so it is asked for on demand, never on page load."""
    return await asyncio.to_thread(
        plugin_installer.catalog, "plugin", config.PLUGINS_DIR)


@router.post("/install", status_code=202)
async def install_plugin(req: InstallReq, request: Request):
    """Start an install and return at once; GET /api/plugins reports the job."""
    require_protected()
    try:
        if req.repo:
            source = plugin_installer.github_source(req.repo, req.ref, req.subdir)
            expect, target = "", source["repo"]
        else:
            source = await asyncio.to_thread(
                plugin_installer.find_entry, "plugin", req.id)
            expect = target = source["id"]
    except InstallError as e:
        raise _fail(e)
    state = request.app.state

    def work(say):
        plugin_installer.install(source, config.PLUGINS_DIR, expect_id=expect, say=say)
        state.plugins_dirty = True
        say("Restart MyAgent to activate it.")

    try:
        JOB.start(target, work)
    except InstallError as e:
        raise _fail(e, 409)
    return {"started": True}


@router.delete("/{plugin_id}")
async def uninstall_plugin(plugin_id: str, request: Request):
    """Remove the plugin's code. Its state (~/myagent/<plugin>/) stays."""
    require_protected()
    try:
        plugin_installer.uninstall(config.PLUGINS_DIR, plugin_id)
    except InstallError as e:
        raise _fail(e, 404)
    request.app.state.plugins_dirty = True
    return {"removed": True}


@router.post("/{plugin_id}/rollback")
async def rollback_plugin(plugin_id: str, request: Request):
    """Put back the version the last install replaced."""
    require_protected()
    try:
        plugin_installer.rollback(config.PLUGINS_DIR, plugin_id)
    except InstallError as e:
        raise _fail(e, 404)
    request.app.state.plugins_dirty = True
    return {"rolled_back": True}
