#!/usr/bin/env python3
"""A recurring task reported its OWN previous run as this run's delivery.

Orin, 2026-09-17, Qwen3-VL-4B. The scheduled task "send Alessandro a picture
of a heart" fired at 17:01. The model opened the wake by listing its tasks,
read the line the MORNING run had left on that same task — "last run
2026-09-17T07:31:35: acted — Task completed: Sent a heart image to Alessandro
Vernassa on Telegram" — and took it for this run's outcome: it drew nothing,
called notify_user with text only, and closed with "Task completed". The 07:30
wake of the same day had drawn the image, its own history line being from the
previous DAY; so the failure arrives exactly once a recurring task has already
succeeded within living memory of its own record.

Reproduced against the real model with a fake install (stub image tools, no
message actually sent), 3-4 runs per variant:

  * as shipped .................................. 0/3 drew, 0/3 attached
  * task row hidden: the due task shows no
    history and reads DUE NOW ................... 0/3, 0/3
  * wake prompt forbids reading manage_tasks .... 0/3 (it went shopping the web)
  * wake prompt says to PRODUCE it this wake .... 1/2 valid runs
  * both of the last two together ............... 3/3, 3/3

Neither half carries it alone, so both are pinned here:

  * `_task_line` tells a due task apart, and for it prints NEITHER `next_at`
    (which is this wake) nor `last_run`/`last_reply` (which is the occurrence
    before it) — the row of a task not due is unchanged, history and all;
  * the list is marked from `executor.due_task_ids`, and only for the caller's
    own tasks: another agent's wake is not this turn;
  * an executor has no due tasks unless somebody says so, and the wake is what
    says so (AutonomyService._wake), beside `unattended` and for the same reason;
  * the wake prompt tells the agent to make the thing, not only to send it.

Run:  server/.venv/bin/python tests/test_wake_due_task.py
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-duetask-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.autonomy import AutonomyService, build_wake_prompt  # noqa: E402
from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.storage.tasks import TaskStore  # noqa: E402
from app.tools import internal  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BUNDLED = _ROOT / "server" / "tools"
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# The two rows production had at 17:01, verbatim in shape.
DUE = dict(id="task-heart", agent_id="master", cron="30 7 * * *",
           prompt="Manda un'immagine di un cuore ad Alessandro su Telegram",
           next_at="2026-09-17T17:01:00", enabled=True,
           last_run="2026-09-17T07:31:35", last_result="acted",
           last_reply="Task completed: Sent a heart image to Alessandro "
                      "Vernassa on Telegram.")
LATER = dict(id="task-sleep", agent_id="master", cron="30 22 * * *",
             prompt="Di' a Sylvia di andare a dormire", next_at="2026-09-17T22:30:00",
             enabled=True, last_run="2026-09-16T22:30:11", last_result="acted",
             last_reply="Task completed: Message sent to Sylvia.")

# ------------------------------------------------------------- the task row
print("task row")
running = internal._task_line(DUE, True)
resting = internal._task_line(DUE)

check("last run" not in running and "2026-09-17T07:31:35" not in running,
      "due: the previous occurrence is not reported as this one")
check("Task completed" not in running,
      "due: the predecessor's success story is gone")
check("RUNNING NOW" in running and "nothing has been done for it yet" in running,
      "due: the row says this wake is the run")
check(DUE["prompt"] in running and DUE["id"] in running,
      "due: the order and the id still reach the model")
check("cron 30 7 * * *" in running, "due: the recurrence is still stated")
check("last run 2026-09-17T07:31:35" in resting and "Task completed" in resting,
      "not due: the row keeps its history")
check("RUNNING NOW" not in resting, "not due: nothing claims it is running")
check(internal._task_line(DUE, False) == resting,
      "not due is the default, and it is today's rendering")

# --------------------------------------------------- who gets marked, and when
print("\nthe list")
cfg = Path(_TMP) / "config"
tasks = TaskStore(cfg / "tasks")
agents = JsonStore(cfg / "agents")
for t in (DUE, LATER):
    tasks.store.save(t["id"], t)
agents.save("master", {"id": "master", "name": "master", "live": True})
agents.save("other", {"id": "other", "name": "other", "live": True})

model = ModelConfig(id="m", name="m", provider="llamacpp",
                    base_url="http://127.0.0.1:9")  # never contacted
registry = ToolRegistry(Path(_TMP) / "tools", bundled_dir=BUNDLED)
stores = Stores(agents=agents, models=JsonStore(cfg / "models"))
agent = Agent(id="master", name="master", tools=["manage_tasks"],
              memory_enabled=False)
ex = AgentExecutor(agent, model, registry, stores)

check(ex.due_task_ids == set(), "an executor is handed no due task by default")


def listing(executor) -> str:
    import asyncio
    return asyncio.run(internal.manage_tasks_handler(
        action="list", executor=executor, _tasks=tasks))


plain = listing(ex)
check("last run 2026-09-17T07:31:35" in plain,
      "outside a wake the heart task reads exactly as it does today")
check("RUNNING NOW" not in plain, "outside a wake nothing is running")

ex.due_task_ids = {"task-heart"}
woken = listing(ex)
check("RUNNING NOW" in woken and "Task completed: Sent a heart image" not in woken,
      "in a wake the due task loses the story that misled it")
check("Task completed: Message sent to Sylvia" in woken,
      "the tasks NOT due keep their history in the same listing")

# Someone else's tasks are someone else's wake.
other = Agent(id="other", name="other", tools=["manage_tasks"], memory_enabled=False)
ex_other = AgentExecutor(other, model, registry, stores)
ex_other.due_task_ids = {"task-heart"}
ex_other.scheduling_target_ids = lambda: ["master"]
foreign = __import__("asyncio").run(internal.manage_tasks_handler(
    action="list", agent_id="master", executor=ex_other, _tasks=tasks))
check("RUNNING NOW" not in foreign and "Task completed: Sent a heart image" in foreign,
      "another agent's list is not marked by THIS turn's due tasks")

# ------------------------------------------------------------------ who sets it
print("\nwiring")
src = inspect.getsource(AutonomyService._wake)
check("executor.due_task_ids = due_ids" in src,
      "a wake hands its executor the tasks it is running")

# -------------------------------------------------------------- the wake prompt
print("\nwake prompt")
granted = {"manage_tasks", "notify_user"}
with_tasks = build_wake_prompt(agent, [DUE], granted)
without = build_wake_prompt(agent, [], granted)
check("produce it in THIS wake" in with_tasks,
      "a due task is told to be carried out, not just announced")
check("an earlier run of the same recurring task does not count" in with_tasks,
      "and told why its own record is not evidence")
check("produce it in THIS wake" not in without,
      "a manual wake with nothing due says nothing about producing")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all ok")
