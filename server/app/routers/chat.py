from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from datetime import datetime
import asyncio
import json

from app.models import ChatRequest, ChatResponse, ChatMessage, _VALID_ID
from app.engine.executor import AgentExecutor, Stores

router = APIRouter()


def _sse(agen):
    """Wrap an async generator of event dicts into an SSE StreamingResponse."""
    async def gen():
        async for event in agen:
            yield f"data: {json.dumps(event, default=str)}\n\n"
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _title_from(message: str, attachments) -> str:
    t = (message or "").strip().replace("\n", " ")
    if t:
        return t[:60] + ("…" if len(t) > 60 else "")
    return "(attachment)" if attachments else "New chat"


def _record_user(session: dict, req: ChatRequest) -> None:
    """Append the user turn to the session and set the title on first message."""
    session.setdefault("messages", []).append({
        "role": "user",
        "text": req.message,
        "attachments": [a.model_dump() for a in req.attachments],
        "agent_id": req.agent_id,
        "ts": _now(),
    })
    session["agent_id"] = req.agent_id
    if not session.get("title"):
        session["title"] = _title_from(req.message, req.attachments)


def _tool_message_from_step(step: dict) -> dict:
    """Turn one executor trace step into a stored 'tool' message. Keeps the full
    result and — for call_agent — the nested sub_trace, so the archived session
    holds the complete recursive flow (sub-agent calls, results and tools)."""
    msg = {
        "role": "tool",
        "tool": step.get("tool"),
        "arguments": step.get("arguments"),
        "result_preview": step.get("result_preview"),
        "result": step.get("result"),
        "ts": step.get("ts") or _now(),
    }
    if step.get("sub_trace"):
        msg["sub_trace"] = step["sub_trace"]
    return msg


def _steps_from(trace, tool_events: list[dict] | None) -> list[dict]:
    """Prefer the rich recursive trace; fall back to flat tool summaries
    (older path / no trace available)."""
    if trace and trace.get("steps"):
        return trace["steps"]
    return [
        {
            "tool": t.get("tool"),
            "arguments": t.get("arguments"),
            "result_preview": t.get("result_preview"),
            "result": t.get("result") or t.get("result_preview"),
        }
        for t in (tool_events or [])
    ]


def _record_turn(session: dict, steps: list[dict], reply: str, conversation) -> None:
    """Append the tool calls (recursive trace) and assistant reply, and update
    the compact LLM history."""
    for step in steps:
        session["messages"].append(_tool_message_from_step(step))
    session["messages"].append({"role": "assistant", "text": reply, "ts": _now()})
    if conversation is not None:
        session["conversation"] = [
            m for m in conversation if (m.get("role") if isinstance(m, dict) else m.role) != "system"
        ]


def _record_turn_light(session: dict, req: ChatRequest, reply: str, conversation) -> None:
    """Record a channel-session turn: keep only the user/assistant text log and
    the compact LLM history — no full recursive tool trace (those grow large
    and are only useful to the web UI's inspector)."""
    session.setdefault("messages", []).append(
        {"role": "user", "text": req.message, "ts": _now()}
    )
    session["agent_id"] = req.agent_id
    session["messages"].append({"role": "assistant", "text": reply, "ts": _now()})
    if conversation is not None:
        session["conversation"] = [
            m for m in conversation
            if (m.get("role") if isinstance(m, dict) else m.role) != "system"
        ]


async def _chat_named(req: ChatRequest, request: Request, executor) -> ChatResponse:
    """Run one turn against a channel-scoped named session (external
    connectors). Serialized per session_id so concurrent messages from the same
    external chat can't interleave their conversation writes."""
    named = request.app.state.named_sessions
    sid = req.session_id
    async with named.lock(sid):
        session = named.get(sid, agent_id=req.agent_id)
        prior = [ChatMessage(**m) for m in session.get("conversation", [])]
        attachments = [a.model_dump() for a in req.attachments] or None

        response = await executor.run(req.message, prior, attachments)

        conv = [m.model_dump(exclude_none=True) for m in response.conversation]
        _record_turn_light(session, req, response.reply, conv)
        await asyncio.to_thread(named.save, sid, session)
    return response


@router.post("")
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    stores: Stores = request.app.state.stores
    tool_registry = request.app.state.tool_registry
    session_store = request.app.state.sessions
    live = request.app.state.live

    try:
        executor = await AgentExecutor.create_for_agent(req.agent_id, tool_registry, stores)
    except ValueError as e:
        raise HTTPException(404, str(e))

    # External connectors address their own persistent, per-channel session.
    if req.session_id:
        return await _chat_named(req, request, executor)

    session = session_store.get_current()
    if live.is_active(session["id"]):
        raise HTTPException(409, "A generation is already running for this chat")
    prior = [ChatMessage(**m) for m in session.get("conversation", [])]
    attachments = [a.model_dump() for a in req.attachments] or None

    _record_user(session, req)
    await asyncio.to_thread(session_store.save_current, session)

    response = await executor.run(req.message, prior, attachments)

    conv = [m.model_dump(exclude_none=True) for m in response.conversation]
    steps = _steps_from(response.trace, response.tool_results)
    _record_turn(session, steps, response.reply, conv)
    # persist(), not save_current(): the user may have opened another chat
    # while this run was in flight — never clobber the new current.json.
    await asyncio.to_thread(session_store.persist, session)
    return response


