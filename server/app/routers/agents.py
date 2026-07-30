from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.ids import is_valid_id
from app.models import Agent
from app.routers.crud import get_or_404, require_absent, require_exists
from app.storage.sessions import read_json

router = APIRouter()


def _store(request: Request):
    return request.app.state.stores.agents


def _public(data: dict) -> dict:
    """Stored agent as the frontend should see it: normalized through the
    model, so omitted fields surface with their real server-side defaults
    (the UI must never have to re-hardcode them). Malformed data is handed
    back as-is rather than 500."""
    try:
        return Agent(**data).model_dump()
    except Exception:
        return data


def _native_agents_dir() -> Path:
    return config.DEFAULT_CONFIG_DIR / "agents"


def _agent_fingerprint(data: dict) -> dict:
    """Normalize an agent dict through the Agent model for comparison, so that
    omitted defaults and key ordering don't register as spurious differences.
    Falls back to the raw dict if the data doesn't validate.

    ``live`` is deliberately excluded: it is the runtime on/off switch (stored
    in the agent file only so a started agent survives a restart), not part of
    the definition. Starting or stopping an agent must not flag it as locally
    modified — and it is `import_native_agent` that keeps the two consistent by
    preserving the flag across a reset."""
    try:
        out = Agent(**data).model_dump()
    except Exception:
        out = dict(data)
    out.pop("live", None)
    return out


class AgentImportRequest(BaseModel):
    # False = one-time import (fails if already installed).
    # True  = re-import / reset to original, discarding local edits.
    overwrite: bool = False


@router.get("")
async def list_agents(request: Request, selectable: bool = False):
    """List agents. With ``selectable=true`` return only the agents a *user* can
    pick as a conversation entrypoint (i.e. ``enabled``) — used by the chat and
    connector pickers. The admin management list omits the param and also sees
    disabled agents.

    ``callable`` is deliberately NOT applied here: it gates agent→agent
    delegation only (see ``AgentExecutor._agent_can_call``). A task-specific
    agent may well be one the user drives directly from chat or Telegram while
    no other agent is allowed to delegate to it."""
    agents = _store(request).list_all()
    if selectable:
        agents = [a for a in agents if a.get("enabled", True)]
    return [_public(a) for a in agents]


# --- Native agent catalog ------------------------------------------------
# The bundled agents (config.DEFAULT_CONFIG_DIR/agents) are the "native"
# originals. On first run they seed the user's data dir wholesale; these
# endpoints let the user import a single native agent later, or re-import it to
# reset local edits. Declared before "/{agent_id}" so "native" isn't captured
# as a dynamic path segment.


@router.get("/native")
async def list_native_agents(request: Request):
    """List the native agent catalog, annotating install/modification state."""
    native = _native_agents_dir()
    store = _store(request)
    out = []
    if not native.is_dir():
        return out
    for f in sorted(native.glob("*.json")):
        meta = read_json(f)
        if meta is None:
            continue
        aid = f.stem
        installed_data = store.get(aid)
        installed = installed_data is not None
        modified = bool(
            installed and _agent_fingerprint(installed_data) != _agent_fingerprint(meta)
        )
        out.append({
            "id": meta.get("id", aid),
            "name": meta.get("name", aid),
            "description": meta.get("description", ""),
            "model_id": meta.get("model_id", ""),
            "installed": installed,
            "modified": modified,
        })
    return out


@router.post("/native/{agent_id}/import")
async def import_native_agent(agent_id: str, req: AgentImportRequest, request: Request):
    """Copy a native agent into the user's data dir.

    ``overwrite=false`` (default) imports it and fails if it already exists.
    ``overwrite=true`` re-imports it, restoring the original and discarding any
    local edits.
    """
    if not is_valid_id(agent_id):
        raise HTTPException(400, "Invalid agent ID")

    data = read_json(_native_agents_dir() / f"{agent_id}.json")
    if data is None:
        raise HTTPException(404, f"Native agent not found: {agent_id}")

    store = _store(request)
    installed = store.get(agent_id)
    if installed is not None and not req.overwrite:
        raise HTTPException(409, f"Agent already installed: {agent_id}")

    data["id"] = agent_id
    try:
        agent = Agent(**data)
    except Exception as e:
        raise HTTPException(422, f"Native agent is malformed: {e}")
    # A reset restores the DEFINITION. Whether the agent is currently running is
    # operational state the user drives from its card, so carry it over instead
    # of silently starting or stopping the agent (see _agent_fingerprint).
    if installed is not None:
        agent.live = bool(installed.get("live", False))
    store.save(agent_id, agent.model_dump())
    return agent.model_dump()


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    return _public(get_or_404(_store(request), agent_id, "Agent"))


@router.post("", status_code=201)
async def create_agent(agent: Agent, request: Request):
    store = _store(request)
    require_absent(store, agent.id, "Agent")
    store.save(agent.id, agent.model_dump())
    return agent.model_dump()


@router.put("/{agent_id}")
async def update_agent(agent_id: str, agent: Agent, request: Request):
    store = _store(request)
    require_exists(store, agent_id, "Agent")
    agent.id = agent_id
    store.save(agent_id, agent.model_dump())
    return agent.model_dump()


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    store = _store(request)
    if not store.delete(agent_id):
        raise HTTPException(404, f"Agent not found: {agent_id}")
    # Autonomy state (scheduler state, scheduled tasks) is operational data of
    # the deleted agent: remove it — drop_agent owns the on-disk layout. Deep
    # memory is deliberately KEPT: an agent recreated with the same id finds
    # its memory again.
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None:
        await autonomy.stop(agent_id)
        autonomy.drop_agent(agent_id)
    return {"ok": True}
