import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import agent_package, config
from app.ids import is_valid_id
from app.models import Agent
from app.engine.agent_router import AUTO as RESERVED_AGENT_ID
from app.engine.executor import resolve_model_for_agent
from app.engine.llm_provider import LLMProvider
from app.tools.registry import ToolRegistry
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


async def _with_status(data: dict) -> dict:
    """``_public`` plus ``folder_missing``: whether the agent's working folder
    is unreachable right now. Read-only state, never stored (the PUT body
    ignores it): a folder on a disk that is down for a minute is not a
    configuration error, but it must not stay invisible until a tool fails —
    observed on the Orin, an agent pointed at an unmounted NAS for days."""
    out = _public(data)
    try:
        agent = Agent(**data)
    except Exception:
        return out
    out["folder_missing"] = bool(agent.folder) and await agent.folder.missing()
    return out


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
    # Concurrently: each check may wait out its timeout on a dead mount.
    return list(await asyncio.gather(*(_with_status(a) for a in agents)))


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


@router.post("/import")
async def import_agent(request: Request, overwrite: bool = False):
    """Install an agent package (app/agent_package.py) sent as the raw request
    body. A raw body, not a multipart form, so no extra dependency is needed.
    409 lists what already exists; ``overwrite=true`` replaces it."""
    blob = await request.body()
    if len(blob) > agent_package.MAX_PACKAGE_BYTES:
        raise HTTPException(413, "the package is too large")
    state = request.app.state
    try:
        return agent_package.import_agent(blob, state.stores, state.tasks,
                                          state.tool_registry, overwrite=overwrite)
    except agent_package.Conflict as e:
        # Structured, so a client can name the items in its own language.
        raise HTTPException(409, {"message": str(e), "conflicts": e.items})
    except agent_package.PackageError as e:
        raise HTTPException(400, str(e))


class ContextPreviewRequest(BaseModel):
    model_id: str = ""
    tools: list[str] = []


@router.post("/context-preview")
async def context_preview(req: ContextPreviewRequest, request: Request):
    """How much room the model leaves to the messages (``fit``) with these
    tools, for the form's share sentences. Measured the way a turn measures
    it (resolve the model, schemas in OpenAI format, context_state), so the
    numbers shown are the ones the executor enforces."""
    stores = request.app.state.stores
    draft = Agent(id="preview", name="preview", model_id=req.model_id, tools=req.tools)
    cfg, _ = await resolve_model_for_agent(draft, stores)
    defs = request.app.state.tool_registry.get_definitions_for_agent(req.tools)
    provider = LLMProvider(cfg)
    try:
        state = await provider.context_state(
            [], ToolRegistry.to_openai_format(defs) if defs else None)
    finally:
        await provider.close()
    return {"model_name": cfg.name or cfg.id, "window": state["window"],
            "fit": state["fit"]}


@router.get("/{agent_id}/export")
async def export_agent(agent_id: str, request: Request):
    """The agent as one .tar.gz: definition, tasks, user tools, tool settings
    without secrets. See app/agent_package.py for what is left out and why."""
    state = request.app.state
    try:
        blob = agent_package.export_agent(agent_id, state.stores, state.tasks,
                                          state.tool_registry)
    except KeyError:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    return Response(blob, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="myagent-{agent_id}.tar.gz"'})


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    return await _with_status(get_or_404(_store(request), agent_id, "Agent"))


def _check_timezone(agent: Agent) -> None:
    """Refuse a working zone this machine cannot resolve, on the way IN.

    Not a model validator: a bad name hand-written into an agent's .json must
    still load (config.now_in_timezone degrades to the host zone and logs), or
    one typo would take the agent off the air. But accepting one through the
    API would inject a confidently wrong date into every prompt, which is the
    failure the date block exists to prevent."""
    tz = (agent.timezone or "").strip()
    if not config.is_valid_timezone(tz):
        raise HTTPException(
            400, f"Unknown timezone {tz!r}. Use an IANA name such as "
                 "'Europe/Rome', or leave it empty to follow the app settings.")
    agent.timezone = tz


def _check_budgets(agent: Agent, previous: dict | None = None) -> None:
    """Refuse a tool budget the round budget can never spend.

    The two limits measure different things — rounds are model calls, tool calls
    are actions — but a round usually carries ONE call, so N iterations reach
    about N actions. With `max_iterations` below `max_tool_calls` the turn stops
    at the smaller number and part of the tool budget is unreachable, silently.
    Measured on a production install, 2026-11-27: an agent at 10 / 20 spent ten
    rounds on reading, ended with the file it had composed unwritten, and could
    not tell which limit had stopped it.

    Not a model validator, for the reason _check_timezone is not one: an agent
    file already on disk with those numbers must keep loading. But a NEW one is
    refused, and so is an EDIT OF THE BUDGETS — `previous` is the stored agent,
    and an untouched pair passes, so opening an old agent and changing its
    prompt never fails on numbers the user did not touch.

    No tools means no tool calls: there the iteration budget IS the budget
    (a conversation agent), and the rule does not apply."""
    if not agent.tools:
        return
    if agent.max_tool_calls <= agent.max_iterations:
        return
    if previous is not None:
        same_rounds = previous.get("max_iterations") == agent.max_iterations
        same_calls = previous.get("max_tool_calls") == agent.max_tool_calls
        if same_rounds and same_calls:
            return
    raise HTTPException(
        400,
        f"max_tool_calls ({agent.max_tool_calls}) is larger than max_iterations "
        f"({agent.max_iterations}). A round usually carries one tool call, so "
        f"this turn would stop after about {agent.max_iterations} actions and "
        f"part of the tool budget could never be spent — it would end with work "
        f"still to do. Raise max_iterations to at least {agent.max_tool_calls}, "
        f"or lower max_tool_calls if a tighter limit is what you want.")


@router.post("", status_code=201)
async def create_agent(agent: Agent, request: Request):
    store = _store(request)
    if agent.id == RESERVED_AGENT_ID:
        # The chat's routing sentinel: a real agent with this id would switch
        # Auto off silently (literal beats sentinel in agent_router).
        raise HTTPException(400, f"'{RESERVED_AGENT_ID}' is reserved for automatic agent selection")
    require_absent(store, agent.id, "Agent")
    _check_timezone(agent)
    _check_budgets(agent)
    store.save(agent.id, agent.model_dump())
    return agent.model_dump()


@router.put("/{agent_id}")
async def update_agent(agent_id: str, agent: Agent, request: Request):
    store = _store(request)
    require_exists(store, agent_id, "Agent")
    previous = store.get(agent_id)
    agent.id = agent_id
    _check_timezone(agent)
    _check_budgets(agent, previous)
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
