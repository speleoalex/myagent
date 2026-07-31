"""Device inbound: the route a satellite pushes speech (or text) to.

Authenticated by the BINDING's own token, not by MYAGENT_API_KEY — the device
holds one shared key that works in both directions (we call its /say with it,
it calls this route with it), and it must not need the admin key of the whole
API. That is why plugin.py registers this router's prefix in
``app.state.self_authenticated_prefixes``: the global key middleware steps
aside, and ``_authorize`` below enforces the per-binding credential instead.

The reply travels back in the SAME response: a voice exchange must not race a
push to the device's /say (which stays reserved for unsolicited messages, i.e.
notify_user).
"""
from __future__ import annotations

import base64
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from myagent_connectors.models import Binding
from myagent_connectors.services import Connectors, services

log = logging.getLogger("connectors.inbound")

router = APIRouter()

# Decoded audio cap — mirrors the Telegram channel's MAX_FILE reasoning: a
# push-to-talk utterance is seconds long, anything bigger is a mistake.
MAX_AUDIO = 15 * 1024 * 1024


class InboundReq(BaseModel):
    """One utterance from the device: text, or audio to transcribe."""
    text: str = ""
    audio_b64: str = ""
    filename: str = "speech.wav"   # extension drives the transcoder
    sender_name: str = ""          # who spoke, when the device knows
    # What is spoken at that device, e.g. "it". Sent per request rather than
    # stored on the binding because the device already holds it (in its own
    # config.json, which is where it is edited) and mirroring it here would be a
    # second copy free to disagree with the one the microphone actually uses.
    # Empty = let whisper detect, which is what happened before this existed.
    language: str = ""


def _authorize(request: Request, binding: Binding) -> None:
    """Bearer <binding.token>, constant-time. 401 without leaking which part
    (unknown id vs bad key) failed — both read the same from outside."""
    auth = request.headers.get("authorization", "")
    candidate = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not binding.token or \
            not hmac.compare_digest(candidate.encode(), binding.token.encode()):
        raise HTTPException(401, "Invalid or missing device key")


@router.post("/{binding_id}")
async def inbound(binding_id: str, req: InboundReq, request: Request):
    svc: Connectors = services(request)
    data = svc.bindings.get(binding_id)
    if data is None:
        # Same body as a failed key on purpose (see _authorize).
        raise HTTPException(401, "Invalid or missing device key")
    binding = Binding(**data)
    _authorize(request, binding)

    connector = svc.manager.get_connector(binding_id)
    if connector is None:
        # Authenticated, so now the answer can be specific: disabled binding
        # or plugin kill switch — either way nothing is listening.
        raise HTTPException(503, "This binding is not running (disabled, or "
                                 "connectors are stopped)")
    if not hasattr(connector, "ask"):
        raise HTTPException(400, f"The '{binding.type}' channel does not "
                                 "accept device inbound")

    text = (req.text or "").strip()
    if req.audio_b64:
        try:
            content = base64.b64decode(req.audio_b64, validate=True)
        except Exception:
            raise HTTPException(400, "audio_b64 is not valid base64")
        if len(content) > MAX_AUDIO:
            raise HTTPException(400, "audio too large")
        try:
            text = await svc.core.transcribe(content, req.filename,
                                             req.language.strip()[:8] or None)
        except Exception as e:
            # The device shows/logs this; keep it readable and specific.
            raise HTTPException(502, f"transcription failed: {e}")
    if not text.strip():
        raise HTTPException(400, "Nothing to say: empty text and no speech "
                                 "recognized")

    try:
        reply = await connector.ask(text, sender_name=req.sender_name)
    except Exception as e:
        log.exception("inbound turn failed (%s): %s", binding_id, e)
        raise HTTPException(502, "Error generating the reply. Try again later.")
    # `text` goes back too: after an audio request it is the transcription,
    # which the device logs so a mis-hearing is diagnosable.
    return {"reply": reply, "text": text}
