"""CRUD, token test and status for bot bindings.

Two endpoints that existed when this was a separate server are gone: the proxy
to myagent's agent list (the UI is now part of myagent and calls
``GET /api/agents?selectable=true`` itself) and ``POST /{id}/send`` (its only
caller was the notify_user tool, which now reaches the connector in-process).
Authentication is gone too, in the good sense: mounted under /api these routes
are covered by myagent's own MYAGENT_API_KEY gate, so the bot tokens are no
longer served by an unauthenticated endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.routers.secrets import SECRET_MASK

from myagent_connectors.channels.registry import available_types, create_connector
from myagent_connectors.models import Binding
from myagent_connectors.services import services

router = APIRouter()


def _masked(data: dict) -> dict:
    """The public shape of a binding: validated through the model, secrets masked.

    Going through ``Binding`` matters for more than tidiness — it is what makes a
    response agree with the schema. Stored files written before identifiers became
    strings hold ints, and returning them raw meant GET answered with ints while
    PUT accepted strings. A file that no longer validates is still listed, as-is,
    rather than making the whole list fail.
    """
    try:
        data = Binding(**data).model_dump()
    except Exception:
        data = dict(data)
    if data.get("token"):
        data["token"] = SECRET_MASK
    # never leak the plaintext activation password either
    if data.get("password"):
        data["password"] = SECRET_MASK
    return data


# Declared before /{binding_id}: otherwise the path parameter swallows it.
@router.get("/types")
async def list_types():
    return {"types": available_types()}


@router.get("")
async def list_bindings(request: Request):
    svc = services(request)
    out = []
    for data in svc.bindings.list_all():
        d = _masked(data)
        d["status"] = svc.manager.status(data["id"])
        out.append(d)
    return out


@router.get("/{binding_id}")
async def get_binding(binding_id: str, request: Request):
    svc = services(request)
    data = svc.bindings.get(binding_id)
    if data is None:
        raise HTTPException(404, "Binding not found")
    d = _masked(data)
    d["status"] = svc.manager.status(binding_id)
    return d


@router.post("")
async def create_binding(binding: Binding, request: Request):
    svc = services(request)
    if svc.bindings.get(binding.id) is not None:
        raise HTTPException(409, "A binding with this id already exists")
    svc.bindings.save(binding.id, binding.model_dump())
    await svc.manager.reconcile(binding.id)
    return _masked(svc.bindings.get(binding.id))


@router.put("/{binding_id}")
async def update_binding(binding_id: str, binding: Binding, request: Request):
    svc = services(request)
    existing = svc.bindings.get(binding_id)
    if existing is None:
        raise HTTPException(404, "Binding not found")
    if binding.id != binding_id:
        raise HTTPException(400, "id mismatch")
    data = binding.model_dump()
    # Masked/empty secrets mean "keep the stored value".
    if data.get("token") in ("", SECRET_MASK):
        data["token"] = existing.get("token", "")
    if data.get("password") in ("", SECRET_MASK):
        data["password"] = existing.get("password", "")
    svc.bindings.save(binding_id, data)
    # Saving is also the way out of an auto-pause: editing the configuration is
    # an explicit statement of intent (same rule as re-saving a live agent).
    await svc.manager.reconcile(binding_id)
    return _masked(svc.bindings.get(binding_id))


@router.delete("/{binding_id}")
async def delete_binding(binding_id: str, request: Request):
    svc = services(request)
    if not svc.bindings.delete(binding_id):
        raise HTTPException(404, "Binding not found")
    svc.grants.clear(binding_id)
    await svc.manager.reconcile(binding_id)
    return {"ok": True}


@router.post("/{binding_id}/resume")
async def resume_binding(binding_id: str, request: Request):
    """Clear an auto-pause and reconnect. The counterpart of the scheduler's
    /api/autonomy/{id}/resume: a paused connector stays down until asked."""
    svc = services(request)
    if svc.bindings.get(binding_id) is None:
        raise HTTPException(404, "Binding not found")
    await svc.manager.resume(binding_id)
    return {"ok": True, "status": svc.manager.status(binding_id)}


class TestReq(BaseModel):
    type: str = "telegram"
    token: str = ""
    url: str = ""    # device URL, for channels verified by probing the device


async def _verify(binding: Binding, request: Request) -> dict:
    """Build the binding's connector from the registry and ask it to check its
    own credentials. No channel type is named here: an unknown type fails in
    create_connector, and a channel without a verify() says so itself."""
    svc = services(request)
    try:
        connector = create_connector(binding, svc.core, svc.grants)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        return {"ok": True, **(await connector.verify() or {})}
    except NotImplementedError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Invalid credentials: {e}")


@router.post("/test")
async def test_token(req: TestReq, request: Request):
    """Validate credentials before saving them."""
    if req.token in ("", SECRET_MASK):
        raise HTTPException(400, "Provide a token to test")
    return await _verify(Binding(id="probe", type=req.type, token=req.token,
                                 url=req.url), request)


@router.post("/{binding_id}/test")
async def test_binding(binding_id: str, request: Request):
    """Validate the STORED credentials: the UI only ever sees the mask, so it
    cannot send the token back for a plain /test."""
    data = services(request).bindings.get(binding_id)
    if data is None:
        raise HTTPException(404, "Binding not found")
    return await _verify(Binding(**data), request)
