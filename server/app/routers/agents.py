import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.models import Agent

router = APIRouter()

# Agent ids become filenames under config/agents/: same safe charset as JsonStore.
_VALID_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _store(request: Request):
    return request.app.state.stores.agents


def _native_agents_dir() -> Path:
    return config.DEFAULT_CONFIG_DIR / "agents"


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


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
    return agents


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
        meta = _load_json(f)
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
    if not _VALID_AGENT_ID.match(agent_id or "") or ".." in agent_id:
        raise HTTPException(400, "Invalid agent ID")

    data = _load_json(_native_agents_dir() / f"{agent_id}.json")
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
    data = _store(request).get(agent_id)
    if data is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    return data


@router.post("", status_code=201)
async def create_agent(agent: Agent, request: Request):
    store = _store(request)
    if store.exists(agent.id):
        raise HTTPException(409, f"Agent already exists: {agent.id}")
    store.save(agent.id, agent.model_dump())
    return agent.model_dump()


@router.put("/{agent_id}")
async def update_agent(agent_id: str, agent: Agent, request: Request):
    store = _store(request)
    if not store.exists(agent_id):
        raise HTTPException(404, f"Agent not found: {agent_id}")
    agent.id = agent_id
    store.save(agent_id, agent.model_dump())
    return agent.model_dump()


class EventRequest(BaseModel):
    """External event producer (webhooks, scripts, other services). Already
    behind the global API-key middleware when MYAGENT_API_KEY is set."""
    type: str = "message"          # message | schedule | reminder | webhook
    payload: dict = {}
    due_at: str = ""               # ISO timestamp; empty = due now
    source: str = "api"
    repeat_s: int = 0              # > 0 = recurring


@router.post("/{agent_id}/events", status_code=201)
async def queue_agent_event(agent_id: str, req: EventRequest, request: Request):
    """Queue an event for an agent. A live agent is woken right away; a
    non-live agent accumulates events until it is started."""
    if _store(request).get(agent_id) is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    try:
        event = request.app.state.events.append(
            agent_id, type=req.type, payload=req.payload,
            due_at=req.due_at, source=req.source or "api",
            repeat_s=req.repeat_s,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    return {"id": event["id"], "due_at": event["due_at"]}


@router.get("/{agent_id}/events")
async def list_agent_events(agent_id: str, request: Request):
    """Pending events plus the recent audit trail (reacted + reaction)."""
    if _store(request).get(agent_id) is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    events = request.app.state.events
    return {"pending": events.pending(agent_id, now="9999"),
            "archive": events.archive(agent_id)}


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    store = _store(request)
    if not store.delete(agent_id):
        raise HTTPException(404, f"Agent not found: {agent_id}")
    # Autonomy state (event queues, scheduler state) is operational data of
    # the deleted agent: remove it. Deep memory is deliberately KEPT — an
    # agent recreated with the same id finds its memory again.
    autonomy = getattr(request.app.state, "autonomy", None)
    if autonomy is not None:
        await autonomy.stop(agent_id)
        autonomy.drop_agent(agent_id)
    agent_dir = config.AUTONOMY_DIR / agent_id
    if agent_dir.is_dir():
        shutil.rmtree(agent_dir, ignore_errors=True)
    return {"ok": True}
