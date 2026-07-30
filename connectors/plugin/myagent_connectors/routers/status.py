"""Plugin-level state: how many bots are up, and the kill switch.

Separate from a binding's own status because it answers a different question:
not "is this bot healthy" but "is this plugin doing anything at all".
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from myagent_connectors.channels.registry import all_channels
from myagent_connectors.services import services

router = APIRouter()


class EnabledReq(BaseModel):
    enabled: bool = True


@router.get("/status")
async def get_status(request: Request):
    """Plugin state plus every discovered channel — broken ones included, with
    their error, so "installed but not loading" is visible instead of looking
    like "not installed"."""
    return {**services(request).manager.summary(), "channels": all_channels()}


@router.post("/stop")
async def stop_all(request: Request):
    """Stop every bot and remember it across restarts. The way out is
    POST /api/connectors/start — deliberately not automatic, because this is
    what you reach for when the plugin is causing damage."""
    manager = services(request).manager
    await manager.set_enabled(False)
    return manager.summary()


@router.post("/start")
async def start_all(request: Request):
    manager = services(request).manager
    await manager.set_enabled(True)
    return manager.summary()
