"""MyAgent connectors plugin — entry point.

Bridges messaging bots (Telegram today) to myagent's agents. It used to be a
standalone server on its own port talking to myagent over HTTP; it now runs
inside myagent's process, so there is one service, one port and one UI.

Contract (see docs/PLUGINS.md): register(app) wires everything up, and
startup/shutdown own the background polling tasks. Order matters inside
register(): the routers go last, so a failure while building the stores can
never leave endpoints mounted with nothing behind them.
"""
from __future__ import annotations

import logging

from app.storage.store import JsonStore

from myagent_connectors import config
from myagent_connectors.channels.manager import ConnectorManager
from myagent_connectors.core import CoreClient
from myagent_connectors.routers import bindings, contacts, inbound, status
from myagent_connectors.services import STATE_KEY, Connectors
from myagent_connectors.storage import GrantStore

log = logging.getLogger("connectors")


def register(app) -> None:
    config.ensure_dirs()
    # Logged because a binding that "does not come back" is almost always a
    # state directory resolving somewhere unexpected.
    log.info("Connectors state directory: %s", config.STATE_DIR)

    core = CoreClient(app.state)
    bindings_store = JsonStore(config.BINDINGS_DIR)
    grants_store = GrantStore(config.GRANTS_DIR)
    services = Connectors(
        bindings=bindings_store,
        contacts=JsonStore(config.CONTACTS_DIR),
        grants=grants_store,
        core=core,
        manager=ConnectorManager(bindings_store, grants_store, core),
    )
    setattr(app.state, STATE_KEY, services)

    # Mounted under /api/connectors: the whole surface — bot tokens included —
    # inherits myagent's MYAGENT_API_KEY gate (as a separate server, only the
    # outbound /send endpoint was authenticated), and a second plugin cannot
    # collide with a generic name like /api/contacts.
    app.include_router(status.router, prefix="/api/connectors", tags=["connectors"])
    app.include_router(bindings.router, prefix="/api/connectors/bindings",
                       tags=["connectors"])
    app.include_router(contacts.router, prefix="/api/connectors/contacts",
                       tags=["connectors"])
    app.include_router(inbound.router, prefix="/api/connectors/inbound",
                       tags=["connectors"])
    # Devices authenticate with their binding's own shared key, so the global
    # MYAGENT_API_KEY middleware must step aside for this prefix (the route
    # enforces its credential itself — see routers/inbound.py). getattr: the
    # set exists on any core that has the handoff mechanism; on an older core
    # the route simply stays behind the global key.
    prefixes = getattr(app.state, "self_authenticated_prefixes", None)
    if prefixes is not None:
        prefixes.add("/api/connectors/inbound/")


async def startup(app) -> None:
    # Polling tasks must be created inside the running event loop, which is why
    # this is a lifespan hook and not part of register().
    await getattr(app.state, STATE_KEY).manager.start_all()


async def shutdown(app) -> None:
    await getattr(app.state, STATE_KEY).manager.stop_all()
