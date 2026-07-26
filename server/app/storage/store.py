import json
import os
import re
from pathlib import Path

# Entity ids come from request bodies and become filenames: allow only a safe
# charset (no path separators, no dot-dot) to prevent path traversal.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class JsonStore:
    """Generic JSON file store. Each entity is one .json file."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, entity_id: str) -> Path:
        if not _VALID_ID.match(entity_id or "") or ".." in entity_id:
            raise ValueError(f"Invalid id: {entity_id!r}")
        return self.directory / f"{entity_id}.json"

    def list_all(self) -> list[dict]:
        results = []
        for f in sorted(self.directory.glob("*.json")):
            try:
                with open(f) as fh:
                    results.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                continue
        return results

    def get(self, entity_id: str) -> dict | None:
        try:
            path = self._path(entity_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, entity_id: str, data: dict) -> None:
        path = self._path(entity_id)
        # Atomic write; restrictive perms since entities may hold secrets
        # (e.g. model API keys).
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def delete(self, entity_id: str) -> bool:
        try:
            path = self._path(entity_id)
        except ValueError:
            return False
        if path.exists():
            path.unlink()
            return True
        return False

    def exists(self, entity_id: str) -> bool:
        try:
            return self._path(entity_id).exists()
        except ValueError:
            return False
