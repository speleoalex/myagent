"""Serve workspace files to the browser: ``GET /api/files/{path}``.

This is the delivery half of the tool -> chat resource channel
(``app/tools/resources.py``): tools drop files under the workspace
(``_resources/`` for anything meant to outlive a turn) and the chat UI points
``<img src>`` / download links here. Living under ``/api/`` is load-bearing
twice over: the API-key middleware guards it (and already accepts ``?api_key=``,
which is what a header-less ``<img>`` request needs), and the service worker
never caches it (``isApi`` in ui/sw.js — anything else same-origin is cached
forever).

Security posture:

- Read-only, GET only, whole workspace. The api_key holder can already read any
  workspace file through an agent with ``file_read``; this route grants no new
  reach, only a direct wire.
- Path traversal: a lexical refusal of ``..``/absolute paths first (the path is
  attacker-influencable), then realpath containment under WORKSPACE_DIR. The
  realpath check is correct HERE, unlike local_read's lexical-only rule: a
  library is assembled from symlinks pointing outside its root, the workspace
  is not.
- ``Content-Security-Policy: sandbox allow-scripts`` on every response: a
  generated HTML page opened from here runs in an OPAQUE origin — no
  localStorage (where the UI keeps the api key), no credentialed same-origin
  calls — while its own scripts still work. Do not remove it.
- Traversal attempts, directories and missing files all answer the same 404:
  nothing to learn from the difference.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import config

router = APIRouter()

# Python 3.10's mimetypes table doesn't know webp — exactly the type ZIM
# article images come back as.
mimetypes.add_type("image/webp", ".webp")

_HEADERS_BASE = {
    "Content-Security-Policy": "sandbox allow-scripts",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/{path:path}")
async def get_file(path: str, download: int = 0):
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=404, detail="Not found")
    root = config.WORKSPACE_DIR.resolve()
    try:
        target = (root / path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    headers = dict(_HEADERS_BASE)
    # _resources/ names are content-addressed (…-<md5>.<ext>), so a long-lived
    # cache can never serve stale bytes; anything else in the workspace is a
    # mutable file an agent may rewrite between two requests.
    headers["Cache-Control"] = (
        "private, max-age=86400, immutable"
        if path.startswith("_resources/") else "no-store"
    )
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if download:
        # FileResponse(filename=...) writes a properly quoted
        # Content-Disposition: attachment for us.
        return FileResponse(target, media_type=mime, headers=headers,
                            filename=target.name)
    return FileResponse(target, media_type=mime, headers=headers)
