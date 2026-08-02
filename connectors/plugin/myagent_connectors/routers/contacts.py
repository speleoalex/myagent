"""CRUD for the address book (contacts).

Contacts are plain records (name + messaging ids) used by the admin UI: the
binding form's authorized-users field offers them as one-click chips. No
secrets inside, so nothing is masked.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.routers.crud import get_or_404, require_absent, require_exists

from myagent_connectors.models import Contact
from myagent_connectors.services import services

router = APIRouter()


def _public(data: dict) -> dict:
    """A contact as the API describes it: validated through the model.

    Same reason as the bindings router — stored files written before per-channel
    handles existed carry ``user_id``/``username``, and the model folds them into
    ``handles`` on load. Returning the raw dict would leave the frontend (and the
    agent) reading fields the schema no longer has. A file that cannot be
    validated is passed through untouched rather than breaking the list."""
    try:
        return Contact(**data).model_dump()
    except Exception:
        return data


@router.get("")
async def list_contacts(request: Request):
    out = [_public(c) for c in services(request).contacts.list_all()]
    out.sort(key=lambda c: (c.get("name") or c.get("id") or "").lower())
    return out


@router.get("/{contact_id}")
async def get_contact(contact_id: str, request: Request):
    return _public(get_or_404(services(request).contacts, contact_id, "Contact"))


@router.post("")
async def create_contact(contact: Contact, request: Request):
    store = services(request).contacts
    require_absent(store, contact.id, "Contact")
    store.save(contact.id, contact.model_dump())
    return _public(store.get(contact.id))


@router.put("/{contact_id}")
async def update_contact(contact_id: str, contact: Contact, request: Request):
    store = services(request).contacts
    # require_exists, not get(): a corrupt file stays repairable by overwrite.
    require_exists(store, contact_id, "Contact")
    if contact.id != contact_id:
        raise HTTPException(400, "id mismatch")
    store.save(contact_id, contact.model_dump())
    return _public(store.get(contact_id))


@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, request: Request):
    if not services(request).contacts.delete(contact_id):
        raise HTTPException(404, "Contact not found")
    return {"ok": True}
