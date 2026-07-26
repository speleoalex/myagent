"""CRUD + test + status for bot bindings, plus a small proxy to myagent's
agent list so the admin UI can offer an agent dropdown."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.channels.registry import available_types
from app.channels.telegram import TelegramConnector
from app.models import Binding
from app.myagent_client import MyAgentClient

router = APIRouter()

# Write-only secret: the UI receives this sentinel instead of the real token; a
# PUT that echoes it back means "keep the stored token".
TOKEN_MASK = "********"


def _bindings(request: Request):
    return request.app.state.bindings


def _manager(request: Request):
    return request.app.state.manager


def _masked(data: dict) -> dict:
    if data.get("token"):
        data = {**data, "token": TOKEN_MASK}
    # never leak the plaintext activation password either
    if data.get("password"):
        data = {**data, "password": TOKEN_MASK}
    return data


@router.get("/types")
async def list_types():
    return {"types": available_types()}


@router.get("/agents")
async def list_agents(request: Request):
    """Proxy myagent's agent list for the UI's agent picker.

    Only selectable (i.e. enabled) agents are offered as binding targets. The
    ``callable`` flag is not involved: it gates agent→agent delegation, not what
    a user may drive directly from a channel."""
    client: MyAgentClient = request.app.state.myagent
    try:
        agents = await client.list_agents(selectable=True)
    except Exception as e:
        raise HTTPException(502, f"Cannot reach myagent: {e}")
    return [{"id": a.get("id"), "name": a.get("name", a.get("id"))} for a in agents]


@router.get("")
async def list_bindings(request: Request):
    store = _bindings(request)
    mgr = _manager(request)
    out = []
    for data in store.list_all():
        d = _masked(data)
        d["status"] = mgr.status(data["id"])
        out.append(d)
    return out


@router.get("/{binding_id}")
async def get_binding(binding_id: str, request: Request):
    data = _bindings(request).get(binding_id)
    if data is None:
        raise HTTPException(404, "Binding not found")
    d = _masked(data)
    d["status"] = _manager(request).status(binding_id)
    return d


@router.post("")
async def create_binding(binding: Binding, request: Request):
    store = _bindings(request)
    if store.get(binding.id) is not None:
        raise HTTPException(409, "A binding with this id already exists")
    store.save(binding.model_dump())
    await _manager(request).reconcile(binding.id)
    return _masked(store.get(binding.id))


@router.put("/{binding_id}")
async def update_binding(binding_id: str, binding: Binding, request: Request):
    store = _bindings(request)
    existing = store.get(binding_id)
    if existing is None:
        raise HTTPException(404, "Binding not found")
    if binding.id != binding_id:
        raise HTTPException(400, "id mismatch")
    data = binding.model_dump()
    # Masked/empty secrets mean "keep the stored value".
    if data.get("token") in ("", TOKEN_MASK):
        data["token"] = existing.get("token", "")
    if data.get("password") in ("", TOKEN_MASK):
        data["password"] = existing.get("password", "")
    store.save(data)
    await _manager(request).reconcile(binding_id)
    return _masked(store.get(binding_id))


@router.delete("/{binding_id}")
async def delete_binding(binding_id: str, request: Request):
    store = _bindings(request)
    if not store.delete(binding_id):
        raise HTTPException(404, "Binding not found")
    request.app.state.grants.clear(binding_id)
    await _manager(request).reconcile(binding_id)
    return {"ok": True}


class TestReq(BaseModel):
    type: str = "telegram"
    token: str = ""


@router.post("/test")
async def test_token(req: TestReq, request: Request):
    """Validate a token before saving (getMe for Telegram)."""
    token = req.token
    if token in ("", TOKEN_MASK):
        raise HTTPException(400, "Provide a token to test")
    if req.type != "telegram":
        raise HTTPException(400, f"Test not supported for type '{req.type}'")
    probe = TelegramConnector(
        Binding(id="probe", type="telegram", token=token),
        request.app.state.myagent, request.app.state.grants,
    )
    try:
        me = await probe.get_me()
    except Exception as e:
        raise HTTPException(400, f"Token non valido: {e}")
    return {"ok": True, "bot": me.get("username"), "name": me.get("first_name")}


@router.post("/{binding_id}/test")
async def test_binding(binding_id: str, request: Request):
    data = _bindings(request).get(binding_id)
    if data is None:
        raise HTTPException(404, "Binding not found")
    b = Binding(**data)
    if b.type != "telegram":
        raise HTTPException(400, f"Test not supported for type '{b.type}'")
    probe = TelegramConnector(b, request.app.state.myagent, request.app.state.grants)
    try:
        me = await probe.get_me()
    except Exception as e:
        raise HTTPException(400, f"Token non valido: {e}")
    return {"ok": True, "bot": me.get("username"), "name": me.get("first_name")}
