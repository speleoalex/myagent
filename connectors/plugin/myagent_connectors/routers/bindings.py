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

from app.routers.crud import get_or_404, require_absent, require_exists
from app.routers.secrets import SECRET_MASK

from myagent_connectors.channels.base import Unreachable
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
    d = _masked(get_or_404(svc.bindings, binding_id, "Binding"))
    d["status"] = svc.manager.status(binding_id)
    return d


@router.post("")
async def create_binding(binding: Binding, request: Request):
    svc = services(request)
    require_absent(svc.bindings, binding.id, "Binding")
    svc.bindings.save(binding.id, binding.model_dump())
    await svc.manager.reconcile(binding.id)
    return _masked(svc.bindings.get(binding.id))


@router.put("/{binding_id}")
async def update_binding(binding_id: str, binding: Binding, request: Request):
    svc = services(request)
    # require_exists, not get(): a corrupt file must stay repairable by
    # overwriting it (the drift crud.py was written to end).
    require_exists(svc.bindings, binding_id, "Binding")
    existing = svc.bindings.get(binding_id) or {}
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
    require_exists(svc.bindings, binding_id, "Binding")
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
    except Unreachable as e:
        # Verbatim: it already names the address that was tried. Prefixing this
        # with "Invalid credentials" is what sent a user hunting for a wrong
        # token while the device was simply switched off.
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
    data = get_or_404(services(request).bindings, binding_id, "Binding")
    return await _verify(Binding(**data), request)


# ------------------------------------------------------- device configuration
# Some channels are a DEVICE we can configure back (the voice satellite: its
# language, voice and microphone thresholds live in a file on the device). These
# three routes are deliberately generic — no channel type is named, exactly as
# in _verify: a channel either offers the methods or the route reports 501. That
# way a second device channel needs no route of its own.
def _device_connector(binding_id: str, request: Request, method: str):
    svc = services(request)
    data = get_or_404(svc.bindings, binding_id, "Binding")
    try:
        connector = create_connector(Binding(**data), svc.core, svc.grants)
    except ValueError as e:
        raise HTTPException(400, str(e))
    fn = getattr(connector, method, None)
    if fn is None:
        raise HTTPException(501, "This channel has no device to configure")
    return fn


async def _device_call(fn, *args, **kwargs) -> dict:
    """A device is a thing on a LAN: unplugged, moved, asleep. That is normal
    operation, not a server fault, so it answers 400 with the device's own
    message for the form to show — never a 500."""
    try:
        return await fn(*args, **kwargs)
    except Exception as e:
        raise HTTPException(400, str(e) or e.__class__.__name__)


@router.get("/{binding_id}/device")
async def get_device_config(binding_id: str, request: Request):
    return await _device_call(
        _device_connector(binding_id, request, "device_config"))


@router.put("/{binding_id}/device")
async def put_device_config(binding_id: str, patch: dict, request: Request):
    """The patch is passed through as-is: the DEVICE owns the writable list
    (its pairing fields are refused there, next to the file), so duplicating it
    here would be a second definition free to drift from the first."""
    return await _device_call(
        _device_connector(binding_id, request, "device_config_update"), patch)


class VoiceReq(BaseModel):
    name: str
    use: bool = True    # speak with it once installed — the reason to install


@router.post("/{binding_id}/device/voices")
async def install_device_voice(binding_id: str, req: VoiceReq, request: Request):
    return await _device_call(
        _device_connector(binding_id, request, "install_voice"),
        req.name, req.use)
