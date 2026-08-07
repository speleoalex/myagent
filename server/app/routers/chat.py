from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio
import json

from app.engine import prompts
from app.engine.channel_turn import run_channel_turn
from app.engine.executor import AgentExecutor, Stores
from app.engine.memory_compactor import schedule_compaction
from app.ids import is_valid_id
from app.models import ChatRequest, ChatResponse, ChatMessage
from app.storage.sessions import (memory_context, now_iso, record_turn,
                                  record_user_turn, steps_from)

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


def _record_user(session: dict, req: ChatRequest) -> None:
    record_user_turn(session, req.message,
                     [a.model_dump() for a in req.attachments], req.agent_id)


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
        return await run_channel_turn(req, request.app.state.named_sessions, executor)

    session = session_store.get_current()
    if live.is_active(session["id"]):
        raise HTTPException(409, "A generation is already running for this chat")
    prior = [ChatMessage(**m) for m in session.get("conversation", [])]
    attachments = [a.model_dump() for a in req.attachments] or None

    _record_user(session, req)
    await asyncio.to_thread(session_store.save_current, session)

    response = await executor.run(req.message, prior, attachments,
                                  memory_context=memory_context(session))

    conv = [m.model_dump(exclude_none=True) for m in response.conversation]
    steps = steps_from(response.trace, response.tool_results)
    record_turn(session, steps, response.reply, conv, response.reasoning)
    # persist(), not save_current(): the user may have opened another chat
    # while this run was in flight — never clobber the new current.json.
    await asyncio.to_thread(session_store.persist, session)
    schedule_compaction(executor, session["id"], session_store=session_store,
                        live=live)
    return response


def _make_drive(executor, message, prior, attachments, session, session_store, live=None):
    """Build the async routine that drives one turn: stream events from the
    executor, persist the turn, and emit each event into the LiveRun. On Stop
    (task cancellation) it persists the partial answer instead of losing it."""
    async def drive(run):
        tool_events: list[dict] = []
        reply_text = ""
        reasoning_text = ""
        recorded = False  # the completed turn has been recorded to `session`
        try:
            async for event in executor.run_stream(message, prior, attachments,
                                                   memory_context(session)):
                et = event.get("type")
                # agent_event (a sub-agent's live tokens/tools) is deliberately
                # pass-through: it must reach SSE via run.emit below but never
                # touch reply_text/tool_events — the sub-agent's activity is
                # persisted via the trace's sub_trace, not here.
                if et == "tool_result":
                    tool_events.append(event.get("data", {}))
                elif et == "token":
                    reply_text += event.get("data", "")
                elif et == "reasoning":
                    reasoning_text += event.get("data", "")
                elif et == "clear_tokens":
                    reply_text = ""
                elif et in ("error", "notice"):
                    # A notice ("answering with X because the default is down")
                    # is recorded like an error so it survives a reload: it
                    # explains an answer the user is about to read, and losing
                    # it makes that answer look like it came from the model
                    # they configured.
                    session["messages"].append(
                        {"role": et, "text": str(event.get("data", "")), "ts": now_iso()}
                    )
                    await asyncio.to_thread(session_store.persist, session)
                elif et == "done":
                    data = event.get("data", {})
                    steps = steps_from(data.get("trace"), tool_events)
                    # Record + persist BEFORE emitting 'done' so a client that
                    # reacts to 'done' by reloading reads the fully-recorded
                    # turn. Sessions grow large (full traces) so the write runs
                    # off the event loop, but shielded: a Stop landing in this
                    # window must not lose the completed turn or trigger the
                    # partial-answer path below on top of it.
                    record_turn(session, steps, data.get("reply") or reply_text,
                                data.get("conversation"),
                                data.get("reasoning") or reasoning_text)
                    recorded = True
                    await asyncio.shield(asyncio.to_thread(session_store.persist, session))
                    schedule_compaction(executor, session["id"],
                                        session_store=session_store, live=live)
                run.emit(event)
        except asyncio.CancelledError:
            # Stop pressed mid-generation: keep the partial answer, unless the
            # turn already completed (Stop raced the final write). Persist
            # synchronously — no new awaits in an already-cancelled task.
            if not recorded:
                partial = (f"{reply_text}\n\n{prompts.INTERRUPTED}" if reply_text
                           else prompts.INTERRUPTED)
                record_turn(session, steps_from(None, tool_events), partial, None,
                            reasoning_text)
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

    drive = _make_drive(executor, req.message, prior, attachments, session,
                        session_store, live)
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
    if not is_valid_id(session_id):
        raise HTTPException(400, "invalid session_id")
    return session_id


@router.get("/sessions/{session_id}")
async def get_named_session(session_id: str, request: Request):
    """Fetch a channel-scoped session (used by external connectors)."""
    _valid_session_id(session_id)
    named = request.app.state.named_sessions
    if not named.exists(session_id):
        raise HTTPException(404, "Session not found")
    return await asyncio.to_thread(named.get, session_id)


@router.delete("/sessions/{session_id}")
async def delete_named_session(session_id: str, request: Request):
    """Reset a channel-scoped conversation (e.g. Telegram /reset command).

    Before clearing it, the store archives the conversation into the web UI
    history so past external (bot) chats remain reviewable in the storico —
    the reset "closes" the conversation just like starting a new chat archives
    the current one. Deliberately idempotent (a double /reset is fine)."""
    _valid_session_id(session_id)
    named = request.app.state.named_sessions
    _existed, archived = await named.archive_and_reset(session_id)
    return {"ok": True, "archived": archived}
