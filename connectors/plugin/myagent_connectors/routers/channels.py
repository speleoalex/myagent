"""Installing channels from the Channels page.

A channel is the unit this plugin is extended by, so this is the plugin's own
use of the core installer (``app.plugin_installer``): same sources, same rules,
same single background job — only the destination (``config.USER_CHANNELS_DIR``)
and the manifest (``channel.json``, whose ``type`` is the id) differ.

Unlike a plugin, a channel mounts no routes: once installed the registry is
scanned again and the bots of that type are restarted, with no server restart.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app import plugin_installer
from app.plugin_installer import JOB, InstallError
from app.routers.plugins import InstallReq, require_protected

from myagent_connectors import config
from myagent_connectors.channels import registry
from myagent_connectors.services import Connectors, services

log = logging.getLogger("connectors.channels")
router = APIRouter()

KIND = "channel"
MANIFEST = "channel.json"


async def _reload(svc: Connectors, channel_type: str) -> None:
    """Pick the change up: rescan, then restart the bots built on that type so
    they run the new class (or stop, when it is gone)."""
    registry.discover(refresh=True)
    for data in svc.bindings.list_all():
        if data.get("type") == channel_type:
            try:
                await svc.manager.reconcile(data["id"])
            except Exception as e:  # one bad binding must not hide the others
                log.warning("binding %r not restarted: %s", data.get("id"), e)


@router.get("/catalog")
async def get_catalog():
    return await asyncio.to_thread(
        plugin_installer.catalog, KIND, config.USER_CHANNELS_DIR, MANIFEST)


@router.get("/job")
async def get_job():
    return JOB.status()


@router.post("/install", status_code=202)
async def install_channel(req: InstallReq, request: Request):
    require_protected()
    try:
        if req.repo:
            source = plugin_installer.github_source(req.repo, req.ref, req.subdir)
            expect, target = "", source["repo"]
        else:
            source = await asyncio.to_thread(plugin_installer.find_entry, KIND, req.id)
            expect = target = source["id"]
    except InstallError as e:
        raise HTTPException(400, str(e))
    svc = services(request)
    loop = asyncio.get_running_loop()

    def work(say):
        info = plugin_installer.install(
            source, config.USER_CHANNELS_DIR, manifest_name=MANIFEST,
            id_key="type", required=(), expect_id=expect, say=say)
        # The job runs in a thread; the registry and the bots belong to the loop.
        asyncio.run_coroutine_threadsafe(_reload(svc, info["id"]), loop).result()
        channel = registry.get_channel(info["id"])
        if channel is None or not channel.loaded:
            raise InstallError(
                "installed, but the channel does not load: "
                + (channel.error if channel else "no channel.json found"))

    try:
        JOB.start(target, work)
    except InstallError as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@router.delete("/{channel_type}")
async def uninstall_channel(channel_type: str, request: Request):
    """Remove a channel the user installed. A bundled one of the same type, if
    any, takes over; otherwise the bots using it would be left with no code,
    so that is refused until they are deleted."""
    require_protected()
    svc = services(request)
    bundled = (registry.CHANNELS_DIR / channel_type / MANIFEST).is_file()
    in_use = [d["id"] for d in svc.bindings.list_all() if d.get("type") == channel_type]
    if in_use and not bundled:
        raise HTTPException(
            409, f"channel '{channel_type}' is used by: {', '.join(in_use)}")
    try:
        plugin_installer.uninstall(config.USER_CHANNELS_DIR, channel_type)
    except InstallError as e:
        raise HTTPException(404, str(e))
    await _reload(svc, channel_type)
    return {"removed": True}
