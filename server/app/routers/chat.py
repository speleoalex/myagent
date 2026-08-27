from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio
import json
import logging

from app.engine import prompts
from app.engine.agent_router import AUTO, mark_foreign, resolve_auto
from app.engine.channel_turn import run_channel_turn
from app.engine.executor import AgentExecutor, Stores
from app.engine.memory_compactor import cancel_compaction, schedule_compaction
from app.ids import is_valid_id
from app.models import ChatRequest, ChatResponse, ChatMessage
from app.storage.sessions import (delegation_history, memory_context, now_iso,
                                  record_turn, record_user_turn, steps_from,
                                  tool_history)

log = logging.getLogger(__name__)

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


def _remember_model_override(session: dict, req: ChatRequest) -> None:
    """Keep the chat's model pick on the CURRENT session, for the UI only.

    The override itself travels in every request — this stored copy is what
    lets the selector survive a page reload without pretending to be a
    setting: a new chat starts from a fresh session and is back on the
    default, and nothing outside the web current chat ever reads it."""
    if req.model_override:
        session["model_override"] = req.model_override
    else:
        session.pop("model_override", None)


def _remember_agent_auto(session: dict, on: bool) -> None:
    """Keep the chat's Auto mode on the CURRENT session, for the UI only —
    the exact mirror of _remember_model_override. It needs its own key because
    record_user_turn overwrites session["agent_id"] with the RESOLVED agent on
    every turn (which is also what makes the last-used fallback work), so the
    combo could never read "auto" back from there."""
    if on:
        session["agent_auto"] = True
    else:
        session.pop("agent_auto", None)


async def _route_auto(req: ChatRequest, request: Request, session: dict,
                      named: bool = False) -> str | None:
    """Resolve agent_id "auto" → a concrete id, IN PLACE on ``req``; returns
    the fallback note (or None). The resolution itself is agent_router.
    resolve_auto, shared with the connectors plugin — this wrapper owns the
    web session's Auto flag (web only: a channel session has no selector to
    restore), ``req.agent_auto`` and the HTTP error shape."""
    stores: Stores = request.app.state.stores
    was_auto = req.agent_id == AUTO and stores.agents.get(AUTO) is None
    if not named:
        _remember_agent_auto(session, was_auto)
    try:
        req.agent_id, note = await resolve_auto(
            req.agent_id, req.message, session, stores,
            request.app.state.tool_registry, model_override=req.model_override)
    except ValueError as e:
        raise HTTPException(404, str(e))
    req.agent_auto = was_auto
    return note


def _mark_foreign(prior: list[ChatMessage], session: dict, agent_id: str) -> None:
    """Auto mode only: flag the history turns another agent answered (see
    agent_router.mark_foreign). Outside Auto mode nothing is flagged — a
    manual agent switch mid-chat keeps its long-standing verbatim context
    ("traduci quello sopra" needs it)."""
    if session.get("agent_auto"):
        mark_foreign(prior, session, agent_id)


