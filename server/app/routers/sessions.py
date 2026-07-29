import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.engine.executor import AgentExecutor

router = APIRouter()


class NewChatReq(BaseModel):
    agent_id: str = ""


class RewindReq(BaseModel):
    # 0-based ordinal of the user message to rewind to; -1 = the last one.
    user_turn: int = -1


def _truncate_conversation(conversation: list[dict], keep_turns: int) -> list[dict]:
    """Cut the compact LLM history right before its ``keep_turns``-th real user
    turn, so a re-sent turn cannot see the answers being discarded.

    The stored history also holds tool scaffolding (results delivered as a user
    turn, '(used tool: ...)' markers, native tool messages), which are not turns
    — hence the shared predicate. When the history holds fewer real turns than
    the message log there is nothing to cut: the discarded turn never made it in
    (a Stop records the message log but leaves the history untouched)."""
    seen = 0
    for i, m in enumerate(conversation):
        if m.get("role") != "user":
            continue
        if AgentExecutor.is_scaffolding_message("user", m.get("content"), m.get("tool_calls")):
            continue
        if seen == keep_turns:
            return conversation[:i]
        seen += 1
    return conversation


@router.get("")
async def list_sessions(request: Request):
    """List archived chats (newest first), plus the live agents' autonomous
    sessions — they never pass through "new chat", so they would otherwise be
    invisible until rotated into the history."""
    out = request.app.state.sessions.list_history()
    out.extend(request.app.state.named_sessions.list_summaries(prefix="autonomous_"))
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out


@router.get("/current")
async def get_current(request: Request):
    """The active chat."""
    return request.app.state.sessions.get_current()


@router.post("/current/rewind")
async def rewind_current(req: RewindReq, request: Request):
    """Drop a user turn and everything after it from the active chat, returning
    the removed message so the caller can send it again (regenerate) or send an
    edited version (edit prompt). The re-send goes through the normal chat
    endpoints, so this only rewinds state — it never runs the agent."""
    store = request.app.state.sessions
    session = store.get_current()
    if request.app.state.live.is_active(session["id"]):
        raise HTTPException(409, "A generation is already running for this chat")

    messages = session.get("messages", [])
    user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_idx:
        raise HTTPException(400, "This chat has no user message to rewind to")
    turn = req.user_turn if req.user_turn >= 0 else len(user_idx) - 1
    if turn >= len(user_idx):
        raise HTTPException(404, f"No user turn {turn} in this chat")

    cut = user_idx[turn]
    removed = messages[cut]
    session["messages"] = messages[:cut]
    # Memory-enabled agents: the oldest turns may have been archived to deep
    # memory (spliced out of the compact history), so the ordinal in the
    # message log is offset by archived_user_turns. Rewinding to BEFORE the
    # archived boundary empties the history and this session's summary context
    # — the deep chunks stay: memory is not the transcript.
    mem = session.get("memory") or {}
    conv_turn = turn - mem.get("archived_user_turns", 0)
    if conv_turn >= 0:
        session["conversation"] = _truncate_conversation(
            session.get("conversation", []), conv_turn)
    else:
        session["conversation"] = []
        mem["context"] = []
        mem["archived_user_turns"] = turn
        session["memory"] = mem
    if turn == 0:
        session["title"] = ""  # nothing left to title the chat: the re-send will
    # Sessions carry full recursive traces and grow large: write off the loop.
    await asyncio.to_thread(store.save_current, session)
    return {
        "ok": True,
        "message": {
            "text": removed.get("text") or "",
            "attachments": removed.get("attachments") or [],
        },
        "session": session,
    }


@router.post("/new")
async def new_chat(req: NewChatReq, request: Request):
    """Archive the current chat and start a fresh one."""
    return request.app.state.sessions.new_chat(req.agent_id)


@router.post("/{session_id}/resume")
async def resume_session(session_id: str, request: Request):
    """Reopen an archived chat as the current one (keeps its original agent)."""
    s = request.app.state.sessions.resume(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    return s


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    s = request.app.state.sessions.get(session_id)
    if s is None and session_id.startswith("autonomous_"):
        named = request.app.state.named_sessions
        if named._path(session_id).exists():
            s = await asyncio.to_thread(named.get, session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    return s


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request):
    if request.app.state.sessions.delete(session_id):
        return {"ok": True}
    if session_id.startswith("autonomous_"):
        # An autonomous session is a living channel file: deleting it means
        # archiving the log into the regular history first (same flow as a
        # connector /reset), serialized on the session lock so it can't race
        # an in-flight wake.
        named = request.app.state.named_sessions
        session_store = request.app.state.sessions
        async with named.lock(session_id):
            if named._path(session_id).exists():
                session = await asyncio.to_thread(named.get, session_id)
                archived = await asyncio.to_thread(
                    session_store.archive_session, session)
                named.delete(session_id)
                return {"ok": True, "archived": archived}
    raise HTTPException(404, "Session not found")
