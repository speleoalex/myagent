import json
import re
import shutil
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config

router = APIRouter()

# Tool ids become directory names under tools/: reject anything that could
# escape it (path traversal via '..', separators, etc.).
_VALID_TOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_tool_dir(request: Request, tool_id: str) -> Path:
    if not _VALID_TOOL_ID.match(tool_id or "") or ".." in tool_id:
        raise HTTPException(400, "Invalid tool ID")
    return _tools_dir(request) / tool_id


def _tools_dir(request: Request) -> Path:
    return request.app.state.tool_registry.tools_dir


def _registry(request: Request):
    return request.app.state.tool_registry


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _tool_differs(native: Path, user: Path) -> bool:
    """True if the installed tool differs from its native original.

    Compares the tool.json metadata (parsed, so formatting/key order is ignored)
    and the ``run`` script bytes. Vendored subfolders (node_modules, venvs) are
    intentionally not compared: this flag answers "did the user edit the tool?",
    which lives in tool.json + run.
    """
    if _load_json(native / "tool.json") != _load_json(user / "tool.json"):
        return True
    nr, ur = native / "run", user / "run"
    if nr.exists() != ur.exists():
        return True
    if nr.exists():
        try:
            return nr.read_bytes() != ur.read_bytes()
        except OSError:
            return True
    return False


class ToolCreateRequest(BaseModel):
    id: str
    name: str
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    script: str = "#!/bin/bash\necho 'Hello from tool'\n"
    timeout: int = 30
    max_output: int = 10000


class ToolUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: dict | None = None
    script: str | None = None
    timeout: int | None = None
    max_output: int | None = None
    enabled: bool | None = None


class ToolImportRequest(BaseModel):
    # False = one-time import (fails if already installed).
    # True  = re-import / reset to original, discarding local edits.
    overwrite: bool = False


@router.get("")
async def list_tools(request: Request):
    return _registry(request).get_all_definitions()


# --- Native tool catalog -------------------------------------------------
# The bundled tools (config.DEFAULT_TOOLS_DIR) are the "native" originals. On
# first run they seed the user's tools dir wholesale; these endpoints let the
# user import a single native tool later, or re-import it to reset local edits.
# NOTE: these routes must be declared before "/{tool_id}" so that "native" is
# not swallowed by the dynamic path segment.


@router.get("/native")
async def list_native_tools(request: Request):
    """List the native tool catalog, annotating install/modification state."""
    native = config.DEFAULT_TOOLS_DIR
    user_dir = _tools_dir(request)
    out = []
    if not native.is_dir():
        return out
    for entry in sorted(native.iterdir()):
        if not entry.is_dir():
            continue
        meta = _load_json(entry / "tool.json")
        if meta is None:
            continue
        tid = entry.name
        installed_dir = user_dir / tid
        installed = installed_dir.is_dir() and (installed_dir / "tool.json").exists()
        out.append({
            "id": tid,
            "name": meta.get("name", tid),
            "description": meta.get("description", ""),
            "internal": bool(meta.get("internal")),
            "installed": installed,
            "modified": bool(installed and _tool_differs(entry, installed_dir)),
        })
    return out


@router.post("/native/{tool_id}/import")
async def import_native_tool(tool_id: str, req: ToolImportRequest, request: Request):
    """Copy a native tool into the user's tools dir.

    ``overwrite=false`` (default) imports it and fails if it already exists.
    ``overwrite=true`` re-imports it, restoring the original and discarding any
    local edits.
    """
    if not _VALID_TOOL_ID.match(tool_id or "") or ".." in tool_id:
        raise HTTPException(400, "Invalid tool ID")

    src = config.DEFAULT_TOOLS_DIR / tool_id
    if not (src.is_dir() and (src / "tool.json").exists()):
        raise HTTPException(404, f"Native tool not found: {tool_id}")

    dst = _safe_tool_dir(request, tool_id)
    if src.resolve() == dst.resolve():
        # Would happen if MYAGENT_TOOLS points at the bundled catalog itself;
        # rmtree+copytree onto its own source would destroy the original.
        raise HTTPException(400, "Native catalog and tools dir are the same")

    if dst.exists():
        if not req.overwrite:
            raise HTTPException(409, f"Tool already installed: {tool_id}")
        shutil.rmtree(dst)

    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    run_path = dst / "run"
    if run_path.exists():
        run_path.chmod(
            run_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

    meta = _load_json(dst / "tool.json") or {}
    meta["id"] = tool_id
    return meta


@router.get("/{tool_id}")
async def get_tool(tool_id: str, request: Request):
    d = _registry(request).get_definition(tool_id)
    if d is None:
        raise HTTPException(404, f"Tool not found: {tool_id}")
    return d


@router.get("/{tool_id}/source")
async def get_tool_source(tool_id: str, request: Request):
    run_path = _safe_tool_dir(request, tool_id) / "run"
    if not run_path.exists():
        raise HTTPException(404, f"No run script for tool: {tool_id}")
    return {"script": run_path.read_text(errors="replace")}


@router.post("", status_code=201)
async def create_tool(req: ToolCreateRequest, request: Request):
    tool_dir = _safe_tool_dir(request, req.id)

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
    (tool_dir / "tool.json").write_text(json.dumps(meta, indent=2))

    run_path = tool_dir / "run"
    run_path.write_text(req.script)
    run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    meta["id"] = req.id
    return meta


@router.put("/{tool_id}")
async def update_tool(tool_id: str, req: ToolUpdateRequest, request: Request):
    tool_dir = _safe_tool_dir(request, tool_id)
    tool_json_path = tool_dir / "tool.json"

    if not tool_json_path.exists():
        raise HTTPException(404, f"Tool not found: {tool_id}")

    with open(tool_json_path) as f:
        meta = json.load(f)

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

    tool_json_path.write_text(json.dumps(meta, indent=2))

    if req.script is not None:
        run_path = tool_dir / "run"
        run_path.write_text(req.script)
        run_path.chmod(run_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    meta["id"] = tool_id
    return meta


@router.delete("/{tool_id}")
async def delete_tool(tool_id: str, request: Request):
    tool_dir = _safe_tool_dir(request, tool_id)

    if not tool_dir.exists():
        raise HTTPException(404, f"Tool not found: {tool_id}")

    tool_json = tool_dir / "tool.json"
    if tool_json.exists():
        with open(tool_json) as f:
            meta = json.load(f)
        if meta.get("internal"):
            raise HTTPException(400, "Cannot delete internal tools")

    shutil.rmtree(tool_dir)
    return {"ok": True}
