"""CRUD + connection testing for MCP servers.

Secrets (``bearer``, and every value of ``env`` / ``headers``) are write-only:
GET returns a mask, and a PUT that sends the mask back keeps the stored value —
the same convention the models router uses for API keys.
"""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from app.mcp import naming
from app.models import McpServer

router = APIRouter()

# Sentinel handed to the frontend in place of a stored secret.
SECRET_MASK = "********"


def _manager(request: Request):
    return request.app.state.mcp


def _store(request: Request):
    return request.app.state.mcp_store


def _masked(data: dict) -> dict:
    out = dict(data)
    if out.get("bearer"):
        out["bearer"] = SECRET_MASK
    for field in ("env", "headers"):
        values = out.get(field)
        if isinstance(values, dict):
            out[field] = {k: (SECRET_MASK if v else v) for k, v in values.items()}
    return out


def _public(data: dict, request: Request) -> dict:
    """Stored config as the frontend should see it: normalized, masked, plus live status."""
    try:
        data = McpServer(**data).model_dump()
    except (ValidationError, TypeError):
        pass  # malformed stored config: hand it back as-is rather than 500
    out = _masked(data)
    out["status"] = _manager(request).status(data.get("id") or "")
    return out


def _unmask(new: McpServer, existing: dict) -> McpServer:
    """Resolve mask sentinels against the stored config, per key."""
    if new.bearer == SECRET_MASK:
        new.bearer = existing.get("bearer", "")
    for field in ("env", "headers"):
        stored = existing.get(field) or {}
        current = getattr(new, field) or {}
        resolved = {
            key: (stored.get(key, "") if value == SECRET_MASK else value)
            for key, value in current.items()
        }
        setattr(new, field, resolved)
    return new


# ----------------------------------------------------------------------
# Fixed routes MUST be declared before /{server_id} or they'd be captured
# as a server id (same hazard the tools router documents for /native).
# ----------------------------------------------------------------------

@router.get("")
async def list_servers(request: Request):
    return [_public(s, request) for s in _store(request).list_all()]


@router.get("/status")
async def status_all(request: Request):
    manager = _manager(request)
    return {s.id: manager.status(s.id) for s in manager.servers()}


@router.get("/tools")
async def list_mcp_tools(request: Request):
    """Tool catalogue grouped per server, for the agent tool picker.

    Read-only and side-effect free: it reports what has been discovered (live or
    from the on-disk cache) and never opens a connection.
    """
    manager = _manager(request)
    by_server: dict[str, list[dict]] = {}
    for meta in manager.catalogue(include_unavailable=True):
        by_server.setdefault(meta["mcp"]["server"], []).append(meta)
    out = []
    for cfg in manager.servers():
        out.append({
            "server": cfg.id,
            "name": cfg.name or cfg.id,
            "enabled": cfg.enabled,
            "wildcard": naming.wildcard_for(cfg.id),
            "status": manager.status(cfg.id),
            "tools": by_server.get(cfg.id, []),
        })
    return out


class TestRequest(McpServer):
    """A (possibly unsaved) server draft to probe."""


@router.post("/test")
async def test_server(draft: TestRequest, request: Request):
    """Connect, initialize, list tools, tear down. Never leaves a subprocess.

    Returns HTTP 200 with ``{"ok": false, "error": ...}`` on failure so the UI can
    render the reason in the panel instead of a toast full of JSON.
    """
    cfg = McpServer(**draft.model_dump())
    existing = _store(request).get(cfg.id)
    # Mask sentinels are resolved from the stored config ONLY when this draft
    # still points at the same target. Otherwise a caller could probe
    # {"id": "<known>", "url": "http://attacker/", "bearer": "********"} and have
    # us deliver the stored token there — the whole point of keeping secrets
    # write-only. When the target differs, masked values resolve to empty.
    if existing and _same_target(cfg, existing):
        cfg = _unmask(cfg, existing)
    else:
        cfg = _unmask(cfg, {})
    return await _manager(request).test(cfg)


def _same_target(draft: McpServer, stored: dict) -> bool:
    """Whether a draft still addresses the same endpoint as the stored config.

    A PUT deliberately allows moving a server to a new host while keeping its
    token (a server that changed address is a normal case). A one-shot probe
    carries no such intent, so it must not be able to redirect a secret.
    """
    if draft.transport != (stored.get("transport") or "stdio"):
        return False
    if draft.transport == "http":
        return draft.url.rstrip("/") == (stored.get("url") or "").rstrip("/")
    return (
        draft.command == (stored.get("command") or "")
        and [str(a) for a in (draft.args or [])] == [str(a) for a in (stored.get("args") or [])]
    )


class ImportRequest(BaseModel):
    """A Claude-Desktop / VS Code style config blob."""
    config: dict = {}
    overwrite: bool = False


