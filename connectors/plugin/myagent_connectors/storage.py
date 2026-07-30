"""Password-mode grants.

Bindings and contacts are plain entities: they use the core's ``JsonStore``
directly (atomic write, 0600 perms, id validated against the one charset
definition) — this module used to carry a near-copy of it, which is exactly the
kind of duplication that drifts.

Grants need their own class only because a grant record is not an entity the
user edits: it is the set of messaging user ids that unlocked one binding by
sending its password, and it is written by the connector at runtime.
"""
from __future__ import annotations

from pathlib import Path

from app.storage.store import JsonStore


class GrantStore:
    """The set of user ids that have unlocked each binding via password.

    Persisted so grants survive a restart: one record per binding, holding the
    id list. Every read degrades to an empty set — a missing or corrupt grant
    file must mean "not authorized yet", never an exception on the message path.
    """

    def __init__(self, base_dir: Path):
        self._store = JsonStore(Path(base_dir))

    def get(self, binding_id: str) -> set[int]:
        data = self._store.get(binding_id) or {}
        try:
            return {int(u) for u in data.get("user_ids", [])}
        except (TypeError, ValueError):
            return set()

    def add(self, binding_id: str, user_id: int) -> None:
        ids = self.get(binding_id)
        if user_id in ids:
            return
        ids.add(user_id)
        self._store.save(binding_id, {"id": binding_id, "user_ids": sorted(ids)})

    def clear(self, binding_id: str) -> None:
        self._store.delete(binding_id)
