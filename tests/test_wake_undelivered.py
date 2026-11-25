#!/usr/bin/env python3
"""A wake whose notification never got through must not look like a success.

Orin, 2026-11: the master's daily tasks (weather, good morning, 13:00 news)
lost every message for days. Two Telegram bots were enabled, both bound to the
master, with nothing to pick one by, so notify_user refused the recipient
as ambiguous. The Tasks page showed every run "acted": a wake that calls any
tool was a success, whatever the tools answered. The refusal survived only in
the model's own reply.

Pinned here, with the real AutonomyService bookkeeping and TaskStore on a
temporary MYAGENT_HOME (no model, no channel):

  * every notify_user call failing -> outcome "undelivered", the tool's error
    leading last_reply;
  * one refusal then a delivery -> still "acted";
  * a wake that never notified -> "acted" (nothing to judge it by);
  * "undelivered" is NOT an error: tasks move on, no retry, no error streak;
  * the drive actually feeds the check (source inspection: the bookkeeping only
    sees the result dict).

Run:  server/.venv/bin/python tests/test_wake_undelivered.py
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-undelivered-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine import autonomy  # noqa: E402
from app.engine.autonomy import AutonomyService, _delivery_error  # noqa: E402
from app.models import Agent, AutonomousConfig  # noqa: E402
from app.storage.tasks import TaskStore  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


AMBIGUOUS = ("ERROR: several bots could reach Alessandro — pass one of them as "
             "channel: master-it, myagent-notebook-bot")
SENT = "Message sent via binding 'master-it' to: Alessandro on Telegram."


def event(tool: str, preview: str) -> dict:
    return {"tool": tool, "arguments": {}, "result_preview": preview}


# ------------------------------------------------------------ the judgement
print("_delivery_error")
check(_delivery_error([]) == "", "no tool calls: nothing to judge")
check(_delivery_error([event("web_search", "ERROR: timeout")]) == "",
      "a failing tool that is not the delivery one does not count")
check(_delivery_error([event("notify_user", AMBIGUOUS)] * 3) == AMBIGUOUS,
      "every notify_user refused: the last error is returned")
check(_delivery_error([event("notify_user", AMBIGUOUS),
                       event("notify_user", SENT)]) == "",
      "refused once, then delivered: a delivery")
check(_delivery_error([event("notify_user", "  " + AMBIGUOUS)]) == AMBIGUOUS,
      "leading whitespace does not hide an error")

# ---------------------------------------------------------- the bookkeeping
print("_finish_wake")
cfg_dir = Path(_TMP) / "config"
tasks = TaskStore(cfg_dir / "tasks")
service = AutonomyService(stores=None, tool_registry=None, named_sessions=None,
                          live=None, tasks=tasks, base_dir=Path(_TMP) / "autonomy")
agent = Agent(id="master", name="Master", model_id="m", live=True)
cfg = AutonomousConfig()


def run(result: dict) -> dict:
    tasks.store.save("task-weather", dict(
        id="task-weather", agent_id="master", cron="0 7 * * *",
        prompt="Meteo di oggi", next_at="2026-11-25T07:00:00", enabled=True))
    due = [tasks.get("task-weather")]
    base = {"reply": "", "tool_calls": 0, "error": None, "delivery_error": ""}
    service._finish_wake(agent, cfg, due, {**base, **result},
                         timed_out=False, stopped=False, manual=False)
    return tasks.get("task-weather")


t = run({"reply": "Invio non riuscito", "tool_calls": 4,
         "delivery_error": AMBIGUOUS})
st = service._state("master")
check(t["last_result"] == "undelivered", "refused notification -> undelivered")
check(t["last_reply"].startswith("notify_user: ERROR: several bots"),
      "last_reply opens with the tool's error, not the model's prose")
check("Invio non riuscito" in t["last_reply"], "the model's reply is kept after it")
check(t["next_at"] != "2026-11-25T07:00:00", "the task moved on (no retry of done work)")
check(st["last_result"] == "undelivered", "agent state records it too")
check(st.get("consecutive_errors", 0) == 0 and "retry_after" not in st,
      "not an error: no streak, no backoff")
check(t["last_wake_tasks"] == 1, "a wake with one task says so (no shared-reply mark)")

tasks.store.save("task-greet", dict(
    id="task-greet", agent_id="master", cron="0 7 * * *",
    prompt="Buongiorno", next_at="2026-11-25T07:00:00", enabled=True))
tasks.store.save("task-weather", dict(
    id="task-weather", agent_id="master", cron="0 7 * * *",
    prompt="Meteo di oggi", next_at="2026-11-25T07:00:00", enabled=True))
service._finish_wake(agent, cfg, [tasks.get("task-greet"), tasks.get("task-weather")],
                     {"reply": "Buongiorno inviato", "tool_calls": 3, "error": None,
                      "delivery_error": ""},
                     timed_out=False, stopped=False, manual=False)
check(tasks.get("task-weather")["last_wake_tasks"] == 2
      and tasks.get("task-greet")["last_wake_tasks"] == 2,
      "two tasks in one wake: both rows say their reply is shared by 2")

t = run({"reply": "Meteo inviato", "tool_calls": 2})
check(t["last_result"] == "acted", "delivered -> acted")
check(not t["last_reply"].startswith("notify_user:"), "no error prefix when delivered")

t = run({"reply": "NOOP", "tool_calls": 0, "delivery_error": AMBIGUOUS})
check(t["last_result"] == "noop", "a NOOP stays noop")

# ------------------------------------------------------------- the wiring
print("wiring")
src = inspect.getsource(AutonomyService._make_autonomous_drive)
check('result["delivery_error"] = _delivery_error(tool_events)' in src,
      "the drive computes delivery_error from the turn's tool events")
check(autonomy.NOTIFY_USER == "notify_user", "the delivery tool is notify_user")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
