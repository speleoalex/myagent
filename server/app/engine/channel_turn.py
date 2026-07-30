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
from app.engine.memory_compactor import schedule_compaction
from app.storage.sessions import (memory_context, record_turn, record_user_turn,
                                  steps_from, title_from)


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
        attachments = [a.model_dump() for a in req.attachments] or None

        response = await executor.run(req.message, prior, attachments,
                                      memory_context=memory_context(session))

        conv = [m.model_dump(exclude_none=True) for m in response.conversation]
        record_user_turn(session, req.message,
                         [a.model_dump() for a in req.attachments], req.agent_id)
        record_turn(session, steps_from(response.trace, response.tool_results),
                    response.reply, conv)
        await asyncio.to_thread(named.save_rotating, sid, session)
    schedule_compaction(executor, sid, named=named)
    return response
