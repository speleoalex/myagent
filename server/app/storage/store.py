import json
from pathlib import Path

# Entity ids come from request bodies and become filenames: allow only a safe
# charset (no path separators, no dot-dot) to prevent path traversal.
from app.ids import is_valid_id
from app.storage.sessions import write_json


class JsonStore:
    """Generic JSON file store. Each entity is one .json file."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, entity_id: str) -> Path:
        if not is_valid_id(entity_id):
            raise ValueError(f"Invalid id: {entity_id!r}")
        return self.directory / f"{entity_id}.json"

    def mtime(self, entity_id: str) -> float:
        """Last-modified time of an entity's file, 0.0 when unknown/invalid.
        Public accessor so callers never need to touch ``_path`` directly."""
        try:
            return self._path(entity_id).stat().st_mtime
        except (OSError, ValueError):
            return 0.0

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
        # Atomic write; restrictive perms since entities may hold secrets
        # (e.g. model API keys).
        write_json(self._path(entity_id), data, mode=0o600)

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
