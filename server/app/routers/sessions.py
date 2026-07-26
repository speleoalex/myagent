from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class NewChatReq(BaseModel):
    agent_id: str = ""


@router.get("")
async def list_sessions(request: Request):
    """List archived chats (newest first)."""
    return request.app.state.sessions.list_history()


@router.get("/current")
async def get_current(request: Request):
    """The active chat."""
    return request.app.state.sessions.get_current()


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
    if s is None:
        raise HTTPException(404, "Session not found")
    return s


@router.delete("/{session_id}")
async def delete_session(session_id: str, request: Request):
    if not request.app.state.sessions.delete(session_id):
        raise HTTPException(404, "Session not found")
    return {"ok": True}
