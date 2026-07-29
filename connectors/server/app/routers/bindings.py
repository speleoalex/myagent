"""CRUD + test + status for bot bindings, plus a small proxy to myagent's
agent list so the admin UI can offer an agent dropdown, and the /send endpoint
for unsolicited outbound messages (autonomous agents' notify_user)."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
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


class SendReq(BaseModel):
    chat_id: str | int
    text: str


def _check_send_key(request: Request) -> None:
    """Bearer gate for /send only (the rest of the API has no auth today —
    hardening the whole surface is a separate task). Empty key = open."""
    if not config.SEND_API_KEY:
        return
    auth = request.headers.get("authorization", "")
    candidate = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(candidate.encode(), config.SEND_API_KEY.encode()):
        raise HTTPException(401, "Invalid or missing API key")


@router.post("/{binding_id}/send")
async def send_to_chat(binding_id: str, req: SendReq, request: Request):
    """Send an unsolicited message to a chat through a running binding.

    Best-effort in v1: the connector's send() logs and swallows transport
    errors (Telegram chunking to 4096 included), so a 200 means "accepted",
    not "delivered"."""
    _check_send_key(request)
    connector = _manager(request).get_connector(binding_id)
    if connector is None:
        raise HTTPException(409, "Binding is not running")
    await connector.send(req.chat_id, req.text)
    # The session key travels back with the ack: it is derived from the binding's
    # session_prefix, which the caller has no way to know, and myagent needs it to
    # append this unsolicited message to the right conversation (otherwise the
    # user can see the notification in Telegram but the agent cannot).
    return {"ok": True, "session_id": connector.session_id_for(req.chat_id)}


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
