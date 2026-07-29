"""Runtime control of autonomous (live) agents.

The on/off switch is NOT here: it is the ``live`` field of the agent (PUT
/api/agents/{id}). These endpoints expose the scheduler's runtime state and
the manual levers: wake now, stop the in-flight wake, resume after auto-pause.
"""
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _autonomy(request: Request):
    return request.app.state.autonomy


@router.get("/status")
async def autonomy_status(request: Request):
    """Per-agent scheduler state: {agent_id: {state: idle|running|rate_limited|
    error|paused|disabled, last_wake, last_result, next_wake, ...}}."""
    return _autonomy(request).status()


@router.post("/{agent_id}/wake")
async def wake_agent(agent_id: str, request: Request):
    """Trigger a wake immediately (bypasses schedule, rate limit and pause).
    Works for any enabled agent — the main testing lever."""
    if request.app.state.stores.agents.get(agent_id) is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    started = await _autonomy(request).wake_now(agent_id)
    if not started:
        raise HTTPException(409, "A wake is already running for this agent "
                                 "(or the agent is disabled)")
    return {"ok": True}


@router.post("/{agent_id}/stop")
async def stop_agent_wake(agent_id: str, request: Request):
    """Cancel the wake currently in flight (the partial turn is persisted)."""
    stopped = await _autonomy(request).stop(agent_id)
    return {"stopped": stopped}


@router.post("/{agent_id}/resume")
async def resume_agent(agent_id: str, request: Request):
    """Clear the auto-pause after repeated errors (re-saving the agent's
    config does the same)."""
    if request.app.state.stores.agents.get(agent_id) is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    await _autonomy(request).resume(agent_id)
    return {"ok": True}
