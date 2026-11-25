#!/usr/bin/env python3
"""A recurring task's history, and whether it is running right now.

A task kept only its LAST outcome, so "is this daily job reliable?" had no
answer: "acted" today hid three timeouts last week. And POST /tasks/{id}/run
answered "ok" at once with no way to follow the run — on the Orin the only way
to see it finish was polling the task's JSON file.

Pinned here, with the real AutonomyService bookkeeping, TaskStore and tasks
router (temporary MYAGENT_HOME, no model):

  * every finished wake is logged per agent, newest first, capped at
    RUN_LOG_MAX: start, duration, outcome, tool calls, the limit that cut it
    short, manual or not, the reply or the error;
  * GET /tasks/{id}/runs lists only the runs that served that task;
  * running_since is set while a wake runs, for exactly its tasks, and cleared
    when it ends — GET /tasks and GET /tasks/{id} carry it, nothing stores it;
  * deleting the agent drops its run log with the rest of its state.

Run:  server/.venv/bin/python tests/test_task_runs.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-taskruns-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.engine import autonomy  # noqa: E402
from app.engine.autonomy import AutonomyService  # noqa: E402
from app.engine.executor import Stores  # noqa: E402
from app.models import Agent, AutonomousConfig  # noqa: E402
from app.routers import tasks as tasks_router  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.storage.tasks import TaskStore  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


cfg_dir = Path(_TMP) / "config"
agents = JsonStore(cfg_dir / "agents")
agents.save("cv", Agent(id="cv", name="Cerca lavoro", model_id="m", live=True).model_dump())
tasks = TaskStore(cfg_dir / "tasks")
service = AutonomyService(stores=Stores(agents=agents, models=JsonStore(cfg_dir / "models")),
                          tool_registry=None, named_sessions=None, live=None,
                          tasks=tasks, base_dir=Path(_TMP) / "autonomy")
app = FastAPI()
app.state.tasks = tasks
app.state.autonomy = service
app.state.stores = service.stores
app.include_router(tasks_router.router, prefix="/api/tasks")
client = TestClient(app)
agent = Agent(**agents.get("cv"))


def task(tid: str) -> None:
    tasks.store.save(tid, dict(id=tid, agent_id="cv", cron="30 7 * * *", prompt=tid,
                               next_at="2026-11-25T07:30:00", enabled=True))


def finish(task_ids, result, *, timed_out=False, manual=False, started="2026-11-25T07:30:00"):
    st = service._state("cv")
    st["last_wake"] = started
    base = {"reply": "", "tool_calls": 0, "error": None, "delivery_error": "", "budget_hit": ""}
    service._finish_wake(agent, AutonomousConfig(), [tasks.get(t) for t in task_ids],
                         {**base, **result}, timed_out=timed_out, stopped=False, manual=manual)


task("task-search")
task("task-weather")

print("run log")
finish(["task-search"], {"reply": "10 ricerche, report scritto", "tool_calls": 45,
                         "budget_hit": "max_tool_calls"})
finish(["task-search", "task-weather"], {"error": "connection refused"}, manual=True)
finish(["task-weather"], {"reply": "meteo inviato", "tool_calls": 2})
runs = service.runs("cv")
check([r["result"] for r in runs] == ["acted", "error", "acted"], "newest first, every wake")
first = runs[-1]
check(first["tool_calls"] == 45 and first["budget_hit"] == "max_tool_calls",
      "tool calls and the limit that cut the run short are kept")
check(first["reply"].startswith("10 ricerche") and first["duration_s"] >= 0,
      "reply and duration are kept")
check(runs[1]["manual"] is True and runs[1]["error"] == "connection refused",
      "a failed manual run keeps its error and says it was manual")
check(runs[1]["task_ids"] == ["task-search", "task-weather"], "the tasks a wake served")

print("per task, through the API")
listed = client.get("/api/tasks/task-search/runs").json()
check([r["result"] for r in listed] == ["error", "acted"],
      "only the runs that served this task")
check(client.get("/api/tasks/nope/runs").status_code == 404, "an unknown task is a 404")

print("cap")
for _ in range(autonomy.RUN_LOG_MAX + 5):
    finish(["task-weather"], {"reply": "ok", "tool_calls": 1})
check(len(service.runs("cv")) == autonomy.RUN_LOG_MAX, f"capped at {autonomy.RUN_LOG_MAX}")

print("running now")
service._running["cv"] = {"started": "2026-11-25T07:30:00", "task_ids": ["task-search"]}
rows = {t["id"]: t for t in client.get("/api/tasks").json()}
check(rows["task-search"]["running_since"] == "2026-11-25T07:30:00",
      "the list says which task is running, and since when")
check(rows["task-weather"]["running_since"] == "", "…and only that one")
check(client.get("/api/tasks/task-search").json()["running_since"] == "2026-11-25T07:30:00",
      "the single-task read says it too")
check("running_since" not in tasks.get("task-search"), "it is never stored")
service._running.pop("cv")
check(client.get("/api/tasks/task-search").json()["running_since"] == "", "cleared when done")

print("drop")
service.drop_agent("cv")
check(service.runs("cv") == [], "deleting the agent drops its run log")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
