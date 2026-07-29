"""Read-only proxy to the connectors server.

MyAgent stores no bindings of its own: they live on the connectors server, which
listens on loopback (127.0.0.1:8899 by default) and sets no CORS headers. So the
browser cannot ask it directly — this router is the one hop that lets the UI
offer a real picker for `autonomous.notify_binding_id` instead of asking the user
to type an id they have no way to look up.

It mirrors an existing, opposite-direction precedent: the connectors server
proxies MyAgent's agent list (`GET /api/bindings/agents`) for exactly the same
reason — to populate a dropdown in its own admin UI.

Deliberately read-only and deliberately narrow: only the fields a picker needs
are forwarded. The connectors list endpoint already masks `token`/`password`, but
an allowlist beats trusting that, since this response crosses into a browser.
"""

import httpx
from fastapi import APIRouter

from app import config

router = APIRouter()

# Kept small on purpose: everything here is rendered in a <select> or a
# <datalist>. `allowed_ids` is included because for a Telegram private chat the
# user id IS the chat id, which makes it the only real source of chat-id
# suggestions we have (it does NOT cover group chats — see the UI hint).
_PUBLIC_FIELDS = ("id", "name", "type", "enabled", "agent_id", "allowed_ids")


@router.get("/bindings")
async def list_bindings():
    """The connectors server's bindings, trimmed for the UI.

    Never fails the caller: an unreachable or unconfigured connectors server is
    the normal state for an install that does not use them, so it returns an
    empty list plus the reason. The form falls back to a free-text field then —
    it must stay editable even when nothing can be enumerated.
    """
    base = (config.settings.connectors_base_url or "http://localhost:8899").rstrip("/")
    headers = {}
    if config.settings.connectors_api_key:
        headers["Authorization"] = f"Bearer {config.settings.connectors_api_key}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base}/api/bindings", headers=headers)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        return {"bindings": [], "available": False, "error": f"{type(e).__name__}: {e}"}

    if not isinstance(raw, list):
        return {"bindings": [], "available": False, "error": "unexpected response shape"}

    bindings = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        item = {k: b.get(k) for k in _PUBLIC_FIELDS}
        item["state"] = (b.get("status") or {}).get("state", "")
        bindings.append(item)
    return {"bindings": bindings, "available": True, "error": ""}