def _make_drive(executor, message, prior, attachments, session, session_store):
    """Build the async routine that drives one turn: stream events from the
    executor, persist the turn, and emit each event into the LiveRun. On Stop
    (task cancellation) it persists the partial answer instead of losing it."""
    async def drive(run):
        tool_events: list[dict] = []
        reply_text = ""
        recorded = False  # the completed turn has been recorded to `session`
        try:
            async for event in executor.run_stream(message, prior, attachments):
                et = event.get("type")
                if et == "tool_result":
                    tool_events.append(event.get("data", {}))
                elif et == "token":
                    reply_text += event.get("data", "")
                elif et == "error":
                    session["messages"].append(
                        {"role": "error", "text": str(event.get("data", "")), "ts": _now()}
                    )
                    await asyncio.to_thread(session_store.persist, session)
                elif et == "done":
                    data = event.get("data", {})
                    steps = _steps_from(data.get("trace"), tool_events)
                    # Record + persist BEFORE emitting 'done' so a client that
                    # reacts to 'done' by reloading reads the fully-recorded
                    # turn. Sessions grow large (full traces) so the write runs
                    # off the event loop, but shielded: a Stop landing in this
                    # window must not lose the completed turn or trigger the
                    # partial-answer path below on top of it.
                    _record_turn(session, steps, data.get("reply") or reply_text,
                                 data.get("conversation"))
                    recorded = True
                    await asyncio.shield(asyncio.to_thread(session_store.persist, session))
                run.emit(event)
        except asyncio.CancelledError:
            # Stop pressed mid-generation: keep the partial answer, unless the
            # turn already completed (Stop raced the final write). Persist
            # synchronously — no new awaits in an already-cancelled task.
            if not recorded:
                partial = reply_text + ("\n\n_[interrotto]_" if reply_text else "_[interrotto]_")
                _record_turn(session, _steps_from(None, tool_events), partial, None)
                session_store.persist(session)
            raise
    return drive


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request):
    stores: Stores = request.app.state.stores
    tool_registry = request.app.state.tool_registry
    session_store = request.app.state.sessions
    live = request.app.state.live

    try:
        executor = await AgentExecutor.create_for_agent(req.agent_id, tool_registry, stores)
    except ValueError as e:
        raise HTTPException(404, str(e))

    session = session_store.get_current()
    sid = session["id"]

    # If a generation is already running for this chat, just attach to it
    # (defensive: the UI shows Stop, not Send, while a run is active).
    if live.is_active(sid):
        return _sse(live.get(sid).subscribe())

    prior = [ChatMessage(**m) for m in session.get("conversation", [])]
    attachments = [a.model_dump() for a in req.attachments] or None

    _record_user(session, req)
    await asyncio.to_thread(session_store.save_current, session)

    drive = _make_drive(executor, req.message, prior, attachments, session, session_store)
    run = live.start(sid, drive)
    return _sse(run.subscribe())


@router.get("/stream/attach")
async def chat_attach(request: Request):
    """Re-attach to the live generation of the current chat (replay + tail).
    Used when the user returns to a chat whose response is still streaming."""
    session_store = request.app.state.sessions
    live = request.app.state.live
    session = session_store.get_current()
    run = live.get(session["id"])
    if run is None:
        async def idle():
            yield {"type": "idle"}
        return _sse(idle())
    return _sse(run.subscribe())


@router.post("/stop")
async def chat_stop(request: Request):
    """Stop the generation in progress for the current chat."""
    session_store = request.app.state.sessions
    live = request.app.state.live
    session = session_store.get_current()
    stopped = await live.stop(session["id"])
    return {"stopped": stopped}


@router.get("/live")
async def chat_live(request: Request):
    """Whether a generation is currently running for the current chat."""
    session_store = request.app.state.sessions
    live = request.app.state.live
    session = session_store.get_current()
    return {"active": live.is_active(session["id"])}


def _valid_session_id(session_id: str) -> str:
    """Path segments become filenames: reject anything outside the safe id
    charset (mirrors ChatRequest.session_id validation)."""
    if not _VALID_ID.match(session_id or "") or ".." in session_id:
        raise HTTPException(400, "invalid session_id")
    return session_id


@router.get("/sessions/{session_id}")
async def get_named_session(session_id: str, request: Request):
    """Fetch a channel-scoped session (used by external connectors)."""
    _valid_session_id(session_id)
    named = request.app.state.named_sessions
    if not named._path(session_id).exists():
        raise HTTPException(404, "Session not found")
    return named.get(session_id)


@router.delete("/sessions/{session_id}")
async def delete_named_session(session_id: str, request: Request):
    """Reset a channel-scoped conversation (e.g. Telegram /reset command).

    Before clearing it, archive the conversation into the web UI history so
    past external (bot) chats remain reviewable in the storico — the reset
    "closes" the conversation just like starting a new chat archives the
    current one. Serialized on the session lock so it can't race an in-flight
    turn for the same channel."""
    _valid_session_id(session_id)
    named = request.app.state.named_sessions
    session_store = request.app.state.sessions
    archived = None
    async with named.lock(session_id):
        if named._path(session_id).exists():
            session = named.get(session_id)
            archived = await asyncio.to_thread(
                session_store.archive_session, session, "🤖 "
            )
        named.delete(session_id)
    return {"ok": True, "archived": archived}
