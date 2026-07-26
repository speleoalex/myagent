from fastapi import APIRouter

from app import config
from app.config import save_settings, WORKSPACE_DIR
from app.models import Settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "workspace": str(WORKSPACE_DIR)}


@router.get("/settings")
async def get_settings():
    # Read config.settings live: update_settings rebinds it, so importing the
    # `settings` name directly would return a stale, pre-save snapshot.
    return config.settings.model_dump()


@router.put("/settings")
async def update_settings(new_settings: Settings):
    save_settings(new_settings)
    config.settings = new_settings
    return new_settings.model_dump()
