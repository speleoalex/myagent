"""Shared CRUD guards for the JsonStore-backed routers (agents, models, MCP).

The three routers used to hand-roll these pre-checks and had already drifted:
one checked ``store.exists()`` (file on disk), another ``store.get()`` (file
on disk AND parseable), so the same request on a corrupt entry answered 200 in
one router and 404 in another. One definition, one semantic each:

- ``get_or_404``     — read path: an unreadable entry is as good as absent.
- ``require_exists`` — write path (PUT): the file's presence is what counts,
  so a corrupt entry stays repairable by overwriting it.
- ``require_absent`` — create path (POST): the file's presence is what counts,
  so a corrupt entry is never silently clobbered.
"""
from __future__ import annotations

from fastapi import HTTPException


def get_or_404(store, entity_id: str, what: str) -> dict:
    """The stored entity, or 404. ``get()`` semantics: corrupt == absent."""
    data = store.get(entity_id)
    if data is None:
        raise HTTPException(404, f"{what} not found: {entity_id}")
    return data


def require_exists(store, entity_id: str, what: str) -> None:
    """404 unless the entity's file exists (even if unreadable)."""
    if not store.exists(entity_id):
        raise HTTPException(404, f"{what} not found: {entity_id}")


def require_absent(store, entity_id: str, what: str) -> None:
    """409 when the entity's file exists (even if unreadable)."""
    if store.exists(entity_id):
        raise HTTPException(409, f"{what} already exists: {entity_id}")
