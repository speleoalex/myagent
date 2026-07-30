import json
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.ids import is_valid_id
from app.storage.sessions import write_json

router = APIRouter()


def _check_tool_id(tool_id: str) -> None:
    # Tool ids become directory names under tools/ (group names too): reject
    # anything that could escape it — same charset as every other entity id.
    if not is_valid_id(tool_id):
        raise HTTPException(400, "Invalid tool ID")


def _tools_dir(request: Request) -> Path:
    return request.app.state.tool_registry.tools_dir


def _registry(request: Request):
    return request.app.state.tool_registry


def _annotate(registry, meta: dict) -> dict:
    """Overlay state for the UI, on a COPY (the registry caches its metas):
    ``origin`` ("native" = shipped with the app, "custom" = user-only) is set
    by the scan; ``has_override`` = a user copy shadows the bundled original
    (reset applies); ``modified`` = that copy actually differs."""
    tid = meta["id"]
    return {
        **meta,
        "has_override": registry.native_dir(tid) is not None
        and registry.user_dir(tid) is not None,
        "modified": registry.is_modified(tid),
    }


class ToolCreateRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    script: str = "#!/bin/bash\necho 'Hello from tool'\n"
    timeout: int = 30
    max_output: int = 10000
    # Optional group folder: the tool is created in tools/<category>/<id> and
    # becomes grantable via the <category>/* wildcard.
    category: str | None = None


class ToolUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: dict | None = None
    script: str | None = None
    timeout: int | None = None
    max_output: int | None = None
    enabled: bool | None = None


@router.get("")
async def list_tools(request: Request):
    """Every tool an agent can be given: the bundled catalog overlaid by the
    user dir (no install step — native tools are always present), plus the
    discovered MCP catalogue (each entry marked ``source: "mcp"`` and
    ``available``).

    MCP entries are read-only here — they have no folder, so the create/update/
    delete/reset routes below stay filesystem-only. Including the ones that are
    only known from cache (``available: false``) matters: the agent form rebuilds
    its tool list from what it renders, so omitting a temporarily-down server's
    tools would silently drop them from every agent that uses them.
    """
    registry = _registry(request)
    return [
        m if m.get("source") == "mcp" else _annotate(registry, m)
        for m in registry.get_all_definitions(include_mcp=True)
    ]


@router.get("/{tool_id}")
async def get_tool(tool_id: str, request: Request):
    registry = _registry(request)
    d = registry.get_definition(tool_id)
    if d is None:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    return d if d.get("mcp") else _annotate(registry, d)


@router.get("/{tool_id}/source")
async def get_tool_source(tool_id: str, request: Request):
    tool_dir = _registry(request).tool_dir(tool_id)
    if tool_dir is None or not (tool_dir / "run").exists():
        raise HTTPException(404, f"No run script for tool: {tool_id}")
    return {"script": (tool_dir / "run").read_text(errors="replace")}


def _write_run(tool_dir: Path, script: str) -> None:
    run_path = tool_dir / "run"
    run_path.write_text(script)
    run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@router.post("", status_code=201)
async def create_tool(req: ToolCreateRequest, request: Request):
    _check_tool_id(req.id)
    category = (req.category or "").strip() or None
    if category is not None:
        _check_tool_id(category)  # same charset: it becomes a directory name
        # A group cannot live under a tool folder — in either layer: a user
        # group named like a bundled tool would collide the id namespaces.
        if (_tools_dir(request) / category / "tool.json").exists() or (
            config.DEFAULT_TOOLS_DIR / category / "tool.json"
        ).exists():
            raise HTTPException(400, f"'{category}' is a tool, not a group")

    # Ids are global (they are the function names the model calls) across both
    # layers: colliding with a native tool is just as much a conflict — edit
    # that tool instead of shadowing it with an unrelated one.
    if _registry(request).tool_dir(req.id) is not None:
        raise HTTPException(409, f"Tool already exists: {req.id}")
    tool_dir = _tools_dir(request) / category / req.id if category \
        else _tools_dir(request) / req.id
    if tool_dir.exists():
        raise HTTPException(409, f"Tool already exists: {req.id}")

    tool_dir.mkdir(parents=True)

    meta = {
        "name": req.name,
        "description": req.description,
        "parameters": req.parameters,
        "timeout": req.timeout,
        "max_output": req.max_output,
    }
    write_json(tool_dir / "tool.json", meta)  # atomic, like every other store
    _write_run(tool_dir, req.script)
    _registry(request).mark_dirty()

    meta["id"] = req.id
    if category:
        meta["category"] = category
    return meta


