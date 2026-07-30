"""Where binary attachments land on disk so tools can open them.

Attachments arrive as base64 in a chat request, but tools take paths. They are
written under ``<workspace>/_attachments/`` and addressed by content hash, so the
same upload reused across turns keeps a stable path and is never rewritten.

Shared because there are two producers: the executor, materializing a turn's
attachments for the model to reference, and a connector plugin storing an
inbound voice note before handing it to the transcription tool.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

from app import config

log = logging.getLogger(__name__)

SUBDIR = "_attachments"


def store_attachment(raw: bytes, name: str, kind: str = "file") -> str | None:
    """Write ``raw`` under the workspace attachments dir and return its
    workspace-relative path (``_attachments/<name>-<hash><ext>``), or None if it
    could not be written. Both the stem and the extension are sanitized: an
    unsanitized extension carrying a NUL byte made write_bytes raise ValueError,
    which is not an OSError and so aborted the whole turn.
    """
    workdir = config.WORKSPACE_DIR / SUBDIR
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("cannot create attachments dir: %s", e)
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
    return f"{SUBDIR}/{fname}"
