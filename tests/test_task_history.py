#!/usr/bin/env python3
"""What a scheduled task sees of its own past (Task.history_runs).

A wake used to get the tail of the agent's autonomous conversation, counted in
raw messages: one run of the job-search agent is ~40 of them, nearly all tool
calls, so a window of 10 delivered the last reply and not even its request, and
every task of the agent shared it. Pinned here, on autonomy.task_history:

  * 0 (the default) means nothing at all;
  * a task sees only its OWN runs, by the ids each wake records, and a run
    recorded before those ids existed is matched by the task's text;
  * a run is the request and the final reply, never a tool call;
  * interrupted runs don't count, and N means the last N;
  * older runs are dropped first when they don't fit, the newest is cut instead;
  * the Task model refuses a value out of range.

Run:  server/.venv/bin/python tests/test_task_history.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from app.engine import prompts  # noqa: E402
from app.engine.autonomy import task_history  # noqa: E402
from app.models import Task  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def estimate(text: str) -> int:
    return len(text) // 4


JOBS = {"id": "jobs", "prompt": "Daily job search", "history_runs": 2}
GREET = {"id": "greet", "prompt": "Morning greeting", "history_runs": 0}


def run(ts: str, reply: str, task_ids=None, text="[AUTONOMOUS WAKE] ...") -> list[dict]:
    user = {"role": "user", "text": text, "autonomous": True, "ts": ts}
    if task_ids is not None:
        user["task_ids"] = task_ids
    return [user,
            {"role": "tool", "tool": "job_search", "result": "20 results"},
            {"role": "assistant", "text": reply, "ts": ts}]


session = {"messages": [
    *run("2026-11-20T07:30", "legacy report", text="1. [recurring] Daily job search"),
    *run("2026-11-21T07:30", "report 21", ["jobs"]),
    *run("2026-11-22T07:30", "good morning", ["greet"]),
    *run("2026-11-23T07:30", "report 23", ["jobs"]),
    *run("2026-11-24T07:30", "partial\n\n" + prompts.INTERRUPTED, ["jobs"]),
    {"role": "user", "text": "a person typing in the web chat"},
    {"role": "assistant", "text": "a chat answer"},
    *run("2026-11-25T07:30", "report 25", ["greet", "jobs"]),
]}

print("what a task asks for")
check(task_history(session, [GREET], 10_000, estimate) == [], "0 means no history")
got = task_history(session, [JOBS], 10_000, estimate)
texts = [m.content for m in got]
check(len(got) == 4 and [m.role for m in got] == ["user", "assistant"] * 2,
      "two runs, each a request and a reply")
check(texts[1] == "report 23" and texts[3] == "report 25",
      "the last two COMPLETED runs, oldest first (the interrupted one is skipped)")
check(texts[0].startswith("[Earlier run, 2026-11-23T07:30]") and "Daily job search" in texts[0],
      "the request names the run and the task, not the long wake prompt")
check(not any("good morning" in t or "chat answer" in t or "20 results" in t for t in texts),
      "no other task's run, no web chat, no tool result")

print("runs from before the ids")
deep = dict(JOBS, history_runs=10)
texts = [m.content for m in task_history(session, [deep], 10_000, estimate)]
check("legacy report" in texts, "an untagged run is matched by the task's text")

print("the context budget")
long = {"messages": [*run("2026-11-23T07:30", "x" * 4000, ["jobs"]),
                     *run("2026-11-24T07:30", "y" * 4000, ["jobs"])]}
got = task_history(long, [JOBS], 1200, estimate)
check(len(got) == 2 and got[1].content.startswith("y"),
      "what does not fit is dropped oldest first")
got = task_history(long, [JOBS], 300, estimate)
check(len(got) == 2 and got[1].content.endswith("… [cut]") and len(got[1].content) < 4000,
      "the newest run is cut rather than dropped")

print("the model")
try:
    Task(id="t", agent_id="a", prompt="p", history_runs=11)
    check(False, "history_runs above the maximum is refused")
except ValueError:
    check(True, "history_runs above the maximum is refused")
check(Task(id="t", agent_id="a", prompt="p").history_runs == 0, "default 0")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
