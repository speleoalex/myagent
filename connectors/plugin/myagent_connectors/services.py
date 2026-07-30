"""The plugin's services, and the one way routers reach them.

Everything the plugin owns hangs off a single namespaced key,
``app.state.connectors``. ``app.state`` is shared with the core and with any
other plugin, so claiming generic names there (``bindings``, ``contacts``,
``manager``) would be a collision waiting for the second plugin.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.storage.store import JsonStore

from myagent_connectors.channels.manager import ConnectorManager
from myagent_connectors.core import CoreClient
from myagent_connectors.storage import GrantStore

STATE_KEY = "connectors"


@dataclass
class Connectors:
    bindings: JsonStore
    contacts: JsonStore
    grants: GrantStore
    core: CoreClient
    manager: ConnectorManager


def services(request: Request) -> Connectors:
    """The plugin's services for this request.

    The 503 is a safety net, not the normal "plugin not installed" path: when
    the plugin isn't loaded these routes don't exist at all and FastAPI answers
    404. This only fires if register() somehow half-completed.
    """
    state = getattr(request.app.state, STATE_KEY, None)
    if state is None:
        raise HTTPException(503, "Connectors plugin is not available")
    return state
