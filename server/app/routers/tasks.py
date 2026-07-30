"""Scheduled tasks: the CRUD behind the Tasks page and any external producer.

A "poke from outside" (a webhook, a script, another service) is just a task
with no schedule: POST one with neither ``cron`` nor ``at`` and it is due
immediately, runs once, and disables itself.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.engine import cron as cron_parser
from app.models import Task
from app.routers.crud import get_or_404, require_absent, require_exists

router = APIRouter()


def _tasks(request: Request):
    return request.app.state.tasks


@router.get("")
async def list_tasks(request: Request, agent_id: str = ""):
    """Every task, soonest first; optionally only one agent's."""
    return _tasks(request).list_all(agent_id)


# Fixed routes BEFORE /{task_id}, or "preview" is captured as an id.
@router.get("/preview")
async def preview_cron(cron: str, n: int = 3):
    """The next occurrences of a cron expression, so the form can show what an
    expression actually means. Here and not in JS: one parser, one behaviour."""
    try:
        cron_parser.parse(cron)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"cron": cron,
            "next": [d.isoformat(timespec="minutes")
                     for d in cron_parser.upcoming(cron, max(1, min(n, 10)))]}


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request):
    return get_or_404(_tasks(request).store, task_id, "Task")


@router.post("", status_code=201)
async def create_task(task: Task, request: Request):
    store = _tasks(request)
    require_absent(store.store, task.id, "Task")
    _require_agent(request, task.agent_id)
    return store.save(task)


@router.put("/{task_id}")
async def update_task(task_id: str, task: Task, request: Request):
    store = _tasks(request)
    require_exists(store.store, task_id, "Task")
    _require_agent(request, task.agent_id)
    task.id = task_id                       # the path wins over the body
    # Runtime fields belong to the scheduler, not to the client: keep what is
    # on disk so a stale form cannot erase the last run's outcome.
    stored = store.get(task_id) or {}
    data = task.model_dump()
    for key in ("last_run", "last_result", "last_reply", "created_at"):
        data[key] = stored.get(key, "")
    return store.save(Task(**data))


@router.delete("/{task_id}")
async def delete_task(task_id: str, request: Request):
    if not _tasks(request).delete(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_task(task_id: str, request: Request):
    """Make a task due now. The agent must be live for anything to happen —
    say so instead of leaving the user watching a row that never changes."""
    data = _tasks(request).run_now(task_id)
    if data is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    agent = request.app.state.stores.agents.get(data["agent_id"]) or {}
    return {"ok": True, "next_at": data["next_at"],
            "live": bool(agent.get("enabled", True) and agent.get("live"))}


def _require_agent(request: Request, agent_id: str) -> None:
    get_or_404(request.app.state.stores.agents, agent_id, "Agent")
