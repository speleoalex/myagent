"""Pure helpers: qualified tool names and JSON Schema sanitizing.

No I/O, no MCP wire knowledge — safe to unit-test standalone.

Tool names reach the LLM as OpenAI function names, which must match
``^[a-zA-Z0-9_-]{1,64}$`` (remote OpenAI-compatible gateways reject dots), so a
remote tool name is prefixed with its server id and rewritten to fit:

    mcp_<server_id>_<sanitized name>          e.g. mcp_fs_read_text_file

The mapping is authoritative in the tool metadata (``meta["mcp"]["tool"]``) and
is NEVER recovered by parsing the qualified name back apart: server ids may
contain underscores, so parsing would be ambiguous. Only the *server* segment is
recovered by splitting, which is unambiguous because the prefix is fixed.
"""

from __future__ import annotations

import hashlib
import re

from app.models import MCP_ID_MAX_LEN

# OpenAI function-name budget.
MAX_NAME_LEN = 64
PREFIX = "mcp_"

# Wildcard entry in Agent.tools meaning "every tool this server exposes".
# Deliberately not a valid tool id, so it can never collide with one. The id
# charset/length inside must match models._VALID_MCP_ID — hence the shared cap.
WILDCARD_RE = re.compile(r"^mcp:([a-z0-9][a-z0-9_-]{0,%d})/\*$" % (MCP_ID_MAX_LEN - 1))

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
# Control characters never forwarded to the UI/model verbatim. Shared with
# client._clean_error_body — one definition of "what gets stripped".
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CONTROL = CONTROL_CHARS

# Description limits. MCP descriptions are third-party text that lands verbatim
# in the system prompt (twice, for text-fallback models: once in the tool schema
# and once in the rendered tool list), so they are capped hard.
DESC_LIMIT = 200
PROP_DESC_LIMIT = 120

# JSON Schema keywords we forward. Everything else ($schema, $comment, unevaluated*,
# dependentSchemas, ...) is dropped: llama.cpp derives GBNF grammars from these
# schemas and strict gateways reject unknown keywords.
_ALLOWED_KEYS = {
    "type", "description", "title", "enum", "const", "default",
    "properties", "required", "items", "additionalProperties",
    "minimum", "maximum", "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "format",
}
_JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}

_MAX_PROPS = 64
_MAX_DEPTH = 6


def wildcard_for(server_id: str) -> str:
    return f"mcp:{server_id}/*"


def parse_wildcard(entry: str) -> str | None:
    """Return the server id if *entry* is a wildcard tool entry, else None."""
    m = WILDCARD_RE.match(entry or "")
    return m.group(1) if m else None


def after_prefix(tool_id: str) -> str | None:
    """Everything after the fixed ``mcp_`` prefix (``server id + '_' + tool``),
    or None when the id is not MCP-qualified. It deliberately does NOT split
    the server id out: server ids may contain underscores, so only the caller
    — matching against the set of configured ids — can do that unambiguously
    (see McpManager.servers_for_tool_ids)."""
    if not tool_id or not tool_id.startswith(PREFIX):
        return None
    return tool_id[len(PREFIX):] or None


def qualify(server_id: str, tool_name: str) -> str:
    """Build the LLM-visible function name for a remote MCP tool."""
    prefix = f"{PREFIX}{server_id}_"
    room = MAX_NAME_LEN - len(prefix)
    safe = _UNSAFE.sub("_", tool_name or "") or "tool"
    if safe == tool_name and len(safe) <= room:
        return prefix + safe
    # The name was rewritten or truncated: append a short digest of the ORIGINAL
    # name so two distinct remote tools can never collapse onto one function
    # name. sha1 of the name is stable across restarts (unlike a _2/_3 suffix
    # assigned in discovery order).
    digest = "_" + hashlib.sha1((tool_name or "").encode("utf-8")).hexdigest()[:6]
    body = safe[: max(1, room - len(digest))]
    return prefix + body + digest


def clean_text(text: object, limit: int) -> str:
    """Strip control characters, collapse blank runs and truncate."""
    if not isinstance(text, str):
        return ""
    out = _CONTROL.sub(" ", text).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    if len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out


def truncate_description(text: object) -> str:
    return clean_text(text, DESC_LIMIT)


