"""The scheduled-task store: one JSON file per task, under ``config/tasks/``.

Tasks live in CONFIG_DIR, not in the autonomy runtime dir, because a schedule is
user intent — small, precious, part of the backup set — just like agents and
models. Persistence, atomic writes and id safety come from JsonStore; what this
adds is the ONE thing the rest of the system must not reimplement: turning a
``cron``/``at`` schedule into ``next_at``.

Delivery semantics, inherited from the event queue this replaces:
**run-once-on-success**. ``next_at`` is only advanced after a wake that did not
error, so a failed run is retried (at-least-once; the wake prompt warns about
repeats). Retry pressure is bounded by ``max_wakes_per_hour`` and by the
auto-pause after ``max_consecutive_errors`` — see AutonomyService.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.engine import cron as cron_parser
from app.models import Task
from app.storage.sessions import now_iso
from app.storage.store import JsonStore

log = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class TaskStore:
    def __init__(self, directory: Path):
        self.store = JsonStore(Path(directory))
        # Set by AutonomyService: called after any change, so a task that is due
        # now starts within an instant instead of at the next 5s scan.
        self.on_change: Callable[[str], None] | None = None

    # ------------------------------------------------------------------- read
    def list_all(self, agent_id: str = "") -> list[dict]:
        """All tasks (optionally of one agent), soonest first. Tasks with no
        next occurrence (a one-shot already run) sort last, not first: an empty
        ``next_at`` would win a plain string comparison."""
        rows = [t for t in self.store.list_all()
                if not agent_id or t.get("agent_id") == agent_id]
        return sorted(rows, key=lambda t: (not t.get("next_at"),
                                           t.get("next_at") or "",
                                           t.get("id") or ""))

    def get(self, task_id: str) -> dict | None:
        return self.store.get(task_id)

    def due(self, agent_id: str, now: str | None = None) -> list[dict]:
        """Enabled tasks of one agent whose time has come, soonest first.
        String comparison on the now_iso() format, as everywhere else."""
        now = now or now_iso()
        return [t for t in self.list_all(agent_id)
                if t.get("enabled", True) and t.get("next_at")
                and t["next_at"] <= now]

    # ------------------------------------------------------------------ write
    def save(self, task: Task) -> dict:
        """Persist a task, (re)computing ``next_at`` from its schedule."""
        data = task.model_dump()
        data["created_at"] = data.get("created_at") or now_iso()
        data["next_at"] = self.compute_next(task) if task.enabled else ""
        self.store.save(task.id, data)
        self._changed(task.agent_id)
        return data

    def delete(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if not self.store.delete(task_id):
            return False
        self._changed((task or {}).get("agent_id", ""))
        return True

    def delete_for_agent(self, agent_id: str) -> int:
        """Drop every task of a deleted agent (nothing would ever run them)."""
        gone = 0
        for t in self.list_all(agent_id):
            gone += 1 if self.store.delete(t["id"]) else 0
        return gone

    def run_now(self, task_id: str) -> dict | None:
        """Make a task due immediately (the UI's "run now" and an external
        poke). Enables it too: asking for a run implies wanting it to run."""
        data = self.store.get(task_id)
        if data is None:
            return None
        data["enabled"] = True
        data["next_at"] = now_iso()
        self.store.save(task_id, data)
        self._changed(data.get("agent_id", ""))
        return data

    def advance(self, task_id: str, result: str, reply: str = "", *,
                reschedule: bool = True) -> None:
        """Record the outcome of a run and schedule the next occurrence.

        A recurring task moves to its next cron time; a one-shot has none left,
        so it is disabled and stays in the list as a visible, deletable record
        of what happened (the full transcript is in the autonomous session).

        ``reschedule=False`` records the attempt but leaves ``next_at`` alone,
        so the task stays due and is retried: that is the failure path. It still
        writes the outcome, because otherwise a task that has been failing for
        an hour keeps showing the green result of its last success and the
        Tasks page looks healthy while nothing is getting done."""
        data = self.store.get(task_id)
        if data is None:
            return                       # deleted mid-wake: nothing to update
        data["last_run"] = now_iso()
        data["last_result"] = result
        data["last_reply"] = reply or ""
        if not reschedule:
            self.store.save(task_id, data)
            return
        if data.get("cron"):
            nxt = cron_parser.next_after(data["cron"], datetime.now())
            data["next_at"] = _iso(nxt) if nxt else ""
            if not data["next_at"]:
                data["enabled"] = False  # expression with no future occurrence
        else:
            data["next_at"] = ""
            data["enabled"] = False
        self.store.save(task_id, data)

    # ------------------------------------------------------------------ misc
    def compute_next(self, task: Task, after: datetime | None = None) -> str:
        """The next moment this task should run, "" if never.

        No schedule at all means "as soon as possible": that is how an external
        poke queues one-shot work. A one-shot whose ``at`` has already passed
        keeps that time, so it fires on the next scan rather than being lost —
        the tool layer is what refuses a hallucinated past timestamp, because
        only there can it tell a mistake from a deliberate catch-up."""
        if task.cron:
            nxt = cron_parser.next_after(task.cron, after or datetime.now())
            return _iso(nxt) if nxt else ""
        return task.at or now_iso()

    def new_id(self) -> str:
        return f"task-{uuid.uuid4().hex[:8]}"

    def _changed(self, agent_id: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(agent_id)
        except Exception:
            # The task is durably stored either way; a failed kick only delays
            # pickup to the next scan (~5s).
            log.debug("task kick for '%s' failed", agent_id, exc_info=True)
