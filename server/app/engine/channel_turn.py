"""One agent turn against a channel-scoped named session.

Extracted from the chat router because it has two callers that must not drift:
the HTTP endpoint (`POST /api/chat` with a session_id) and any in-process
connector plugin driving a messaging channel. The per-session lock lives here,
so both callers get the serialization for free instead of each remembering to
take it.
"""
from __future__ import annotations

import asyncio

from app.models import ChatRequest, ChatResponse, ChatMessage
from app.engine.agent_router import mark_foreign
from app.engine.memory_compactor import cancel_compaction, schedule_compaction
from app.storage.sessions import (delegation_history, memory_context, record_turn,
                                  record_user_turn, steps_from, title_from)


async def run_channel_turn(req: ChatRequest, named, executor) -> ChatResponse:
    """Run one turn against a channel-scoped named session (external
    connectors). Serialized per session_id so concurrent messages from the same
    external chat can't interleave their conversation writes.

    Turns are recorded with the same storage helpers as web chats
    (record_user_turn / record_turn), so channel sessions share the web
    session format — title, attachments and the recursive tool trace included."""
    sid = req.session_id
    async with named.lock(sid):
        session = await asyncio.to_thread(named.get, sid, req.agent_id)
        # A compaction still running for this session would compete with THIS
        # turn for the model, and would be discarded anyway.
        cancel_compaction(sid)
        # Provenance: upgrade sessions created before these fields existed.
        session.setdefault("channel", sid)
        if req.source:
            session["source"] = req.source
        if not session.get("title"):
            # Legacy light-format sessions carry no title: backfill it from
            # the conversation's first user message, so the turn recorder
            # doesn't title a years-old chat with whatever message arrives today.
            first = next((m.get("text") or "" for m in session.get("messages", [])
                          if m.get("role") == "user" and m.get("text")), "")
            if first:
                session["title"] = title_from(first)
        prior = [ChatMessage(**m) for m in session.get("conversation", [])]
        # A binding on "auto" routes per message, so this session's history
        # may hold other agents' turns: quoted, not imitated. Gated on the
        # request flag like the web chat's Auto mode — an admin who re-points
        # a binding to a fixed agent keeps the verbatim history.
        if req.agent_auto:
            session["agent_auto"] = True  # the web UI's "via <agent>" labels
            mark_foreign(prior, session, req.agent_id)
        attachments = [a.model_dump() for a in req.attachments] or None

        # Provenance the MODEL sees: who wrote this, on which channel. Prefixed
        # to the message (so a group chat, where the sender changes at every
        # turn, stays attributable in the conversation history) and kept out of
        # the display record below — the UI shows what the person typed.
        # No English words in the wrapper: a small model reads the FIRST line
        # of the user turn as the user's language, and "Message from" was
        # enough to flip whole replies to English (observed on Qwen3-VL-4B).
        model_message = req.message
        if req.sender:
            model_message = f"[{req.sender}]\n{req.message}"

        response = await executor.run(model_message, prior, attachments,
                                      memory_context=memory_context(session),
                                      transcribed=req.transcribed,
                                      delegations=delegation_history(session))

        conv = [m.model_dump(exclude_none=True) for m in response.conversation]
        record_user_turn(session, req.message,
                         [a.model_dump() for a in req.attachments], req.agent_id)
        record_turn(session, steps_from(response.trace, response.tool_results),
                    response.reply, conv, response.reasoning)
        await asyncio.to_thread(named.save_rotating, sid, session)
    schedule_compaction(executor, sid, named=named)
    return response
