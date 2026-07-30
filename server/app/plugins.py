"""Optional plugin loader.

A plugin extends the running server: it can mount API routers, put its own
services on ``app.state`` and run background tasks. It lives OUTSIDE the
install dir (``~/myagent/plugins/<id>/``, see ``config.PLUGINS_DIR``) so a
redeploy never touches it, and it is discovered at startup — nothing in the
core names a specific plugin.

The contract is deliberately minimal (see docs/PLUGINS.md):

    ~/myagent/plugins/<id>/plugin.py
        def register(app) -> None            # required
        async def startup(app) -> None       # optional
        async def shutdown(app) -> None      # optional

``register`` gets the FastAPI app, which is already the dependency-injection
channel of every router in this codebase (``request.app.state.X``), so there
is no separate context object to keep in sync.

Two invariants hold this together:

- **A broken plugin must never stop the server from starting.** Losing an
  optional service is bad; an agent runtime that refuses to boot is worse. Every
  step is guarded and downgraded to a warning, and the failure is remembered so
  the UI can show it instead of silently pretending the plugin is not installed.
- **``startup``/``shutdown`` are the only lifecycle hooks.** A plugin must not
  use ``app.router.on_startup.append(...)``: this app passes a custom
  ``lifespan=`` to FastAPI, which replaces Starlette's default handler, so those
  callbacks would never run — and never report that they didn't.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from types import ModuleType

from app.config import PLUGINS_DIR

log = logging.getLogger("myagent.plugins")

# Module name prefix under which plugin entry points are registered in
# sys.modules, so a plugin called "chat" cannot shadow anything real.
_MODULE_PREFIX = "myagent_plugin_"


@dataclass
class Plugin:
    """One discovered plugin. ``module`` is None when loading failed, in which
    case ``error`` says why — the UI shows an installed-but-broken plugin
    rather than hiding it."""

    id: str
    module: ModuleType | None = None
    error: str = ""

    @property
    def loaded(self) -> bool:
        return self.module is not None

    def info(self) -> dict:
        return {"id": self.id, "loaded": self.loaded, "error": self.error}


def _enabled(name: str) -> bool:
    """Directory names that are skipped on purpose. The ``.disabled`` suffix is
    the documented way to park a plugin without deleting it (the rollback of an
    install renames the directory), so it has to actually disable it."""
    return not (name.startswith((".", "_")) or name.endswith(".disabled"))


def _load_one(app, directory) -> Plugin:
    plugin = Plugin(id=directory.name)
    entry = directory / "plugin.py"
    try:
        # The plugin's own package lives beside plugin.py, so its directory has
        # to be importable. Packages inside a plugin must use a distinctive
        # prefix (myagent_*): this path goes in FRONT of site-packages and a
        # generic name would shadow a real dependency.
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        spec = importlib.util.spec_from_file_location(
            f"{_MODULE_PREFIX}{directory.name}", entry
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {entry}")
        module = importlib.util.module_from_spec(spec)
        # Registered before exec_module: the entry point may import itself
        # (dataclasses, typing.get_type_hints) while it is still executing.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise AttributeError("plugin.py does not define register(app)")
        register(app)
    except Exception as e:
        plugin.error = f"{type(e).__name__}: {e}"
        log.warning("Plugin '%s' not loaded: %s", plugin.id, plugin.error)
        log.debug("Plugin '%s' traceback", plugin.id, exc_info=True)
        return plugin
    plugin.module = module
    return plugin


def load_plugins(app) -> dict[str, Plugin]:
    """Discover and register every plugin in PLUGINS_DIR.

    MUST be called after the core routers are mounted but BEFORE the static
    catch-all mount: Starlette matches routes in registration order, so a route
    added after ``app.mount("/")`` is unreachable — and the symptom is a 404
    indistinguishable from "the plugin is not installed".
    """
    found: dict[str, Plugin] = {}
    if not PLUGINS_DIR.is_dir():
        return found
    for directory in sorted(PLUGINS_DIR.iterdir()):
        if not directory.is_dir() or not _enabled(directory.name):
            continue
        if not (directory / "plugin.py").is_file():
            log.warning("Plugin '%s' has no plugin.py — skipped", directory.name)
            continue
        found[directory.name] = _load_one(app, directory)
    return found


async def start_plugins(app) -> None:
    """Call each loaded plugin's optional startup hook, from the lifespan.

    Background tasks belong here and not in register(): they need the running
    event loop.
    """
    for plugin in getattr(app.state, "plugins", {}).values():
        hook = getattr(plugin.module, "startup", None) if plugin.loaded else None
        if hook is None:
            continue
        try:
            await hook(app)
        except Exception as e:
            log.warning("Plugin '%s' failed to start: %s", plugin.id, e)
            log.debug("Plugin '%s' startup traceback", plugin.id, exc_info=True)


async def stop_plugins(app) -> None:
    """Call each loaded plugin's optional shutdown hook, in reverse order."""
    for plugin in reversed(list(getattr(app.state, "plugins", {}).values())):
        hook = getattr(plugin.module, "shutdown", None) if plugin.loaded else None
        if hook is None:
            continue
        try:
            await hook(app)
        except Exception as e:
            log.warning("Plugin '%s' did not stop cleanly: %s", plugin.id, e)
