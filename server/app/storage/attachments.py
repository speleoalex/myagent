"""Where binary attachments land on disk so tools can open them.

Attachments arrive as base64 in a chat request, but tools take paths. They are
written under ``<workspace>/_attachments/`` and addressed by content hash, so the
same upload reused across turns keeps a stable path and is never rewritten.

Shared because there are two producers: the executor, materializing a turn's
attachments for the model to reference, and a connector plugin storing an
inbound voice note before handing it to the transcription tool.

``_resources/`` is the sibling dir with the OPPOSITE retention: ``_attachments``
is 24h scratch (swept by the executor's prune), ``_resources`` holds files the
chat UI renders and sessions reference indefinitely — never auto-pruned,
cleaned manually. Content-addressed names make re-emission a no-op, not a copy.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

from app import config

log = logging.getLogger(__name__)

SUBDIR = "_attachments"
RESOURCES_SUBDIR = "_resources"


def store_attachment(raw: bytes, name: str, kind: str = "file") -> str | None:
    """Write ``raw`` under the workspace attachments dir and return its
    workspace-relative path (``_attachments/<name>-<hash><ext>``), or None if it
    could not be written."""
    return _store_under(SUBDIR, raw, name, kind)


def store_resource(raw: bytes, name: str, kind: str = "file") -> str | None:
    """Same contract as :func:`store_attachment`, but under ``_resources/`` —
    for files meant to be SHOWN to the user via the resource channel
    (``app/tools/resources.py``), which outlive the attachments prune."""
    return _store_under(RESOURCES_SUBDIR, raw, name, kind)


def _store_under(subdir: str, raw: bytes, name: str, kind: str) -> str | None:
    """Both the stem and the extension are sanitized: an unsanitized extension
    carrying a NUL byte made write_bytes raise ValueError, which is not an
    OSError and so aborted the whole turn."""
    workdir = config.WORKSPACE_DIR / subdir
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("cannot create %s dir: %s", subdir, e)
        return None
    stem, ext = os.path.splitext(name or kind)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)[:40] or kind
    safe_ext = re.sub(r"[^A-Za-z0-9_.-]", "_", ext)[:16]
    fname = f"{safe}-{hashlib.md5(raw).hexdigest()[:8]}{safe_ext}"
    dest = workdir / fname
    if not dest.exists():
        try:
            dest.write_bytes(raw)
        except (OSError, ValueError) as e:
            log.warning("cannot write attachment '%s': %s", name, e)
            return None
    return f"{subdir}/{fname}"