@router.post("")
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    stores: Stores = request.app.state.stores
    tool_registry = request.app.state.tool_registry
    session_store = request.app.state.sessions
    live = request.app.state.live

    # Auto-routing: the web chat owns the Auto flag on its current session; a
    # channel session (an external connector on this endpoint) resolves the
    # same way against its own history, no flag. The live check moves up so a
    # busy chat never pays a classification call.
    session = None
    routed_note = None
    if not req.session_id:
        session = session_store.get_current()
        if live.is_active(session["id"]):
            raise HTTPException(409, "A generation is already running for this chat")
        routed_note = await _route_auto(req, request, session)
        if live.is_active(session["id"]):  # the classification took time
            raise HTTPException(409, "A generation is already running for this chat")
    elif req.agent_id == AUTO:
        named_session = await asyncio.to_thread(
            request.app.state.named_sessions.get, req.session_id, req.agent_id)
        note = await _route_auto(req, request, named_session, named=True)
        if note:
            # run() drains no notice event; the log is where this path declares.
            log.info("%s: %s", req.session_id, note)

    try:
        executor = await AgentExecutor.create_for_agent(
            req.agent_id, tool_registry, stores,
            model_override=req.model_override)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if routed_note:
        executor.notice = (f"{executor.notice} {routed_note}"
                           if executor.notice else routed_note)

    # External connectors address their own persistent, per-channel session.
    if req.session_id:
        return await run_channel_turn(req, request.app.state.named_sessions, executor)

    # A compaction still running for this chat would compete with THIS turn for
    # the model, and phase C would discard its result anyway.
    cancel_compaction(session["id"])
    _remember_model_override(session, req)
    prior = [ChatMessage(**m) for m in session.get("conversation", [])]
    _mark_foreign(prior, session, req.agent_id)
    attachments = [a.model_dump() for a in req.attachments] or None

    _record_user(session, req)
    await asyncio.to_thread(session_store.save_current, session)

    response = await executor.run(req.message, prior, attachments,
                                  memory_context=memory_context(session),
                                  delegations=delegation_history(session),
                                  tool_results=tool_history(session))

    conv = [m.model_dump(exclude_none=True) for m in response.conversation]
    steps = steps_from(response.trace, response.tool_results)
    record_turn(session, steps, response.reply, conv, response.reasoning)
    # persist(), not save_current(): the user may have opened another chat
    # while this run was in flight — never clobber the new current.json.
    await asyncio.to_thread(session_store.persist, session)
    schedule_compaction(executor, session["id"], session_store=session_store,
                        live=live)
    return response


def _make_drive(executor, message, prior, attachments, session, session_store, live=None,
                announce_agent: str | None = None):
    """Build the async routine that drives one turn: stream events from the
    executor, persist the turn, and emit each event into the LiveRun. On Stop
    (task cancellation) it persists the partial answer instead of losing it.

    ``announce_agent`` (Auto mode): the RESOLVED agent id, emitted as the very
    first event so the UI can label the bubble before the first token — the
    ``done`` trace carries it too, but only at the end of the turn. Not
    persisted: the reload path reads the id from the recorded user turn."""
    async def drive(run):
        tool_events: list[dict] = []
        reply_text = ""
        reasoning_text = ""
        recorded = False  # the completed turn has been recorded to `session`
        if announce_agent:
            run.emit({"type": "agent", "data": announce_agent})
        try:
            async for event in executor.run_stream(
                    message, prior, attachments, memory_context(session),
                    delegations=delegation_history(session),
                    tool_results=tool_history(session)):
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

    session = session_store.get_current()
    sid = session["id"]

    # If a generation is already running for this chat, just attach to it
    # (defensive: the UI shows Stop, not Send, while a run is active).
    # Checked BEFORE auto-routing, so a re-attach never pays an LLM call.
    if live.is_active(sid):
        return _sse(live.get(sid).subscribe())

    routed_note = await _route_auto(req, request, session)
    # Re-checked: the classification above can take seconds, and live.start()
    # overwrites silently — a second Send in that window must attach, not
    # start a twin run on the same session.
    if live.is_active(sid):
        return _sse(live.get(sid).subscribe())
    try:
        executor = await AgentExecutor.create_for_agent(
            req.agent_id, tool_registry, stores,
            model_override=req.model_override)
    except ValueError as e:
        raise HTTPException(404, str(e))
    if routed_note:
        # Rides the existing notice machinery: emitted once at depth 0,
        # recorded in the session by _make_drive, drawn above the bubble.
        executor.notice = (f"{executor.notice} {routed_note}"
                           if executor.notice else routed_note)

    cancel_compaction(sid)  # same reason as in chat() above
    _remember_model_override(session, req)

    prior = [ChatMessage(**m) for m in session.get("conversation", [])]
    _mark_foreign(prior, session, req.agent_id)
    attachments = [a.model_dump() for a in req.attachments] or None

    _record_user(session, req)
    await asyncio.to_thread(session_store.save_current, session)

    drive = _make_drive(executor, req.message, prior, attachments, session,
                        session_store, live,
                        announce_agent=req.agent_id if req.agent_auto else None)
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
