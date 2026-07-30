"""The one definition of the safe entity-id charset.

Entity ids (agents, models, tools, sessions, MCP configs) come from request
bodies or URL path segments and become file/directory names on disk, so they
are restricted to a charset that cannot traverse outside the data directories.
This regex used to live as four byte-identical copies (models, JsonStore, the
agents router, the tools router) plus five hand-rolled ``match + '..'``
predicates; a change to one silently left the others behind. Import it — never
copy it. (The connectors subproject runs in a separate process and keeps its
own literal copy of the same charset, marked "keep in sync".)
"""
from __future__ import annotations

import re

VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_valid_id(value: str) -> bool:
    """True when ``value`` is safe to use as an on-disk entity id."""
    return bool(VALID_ID.match(value or "")) and ".." not in value


def check_id(value: str) -> str:
    """Validate-and-return, for Pydantic validators and write paths."""
    if not is_valid_id(value):
        raise ValueError(
            "id may only contain letters, digits, dots, hyphens and underscores"
        )
    return value
