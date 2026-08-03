"""Pure mapping: an MCP ``CallToolResult`` -> the plain string a tool must return.

`ToolRegistry.execute()` always returns a `str` and signals failures in-band as
``"ERROR: ..."``; MCP results follow the same contract so the model can recover
from a broken server exactly as it does from a broken script tool.

Binary content (image/audio/blob resources) is NEVER inlined as base64: it would
land both in the LLM context and in the persisted session file. It is written to
``workspace/_resources/`` with the same content-addressed naming the executor
uses for user attachments, and referenced by a resource marker
(``app/tools/resources.py``) — the executor turns the marker into a short note
for the model and structured metadata for the UI, so MCP images actually SHOW
in the chat. ``_resources`` and not ``_attachments`` because sessions keep the
reference indefinitely while ``_attachments`` is pruned at 24h.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
from pathlib import Path

from app.tools import resources as resource_channel

log = logging.getLogger(__name__)

# Defensive caps: a pathological server must not make us build a huge string.
MAX_BLOCKS = 200
MAX_RAW_CHARS = 2_000_000

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/json": ".json",
}


def flatten(result: object, *, max_output: int, workspace: Path, label: str) -> str:
    """Flatten a tools/call result dict into a model-facing string."""
    if not isinstance(result, dict):
        return "ERROR: MCP server returned a malformed result"

    body: list[str] = []
    notes: list[str] = []
    blocks = result.get("content")
    if isinstance(blocks, list):
        for block in blocks[:MAX_BLOCKS]:
            if not isinstance(block, dict):
                continue
            _render_block(block, body, notes, workspace=workspace, label=label)
        if len(blocks) > MAX_BLOCKS:
            notes.append(f"[{len(blocks) - MAX_BLOCKS} more content blocks omitted]")

    text = "\n\n".join(part for part in body if part)[:MAX_RAW_CHARS]

    # structuredContent is emitted only as a fallback: the spec has servers
    # mirror it as text for back-compat, so emitting both doubles the token cost.
    if not text.strip():
        structured = result.get("structuredContent")
        if structured not in (None, {}, []):
            try:
                text = json.dumps(structured, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(structured)
            # Bounded like everything else: structuredContent can carry the same
            # blob the content path deliberately wrote to disk.
            text = text[:MAX_RAW_CHARS]

    note_text = "\n".join(notes)
    # Truncate the prose, never the file markers: the markers are the only way
    # back to content we already wrote to disk.
    room = max(200, max_output - len(note_text) - 1)
    if len(text) > room:
        text = text[:room] + "\n... [truncated]"

    out = "\n".join(part for part in (text, note_text) if part).strip()
    if not out:
        # An empty tool result reads as a failure to small models.
        out = "(no output)"
    if result.get("isError"):
        out = f"ERROR: {out}"
    return out


# ----------------------------------------------------------------------
# internals
# ----------------------------------------------------------------------

def _render_block(block: dict, body: list[str], notes: list[str], *,
                  workspace: Path, label: str) -> None:
    btype = block.get("type")

    if btype == "text":
        text = block.get("text")
        if isinstance(text, str) and text:
            body.append(text)
        return

    if btype in ("image", "audio"):
        mime = block.get("mimeType") or ("image/png" if btype == "image" else "audio/mpeg")
        notes.append(_store(block.get("data"), mime, btype, workspace, label))
        return

    if btype == "resource":
        resource = block.get("resource")
        if not isinstance(resource, dict):
            return
        uri = resource.get("uri") or ""
        text = resource.get("text")
        if isinstance(text, str) and text:
            body.append(f"[resource: {uri}]\n{text}" if uri else text)
            return
        blob = resource.get("blob")
        if blob:
            mime = resource.get("mimeType") or "application/octet-stream"
            stem = _stem_from_uri(uri) or label
            notes.append(_store(blob, mime, "resource", workspace, stem, uri=uri))
        return

    if btype == "resource_link":
        uri = block.get("uri") or ""
        name = block.get("name") or ""
        # Deliberately NOT fetched: following a server-supplied URI would make
        # the agent a proxy for arbitrary requests.
        notes.append(f"[resource: {uri}{f' ({name})' if name else ''}]")
        return

    notes.append(f"[unsupported content type: {btype}]")


def _store(data: object, mime: object, kind: str, workspace: Path, label: str,
           uri: str = "") -> str:
    """Write base64 *data* under the workspace; return the note for the model."""
    # A server can put anything in mimeType; an unhashable value would blow up
    # the lookup below and take the whole chat turn with it.
    if not isinstance(mime, str) or not mime:
        mime = "application/octet-stream"
    if not isinstance(data, str) or not data:
        return f"[{kind} ({mime}) omitted: no data]"
    try:
        raw = base64.b64decode(data, validate=False)
    except (ValueError, TypeError) as e:
        return f"[{kind} ({mime}) omitted: undecodable data: {e}]"

    size = _human_size(len(raw))
    try:
        target_dir = workspace / "_resources"
        target_dir.mkdir(parents=True, exist_ok=True)
        stem, ext = os.path.splitext(label or kind)
        if not ext:
            ext = _EXT_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)[:40] or kind
        fname = f"{safe}-{hashlib.md5(raw).hexdigest()[:8]}{ext}"
        dest = target_dir / fname
        if not dest.exists():
            dest.write_bytes(raw)
    except OSError as e:
        log.warning("cannot store MCP %s content: %s", kind, e)
        return f"[{kind} ({mime}, {size}) could not be saved: {e}]"

    title = f"{kind} from {uri}" if uri else (label or kind)
    return resource_channel.marker(f"_resources/{fname}", mime, title)


def _stem_from_uri(uri: object) -> str:
    if not isinstance(uri, str) or not uri:
        return ""
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    return tail.split("?", 1)[0][:40]


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
