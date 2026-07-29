"""Disk storage for bindings, contacts and password grants.

One JSON file per record (``bindings/<id>.json``, ``contacts/<id>.json``),
mirroring myagent's simple JsonStore pattern. Bot tokens are written with 0600
perms and never returned to the UI in clear (the router masks them).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class JsonDirStore:
    """One JSON file per record, keyed by the record's ``id`` field.

    ``file_mode`` (when set) restricts perms before the file becomes visible —
    used for records holding secrets.
    """
    def __init__(self, base_dir: Path, file_mode: int | None = None):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self.file_mode = file_mode

    def _path(self, record_id: str) -> Path:
        return self.base / f"{record_id}.json"

    def list_all(self) -> list[dict]:
        out = []
        for f in sorted(self.base.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def get(self, record_id: str) -> dict | None:
        p = self._path(record_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, data: dict) -> dict:
        p = self._path(data["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        if self.file_mode is not None:
            os.chmod(tmp, self.file_mode)
        tmp.replace(p)
        return data

    def delete(self, record_id: str) -> bool:
        p = self._path(record_id)
        if p.exists():
            p.unlink()
            return True
        return False


class BindingStore(JsonDirStore):
    def __init__(self, base_dir: Path):
        # Tokens are secrets: restrict perms before the file becomes visible.
        super().__init__(base_dir, file_mode=0o600)


class ContactStore(JsonDirStore):
    """Address book: people the admin knows, with their messaging ids.
    No secrets inside — default perms are fine."""


class GrantStore:
    """Password-mode grants: the set of messaging user ids that have unlocked a
    binding by sending the correct password. Persisted so grants survive a
    restart. One file per binding: ``grants/<binding_id>.json`` (a list of ids).
    """
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, binding_id: str) -> Path:
        return self.base / f"{binding_id}.json"

    def get(self, binding_id: str) -> set[int]:
        p = self._path(binding_id)
        if not p.exists():
            return set()
        try:
            return set(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError, TypeError):
            return set()

    def add(self, binding_id: str, user_id: int) -> None:
        ids = self.get(binding_id)
        if user_id in ids:
            return
        ids.add(user_id)
        p = self._path(binding_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(ids)))
        tmp.replace(p)

    def clear(self, binding_id: str) -> None:
        self._path(binding_id).unlink(missing_ok=True)
