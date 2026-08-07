"""Per-binding id sets written by the connector at runtime.

Bindings and contacts are plain entities: they use the core's ``JsonStore``
directly (atomic write, 0600 perms, id validated against the one charset
definition) — this module used to carry a near-copy of it, which is exactly the
kind of duplication that drifts.

What lives here instead is the shape ``JsonStore`` does not cover: "the set of
messaging ids that reached some state for one binding". Two things have that
shape — who unlocked a password-protected bot, and who has already been told
they are talking to an AI — so the mechanics sit in one base class and each
concept is a thin subclass naming its own field.
"""
from __future__ import annotations

from pathlib import Path

from app.storage.store import JsonStore


class _IdSetStore:
    """One record per binding, holding a set of messaging ids.

    Every read degrades to an empty set: a missing or corrupt file is normal
    (nobody has reached this state yet) and must never raise on the message
    path. What "empty" means is chosen by the subclass so that degrading is
    always the safe direction — for grants it means "not authorized", for
    disclosures "not told yet, tell them again".

    Ids are STRINGS, matching the rest of the access-control path. Files written
    before identifiers became strings hold ints, so they are coerced on read
    rather than migrated.
    """

    #: key under which the id list is stored in the record
    field = "ids"

    def __init__(self, base_dir: Path):
        self._store = JsonStore(Path(base_dir))

    def get(self, binding_id: str) -> set[str]:
        data = self._store.get(binding_id) or {}
        raw = data.get(self.field, [])
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(u).strip() for u in raw if str(u).strip()}

    def add(self, binding_id: str, user_id) -> None:
        user_id = str(user_id).strip()
        if not user_id:
            return
        ids = self.get(binding_id)
        if user_id in ids:
            return
        ids.add(user_id)
        self._store.save(binding_id, {"id": binding_id, self.field: sorted(ids)})

    def clear(self, binding_id: str) -> None:
        self._store.delete(binding_id)


class GrantStore(_IdSetStore):
    """The set of user ids that have unlocked each binding via password.

    Persisted so grants survive a restart.
    """

    field = "user_ids"


class DisclosureStore(_IdSetStore):
    """The set of chats already told that the replies come from an AI.

    Persisted for one reason: without it every service restart would re-announce
    it to everyone, and a notice repeated at random is the kind of noise an
    operator switches off — which would cost the disclosure entirely. Losing the
    file is harmless in the other direction: the chat simply gets told once more.

    Keyed by CHAT id, not user id: the disclosure is addressed to whoever reads
    that conversation, and in a group that is not one person.
    """

    field = "chat_ids"
