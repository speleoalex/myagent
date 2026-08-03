import secrets as secrets_mod

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.config import save_settings, WORKSPACE_DIR
from app.engine import default_model
from app.models import Settings

router = APIRouter()

# A generated key is 24 random bytes as hex; a hand-typed one only has to clear
# this floor. Short enough to be typeable on a phone, long enough that the port
# it protects is not worth guessing at.
API_KEY_MIN_LEN = 12


class ApiKeyUpdate(BaseModel):
    """Either an explicit key, or generate: true to get a fresh random one."""
    key: str | None = None
    generate: bool = False


@router.get("/health")
async def health():
    return {"status": "ok", "workspace": str(WORKSPACE_DIR)}


@router.get("/ready")
async def ready(request: Request):
    """Can this install actually answer a message right now?

    Separate from /health, which is a liveness probe and must stay instant.
    This one asks the same resolver the executor uses, so it reports exactly
    what the next turn will do — and warms its cache before the first message.
    """
    stores = request.app.state.stores
    try:
        cfg, note = await default_model.resolve_default(
            stores.models, config.settings.default_model_id)
    except ValueError as e:
        return {"ready": False, "model": None, "auto": False, "detail": str(e)}
    return {"ready": True, "model": cfg.name, "auto": bool(note), "detail": note}


@router.get("/settings")
async def get_settings():
    # Read config.settings live: update_settings rebinds it, so importing the
    # `settings` name directly would return a stale, pre-save snapshot.
    return config.settings.model_dump()


@router.put("/settings")
async def update_settings(new_settings: Settings):
    save_settings(new_settings)
    config.settings = new_settings
    # The default model and the backend URLs are exactly what the fallback
    # resolver keys on: choosing one here must take effect on the next turn.
    default_model.invalidate()
    return new_settings.model_dump()


# --- API key -----------------------------------------------------------------
# The key is returned IN CLEAR, unlike every other stored secret (model API
# keys, MCP bearers, bot tokens — those are masked by routers/secrets.py). It is
# the opposite kind of secret: not a credential for a third party that the UI
# merely carries, but the credential of THIS server, which the caller has just
# presented to get here. Masking it would only stop the user from copying it to
# their phone, which is the whole reason to look at it. When no key is set there
# is nothing to leak and the API is open by definition.

@router.get("/api-key")
async def get_api_key():
    return config.api_key_status()


@router.put("/api-key")
async def set_api_key(req: ApiKeyUpdate):
    _require_editable()
    key = secrets_mod.token_hex(24) if req.generate else (req.key or "").strip()
    if not key:
        raise HTTPException(400, "Provide a key, or generate: true "
                                "(use DELETE to turn authentication off)")
    if len(key) < API_KEY_MIN_LEN:
        raise HTTPException(400, f"Key too short (minimum {API_KEY_MIN_LEN} characters)")
    if any(c.isspace() for c in key) or not key.isprintable():
        # It travels in an HTTP header and in a URL query parameter: whitespace
        # would be silently mangled by one or the other, so the key would work
        # in one client and 401 in the next.
        raise HTTPException(400, "Key must not contain spaces or control characters")
    config.set_api_key(key)
    return config.api_key_status()


@router.delete("/api-key")
async def delete_api_key():
    """Turn authentication off. The caller has the current key, so this is
    theirs to decide — but it leaves the whole API (agents, shell tools) open to
    anyone who can reach the port, hence the confirmation in the UI."""
    _require_editable()
    config.set_api_key("")
    return config.api_key_status()


def _require_editable() -> None:
    if not config.api_key_status()["editable"]:
        raise HTTPException(
            409,
            "The API key is pinned by the MYAGENT_API_KEY environment variable; "
            "unset it (e.g. in the systemd drop-in) to manage the key from here",
        )
