"""What the frontend needs to know about optional plugins.

Read-only and never fails: an install with no plugins is the normal case, so
this answers with an empty list rather than an error. The UI uses it to decide
whether to show a plugin's menu entry — a plugin that is installed but failed
to load is reported with its error, not hidden.

A plugin's own routes simply do not exist when it isn't loaded, so they 404.
The core does not shim them: knowing which prefixes to shim would mean naming
a specific plugin here.
"""
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("")
async def list_plugins(request: Request):
    registry = getattr(request.app.state, "plugins", {})
    return {"plugins": [p.info() for p in registry.values()]}
