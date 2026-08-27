"""``/api/index`` — see and steer the semantic index.

Without this the indexer is invisible: it competes with the chat model for the
same backend, so "why is the assistant slow right now" needs an answer, and
there has to be a way to stop it. Read-mostly; the two writes are pause and
resume.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.engine import embedding

router = APIRouter()


def _service(request: Request):
    svc = getattr(request.app.state, "index", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Index service not running")
    return svc


@router.get("/status")
async def status(request: Request):
    """Every root the service knows about, plus WHY nothing is happening.

    ``configured`` is the important field for the UI: with no embedding model
    chosen there is nothing wrong, semantic search is simply off — a state that
    must read as "not enabled", never as "broken".
    """
    svc = _service(request)
    model_id = embedding.embedding_model_id()
    reason, backend = "", ""
    if model_id:
        raw = request.app.state.stores.models.get(model_id) or {}
        if model_id == embedding.LOCAL_ID and not raw:
            backend = "local"
            reason = "" if embedding.local_available() else (
                "the optional `fastembed` package is not installed")
        else:
            backend = "endpoint"
            reason = embedding.rejection_reason(raw)
    return {
        "configured": bool(model_id) and not reason,
        "model_id": model_id,
        # Which KIND of embedder, so the UI can say "in this process" instead
        # of implying there is a server involved — and so "why is indexing
        # using my CPU" has an answer that names the reason.
        "backend": backend,
        # Advertised even when unused: the settings page offers the in-process
        # option only when it would actually work, and it asks this route.
        "local_available": embedding.local_available(),
        "problem": reason,
        "roots": svc.status(),
    }


@router.post("/{key}/stop")
async def stop(key: str, request: Request):
    """Pause a root. Sticky on purpose: without the marker the next search
    would queue it again within seconds."""
    if not await _service(request).stop(key):
        raise HTTPException(status_code=500, detail="Could not pause the index")
    return {"ok": True}


@router.post("/{key}/start")
async def start(key: str, request: Request):
    if not await _service(request).resume(key):
        raise HTTPException(status_code=500, detail="Could not resume the index")
    return {"ok": True}