@router.post("/import")
async def import_servers(req: ImportRequest, request: Request):
    """Import a ``{"mcpServers": {...}}`` blob. Per-entry results, never all-or-nothing."""
    blob = req.config or {}
    entries = blob.get("mcpServers") or blob.get("servers") or blob
    if not isinstance(entries, dict) or not entries:
        raise HTTPException(400, "No server definitions found (expected an "
                                 "'mcpServers' object)")

    store = _store(request)
    created: list[dict] = []
    skipped: list[dict] = []
    # Ids taken WITHIN this request only. Deduping against the ids already on
    # disk would rename an existing entry to "<id>-2" and import it again, so
    # re-importing the same file would pile up duplicates instead of reporting
    # "already exists" (which is what the caller needs to hear).
    used: set[str] = set()

    for raw_name, spec in entries.items():
        if not isinstance(spec, dict):
            skipped.append({"name": str(raw_name), "reason": "not an object"})
            continue
        server_id = _slugify_id(str(raw_name), used)
        if not server_id:
            skipped.append({"name": str(raw_name), "reason": "cannot derive a valid id"})
            continue
        if store.exists(server_id) and not req.overwrite:
            skipped.append({"name": str(raw_name), "id": server_id,
                            "reason": "already exists (tick overwrite to replace it)"})
            continue
        try:
            cfg = _from_blob(server_id, str(raw_name), spec)
        except (ValidationError, ValueError) as e:
            skipped.append({"name": str(raw_name), "id": server_id,
                            "reason": _first_error(e)})
            continue
        store.save(cfg.id, cfg.model_dump())
        used.add(cfg.id)
        created.append({"id": cfg.id, "name": cfg.name, "transport": cfg.transport,
                        "enabled": cfg.enabled})

    manager = _manager(request)
    manager.reload()
    for entry in created:
        manager.schedule_refresh(entry["id"])
    return {"created": created, "skipped": skipped}


# ----------------------------------------------------------------------
# Per-server routes
# ----------------------------------------------------------------------

@router.post("", status_code=201)
async def create_server(server: McpServer, request: Request):
    store = _store(request)
    if store.exists(server.id):
        raise HTTPException(409, f"MCP server already exists: {server.id}")
    server = _unmask(server, {})
    store.save(server.id, server.model_dump())
    manager = _manager(request)
    manager.reload()
    # Discover in the background so the agent tool picker isn't empty until the
    # first chat turn happens to connect this server.
    manager.schedule_refresh(server.id)
    return _public(server.model_dump(), request)


@router.get("/{server_id}")
async def get_server(server_id: str, request: Request):
    data = _store(request).get(server_id)
    if data is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")
    return _public(data, request)


@router.put("/{server_id}")
async def update_server(server_id: str, server: McpServer, request: Request):
    store = _store(request)
    existing = store.get(server_id)
    if existing is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")
    server.id = server_id
    server = _unmask(server, existing)
    store.save(server_id, server.model_dump())
    manager = _manager(request)
    manager.reload()
    # Drop the live connection so the next turn reconnects with the new settings
    # (the manager also detects this by itself, this just makes it immediate).
    await manager.aclose_server(server_id)
    if not server.enabled:
        await manager.forget(server_id)
    else:
        manager.schedule_refresh(server_id)
    return _public(server.model_dump(), request)


@router.delete("/{server_id}")
async def delete_server(server_id: str, request: Request):
    store = _store(request)
    if not store.exists(server_id):
        raise HTTPException(404, f"MCP server not found: {server_id}")
    manager = _manager(request)
    await manager.forget(server_id)
    manager.drop_cache(server_id)
    store.delete(server_id)
    manager.reload()
    return {"ok": True}


@router.post("/{server_id}/refresh")
async def refresh_server(server_id: str, request: Request):
    manager = _manager(request)
    if manager.get(server_id) is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")
    status = await manager.refresh(server_id)
    return {"server": server_id, "status": status,
            "tools": manager.defs_for_tool_ids([naming.wildcard_for(server_id)])}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _slugify_id(name: str, taken: set[str]) -> str:
    """Derive a valid McpServer id from a config key ("my Server.v2" -> "my-server-v2").

    The result is validated by the McpServer model itself, so an unusable slug
    surfaces as a per-entry skip reason rather than being rejected here.
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    base = base.strip("-_")[:24].strip("-_")
    if not base:
        return ""
    if base not in taken:
        return base
    for n in range(2, 100):
        suffix = f"-{n}"
        candidate = base[: 24 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    return ""


def _from_blob(server_id: str, display_name: str, spec: dict) -> McpServer:
    """Map one Claude-Desktop-style entry onto an McpServer.

    Unknown keys (autoApprove, alwaysAllow, timeout in ms, ...) are ignored;
    `disabled: true` is honoured as enabled=False.
    """
    url = spec.get("url") or spec.get("serverUrl") or ""
    declared = (spec.get("type") or spec.get("transport") or "").lower()
    if declared == "sse":
        # The 2024-11-05 HTTP+SSE transport is a different protocol from
        # Streamable HTTP, not a variant of it: importing it as "http" would
        # produce a server that only fails at connect time, with a confusing
        # error. Say so instead. (Such a server can be fronted by mcp-remote
        # over stdio.)
        raise ValueError(
            "the deprecated HTTP+SSE transport is not supported — use a "
            "Streamable HTTP endpoint, or front it with mcp-remote over stdio"
        )
    transport = "http" if (url or declared in ("http", "streamable-http")) else "stdio"
    data = {
        "id": server_id,
        "name": display_name[:60],
        "transport": transport,
        "enabled": not bool(spec.get("disabled")),
    }
    if transport == "stdio":
        data["command"] = str(spec.get("command") or "")
        args = spec.get("args")
        data["args"] = [str(a) for a in args] if isinstance(args, list) else []
        env = spec.get("env")
        data["env"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        data["cwd"] = str(spec.get("cwd") or "")
    else:
        data["url"] = str(url)
        headers = spec.get("headers")
        data["headers"] = (
            {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        )
    return McpServer(**data)


def _first_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        errors = e.errors()
        if errors:
            first = errors[0]
            return str(first.get("msg") or first)
    return str(e)