@router.put("/{tool_id}")
async def update_tool(tool_id: str, req: ToolUpdateRequest, request: Request):
    _check_tool_id(tool_id)
    registry = _registry(request)
    current = registry.get_definition(tool_id)
    if current is None or current.get("mcp"):
        raise HTTPException(404, f"Tool not found: {tool_id}")
    if current.get("internal"):
        raise HTTPException(400, "Internal tools cannot be edited")

    # Copy-on-write: a native tool served from the bundle gets a user copy
    # first, and the edit lands there. The bundled original is never touched —
    # deleting the copy (reset) restores it.
    tool_dir = registry.ensure_override(tool_id)
    if tool_dir is None:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    tool_json_path = tool_dir / "tool.json"

    # Tolerant read: a corrupt tool.json must be repairable through this same
    # endpoint (a 500 here would make the tool uneditable forever).
    try:
        with open(tool_json_path) as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        meta = {}

    if req.name is not None:
        meta["name"] = req.name
    if req.description is not None:
        meta["description"] = req.description
    if req.parameters is not None:
        meta["parameters"] = req.parameters
    if req.timeout is not None:
        meta["timeout"] = req.timeout
    if req.max_output is not None:
        meta["max_output"] = req.max_output
    if req.enabled is not None:
        meta["enabled"] = req.enabled

    write_json(tool_json_path, meta)  # atomic

    if req.script is not None:
        _write_run(tool_dir, req.script)
    registry.mark_dirty()

    meta["id"] = tool_id
    if current.get("category"):
        meta["category"] = current["category"]
    return meta


def _remove_user_copy(request: Request, path: Path) -> None:
    """Delete a tool folder in the user dir, sweeping the group folder if it
    ends up empty (an empty group would linger as a phantom category)."""
    shutil.rmtree(path)
    parent = path.parent
    if parent != _tools_dir(request):
        try:
            parent.rmdir()
        except OSError:
            pass
    _registry(request).mark_dirty()


@router.post("/{tool_id}/reset")
async def reset_tool(tool_id: str, request: Request):
    """Discard the user's copy of a native tool: the bundled original shows
    through again. This is the whole 'reset to original' story — no re-import."""
    _check_tool_id(tool_id)
    registry = _registry(request)
    if registry.native_dir(tool_id) is None:
        if registry.user_dir(tool_id) is not None:
            raise HTTPException(400, "Not a native tool — delete it instead")
        raise HTTPException(404, f"Tool not found: {tool_id}")
    user_copy = registry.user_dir(tool_id)
    if user_copy is None:
        raise HTTPException(400, "Nothing to reset: no local copy")
    _remove_user_copy(request, user_copy)
    d = registry.get_definition(tool_id)
    return _annotate(registry, d) if d else {"ok": True}


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str, request: Request):
    """Delete a user-created tool. Native tools cannot be deleted — they are
    always present in the bundled catalog; reset discards local changes and a
    disabled tool stays hidden from agents."""
    _check_tool_id(tool_id)
    registry = _registry(request)
    if registry.native_dir(tool_id) is not None:
        raise HTTPException(
            400, "Native tools cannot be deleted; use reset to discard local "
                 "changes, or disable the tool")
    user_copy = registry.user_dir(tool_id)
    if user_copy is None:
        raise HTTPException(404, f"Tool not found: {tool_id}")

    meta = registry.get_definition(tool_id) or {}
    if meta.get("internal"):
        raise HTTPException(400, "Cannot delete internal tools")

    _remove_user_copy(request, user_copy)
    return {"ok": True}