def sanitize_schema(schema: object) -> dict:
    """Coerce an MCP ``inputSchema`` into a plain, tool-calling-safe object schema.

    MCP input schemas are JSON Schema 2020-12 and routinely carry ``$ref``/
    ``$defs``, ``anyOf``, ``"type": ["string", "null"]`` and vendor keywords.
    Ollama, llama.cpp grammars and strict remote gateways each choke on a
    different subset, so the schema is flattened to the common denominator:
    a single object with plain-typed properties. This is lossy by design.
    """
    empty = {"type": "object", "properties": {}, "required": []}
    if not isinstance(schema, dict):
        return empty

    defs: dict = {}
    for key in ("$defs", "definitions"):
        block = schema.get(key)
        if isinstance(block, dict):
            defs.update({k: v for k, v in block.items() if isinstance(v, dict)})

    out = _clean(schema, defs, _MAX_DEPTH)
    out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    required = out.get("required")
    required = [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
    # A `required` entry with no matching property makes some validators reject
    # every call, so keep only the ones that survived sanitizing.
    out["required"] = [r for r in required if r in out["properties"]]
    return out


# ----------------------------------------------------------------------
# internals
# ----------------------------------------------------------------------

def _resolve_ref(ref: str, defs: dict) -> dict | None:
    for marker in ("#/$defs/", "#/definitions/"):
        if ref.startswith(marker):
            return defs.get(ref[len(marker):])
    return None


def _deref(node: dict, defs: dict) -> dict:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    rest = {k: v for k, v in node.items() if k != "$ref"}
    target = _resolve_ref(ref, defs)
    return {**target, **rest} if isinstance(target, dict) else rest


def _collapse_type(value: object) -> str | None:
    """`["string", "null"]` -> `"string"`; unknown types are dropped."""
    if isinstance(value, str):
        return value if value in _JSON_TYPES else None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item in _JSON_TYPES and item != "null":
                return item
    return None


def _clean(node: object, defs: dict, depth: int, *, desc_limit: int = DESC_LIMIT) -> dict:
    if not isinstance(node, dict) or depth <= 0:
        return {}
    node = _deref(node, defs)

    # Composition keywords: pick the first usable branch (anyOf/oneOf) or
    # shallow-merge them (allOf), then let the node's own keywords win.
    base: dict = {}
    branches = next(
        (node[k] for k in ("anyOf", "oneOf") if isinstance(node.get(k), list)), None
    )
    if branches:
        for branch in branches:
            if isinstance(branch, dict) and branch.get("type") != "null":
                base = _clean(branch, defs, depth - 1, desc_limit=desc_limit)
                break
    elif isinstance(node.get("allOf"), list):
        for branch in node["allOf"]:
            sub = _clean(branch, defs, depth - 1, desc_limit=desc_limit)
            props = {**base.get("properties", {}), **sub.get("properties", {})}
            required = list(dict.fromkeys(base.get("required", []) + sub.get("required", [])))
            base = {**base, **sub}
            if props:
                base["properties"] = props
            if required:
                base["required"] = required

    out = dict(base)
    for key, value in node.items():
        if key not in _ALLOWED_KEYS:
            continue
        if key == "type":
            collapsed = _collapse_type(value)
            if collapsed:
                out["type"] = collapsed
        elif key == "properties":
            if isinstance(value, dict):
                out["properties"] = {
                    str(name): _clean(sub, defs, depth - 1, desc_limit=PROP_DESC_LIMIT)
                    for name, sub in list(value.items())[:_MAX_PROPS]
                }
        elif key == "items":
            out["items"] = _clean(value, defs, depth - 1, desc_limit=PROP_DESC_LIMIT)
        elif key == "additionalProperties":
            if isinstance(value, bool):
                out["additionalProperties"] = value
            elif isinstance(value, dict):
                out["additionalProperties"] = _clean(
                    value, defs, depth - 1, desc_limit=PROP_DESC_LIMIT
                )
        elif key == "required":
            if isinstance(value, list):
                out["required"] = [r for r in value if isinstance(r, str)]
        elif key in ("description", "title"):
            text = clean_text(value, desc_limit)
            if text:
                out[key] = text
        elif key == "enum":
            if isinstance(value, list):
                out["enum"] = value[:64]
        else:
            out[key] = value
    return out
