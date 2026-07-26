"""Disk storage for bindings and password grants.

One JSON file per binding (``bindings/<id>.json``), mirroring myagent's simple
JsonStore pattern. Bot tokens are written with 0600 perms and never returned to
the UI in clear (the router masks them).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class BindingStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, binding_id: str) -> Path:
        return self.base / f"{binding_id}.json"

    def list_all(self) -> list[dict]:
        out = []
        for f in sorted(self.base.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def get(self, binding_id: str) -> dict | None:
        p = self._path(binding_id)
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
        # Tokens are secrets: restrict perms before the file becomes visible.
        os.chmod(tmp, 0o600)
        tmp.replace(p)
        return data

    def delete(self, binding_id: str) -> bool:
        p = self._path(binding_id)
        if p.exists():
            p.unlink()
            return True
        return False


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
