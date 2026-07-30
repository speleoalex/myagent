"""CRUD for the address book (contacts).

Contacts are plain records (name + messaging ids) used by the admin UI: the
binding form's authorized-users field offers them as one-click chips. No
secrets inside, so nothing is masked.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from myagent_connectors.models import Contact
from myagent_connectors.services import services

router = APIRouter()


@router.get("")
async def list_contacts(request: Request):
    out = services(request).contacts.list_all()
    out.sort(key=lambda c: (c.get("name") or c.get("id") or "").lower())
    return out


@router.get("/{contact_id}")
async def get_contact(contact_id: str, request: Request):
    data = services(request).contacts.get(contact_id)
    if data is None:
        raise HTTPException(404, "Contact not found")
    return data


@router.post("")
async def create_contact(contact: Contact, request: Request):
    store = services(request).contacts
    if store.get(contact.id) is not None:
        raise HTTPException(409, "A contact with this id already exists")
    store.save(contact.id, contact.model_dump())
    return store.get(contact.id)


@router.put("/{contact_id}")
async def update_contact(contact_id: str, contact: Contact, request: Request):
    store = services(request).contacts
    if store.get(contact_id) is None:
        raise HTTPException(404, "Contact not found")
    if contact.id != contact_id:
        raise HTTPException(400, "id mismatch")
    store.save(contact_id, contact.model_dump())
    return store.get(contact_id)


@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, request: Request):
    if not services(request).contacts.delete(contact_id):
        raise HTTPException(404, "Contact not found")
    return {"ok": True}
