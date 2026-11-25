"""Settings a tool declares, and the values the user gave them.

Before this, every user tool invented its own place for parameters and
credentials (send_email on the Orin: a hand-made ``config/send_email.json``).
Now a tool lists what it needs in the ``config`` block of its tool.json::

    "config": [
      {"key": "smtp_host", "label": "SMTP server"},
      {"key": "password", "label": "Password", "secret": true}
    ]

The Tools page renders the form, the values are stored under
``config.TOOL_CONFIG_DIR/<tool_id>.json`` (0600, via JsonStore) and every run of
the tool receives them as environment variables: ``smtp_host`` arrives as
``TOOL_SMTP_HOST``. A prefix, not the bare key, so a tool cannot shadow
``PATH`` or a ``MYAGENT_*`` variable by declaring it. Secrets follow the rule of
every other secret in the app (routers/secrets.py): masked on read, and the
mask sent back means "keep what is stored".
"""
from __future__ import annotations

import re

from app import config
from app.routers.secrets import SECRET_MASK
from app.storage.store import JsonStore

ENV_PREFIX = "TOOL_"
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _store() -> JsonStore:
    # Built per call: cheap, and a test (or a moved MYAGENT_CONFIG) sees the
    # current directory instead of the one bound at import.
    return JsonStore(config.TOOL_CONFIG_DIR)


def fields(meta: dict) -> list[dict]:
    """The declared settings, normalized. An entry with a bad key is dropped
    rather than failing the whole tool: tool.json is hand-written."""
    out = []
    for f in meta.get("config") or []:
        if not isinstance(f, dict) or not _KEY.match(str(f.get("key") or "")):
            continue
        out.append({
            "key": f["key"],
            "label": str(f.get("label") or f["key"]),
            "description": str(f.get("description") or ""),
            "secret": bool(f.get("secret")),
            "default": str(f.get("default") or ""),
        })
    return out


def values(tool_id: str, meta: dict) -> dict[str, str]:
    """The effective value of every declared setting: stored, else default."""
    stored = _store().get(tool_id) or {}
    return {f["key"]: str(stored.get(f["key"], f["default"])) for f in fields(meta)}


def env(tool_id: str, meta: dict) -> dict[str, str]:
    """What a run of this tool receives. Empty values are left out, so a tool
    can tell "not configured" from "configured as empty" by presence."""
    return {ENV_PREFIX + k.upper(): v for k, v in values(tool_id, meta).items() if v}


def public(tool_id: str, meta: dict) -> dict:
    """Fields and values for the UI, secrets masked."""
    declared = fields(meta)
    current = values(tool_id, meta)
    return {
        "fields": declared,
        "values": {f["key"]: (SECRET_MASK if f["secret"] and current[f["key"]]
                              else current[f["key"]]) for f in declared},
    }


def save(tool_id: str, meta: dict, submitted: dict) -> None:
    """Store the submitted values. Unknown keys are refused (ValueError): a
    typo would otherwise be saved and silently never reach the tool."""
    declared = {f["key"]: f for f in fields(meta)}
    unknown = sorted(set(submitted) - set(declared))
    if unknown:
        raise ValueError(f"not declared by the tool: {', '.join(unknown)}")
    stored = _store().get(tool_id) or {}
    out = {}
    for key, f in declared.items():
        value = submitted.get(key, stored.get(key, f["default"]))
        if f["secret"] and value == SECRET_MASK:
            value = stored.get(key, "")
        out[key] = str(value or "")
    _store().save(tool_id, out)


def drop(tool_id: str) -> None:
    """Forget a deleted tool's values."""
    _store().delete(tool_id)
