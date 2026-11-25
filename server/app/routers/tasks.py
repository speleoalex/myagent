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


def _with_state(request: Request, task: dict) -> dict:
    """A task plus ``running_since``: when the wake running it started, "" when
    idle. Read from the scheduler, never stored. ``POST /run`` answers at once,
    so without this a caller can only poll the task file to see a run happen."""
    since = request.app.state.autonomy.running_since(task.get("agent_id", ""),
                                                     task.get("id", ""))
    return {**task, "running_since": since}


@router.get("")
async def list_tasks(request: Request, agent_id: str = ""):
    """Every task, soonest first; optionally only one agent's."""
    return [_with_state(request, t) for t in _tasks(request).list_all(agent_id)]


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
    return _with_state(request, get_or_404(_tasks(request).store, task_id, "Task"))


@router.get("/{task_id}/runs")
async def task_runs(task_id: str, request: Request):
    """The past runs of this task, newest first: start, duration, outcome,
    tool calls, the limit that cut it short. A wake serves every task due at
    once, so a run listed here may have served others too (``task_ids``)."""
    task = get_or_404(_tasks(request).store, task_id, "Task")
    return request.app.state.autonomy.runs(task["agent_id"], task_id)


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
    for key in ("last_run", "last_result", "last_reply", "last_wake_tasks",
                "created_at"):
        data[key] = stored.get(key, Task.model_fields[key].default)
    return store.save(Task(**data))


@router.delete("/{task_id}")
async def delete_task(task_id: str, request: Request):
    if not _tasks(request).delete(task_id):
        raise HTTPException(404, f"Task not found: {task_id}")
    return {"ok": True}


@router.post("/{task_id}/run")
async def run_task(task_id: str, request: Request):
    """Make a task due now. The agent must be live for anything to happen —
    say so instead of leaving the user watching a row that never changes.

    It also lifts the agent's error backoff: after a failure streak the next
    retry can be half an hour away, and a "run now" that silently waits for it
    is the button lying. Pressing it is the user saying the cause is fixed."""
    data = _tasks(request).run_now(task_id)
    if data is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    await request.app.state.autonomy.resume(data["agent_id"])
    agent = request.app.state.stores.agents.get(data["agent_id"]) or {}
    return {"ok": True, "next_at": data["next_at"],
            "live": bool(agent.get("enabled", True) and agent.get("live"))}


def _require_agent(request: Request, agent_id: str) -> None:
    get_or_404(request.app.state.stores.agents, agent_id, "Agent")
