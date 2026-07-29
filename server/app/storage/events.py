"""Persistent per-agent event queues for autonomous agents.

One JSON file per event, under ``<autonomy>/<agent_id>/events/``:

    pending/  20260728T120000_3fa1b2c4.json   # waiting to be delivered
    archive/  20260728T120000_3fa1b2c4.json   # handled (audit trail)

The filename embeds ``due_at`` so lexicographic order IS delivery order.
Consumption is crash-safe without in-place markers: the reacted copy is
written to ``archive/`` first (tmp + os.replace, atomic), then the pending
file is unlinked. If a crash lands in between, the next ``pending()`` scan
skips any file whose name already exists in ``archive/`` (and removes the
leftover), so an event is never delivered after being archived.

Delivery semantics: **delivered-once-on-success, with a recorded outcome**.
Events are rendered into a wake prompt; only after a turn without executor
errors are they marked ``reacted: true`` (+ ``reacted_at``, ``reaction``) and
archived. ``reaction: null`` is legitimate — "seen, chose not to act" (a NOOP
wake) — the event still counts as handled. On error/timeout/crash they stay
pending with ``reacted: false`` and are re-delivered (at-least-once; the wake
prompt warns about possible repeats).

Event schema::

    {"id": "evt_3fa1b2c4", "ts": "...", "due_at": "...",
     "type": "message | schedule | reminder | webhook",
     "source": "tool:schedule_task | api | connector:telegram",
     "payload": {"text": "..."}, "repeat_s": 0,
     "reacted": false, "reacted_at": null, "reaction": null}
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.storage.sessions import now_iso, read_json, write_json

log = logging.getLogger(__name__)

PENDING_CAP = 200   # hard cap: append refuses beyond this (queue is stuck/spammed)
ARCHIVE_CAP = 500   # oldest archived events are pruned past this

_FNAME_TS = "%Y%m%dT%H%M%S"


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


class EventStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        # Set by AutonomyService: called with the agent_id after every append,
        # so a new event wakes the scheduler loop immediately.
        self.on_append: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ paths
    def _events_dir(self, agent_id: str, sub: str) -> Path:
        return self.base / agent_id / "events" / sub

    # ---------------------------------------------------------------- produce
    def append(self, agent_id: str, *, type: str = "message", payload=None,
               due_at: str = "", source: str = "", repeat_s: int = 0) -> dict:
        """Queue one event. Raises RuntimeError when the pending queue is at
        its cap (a stuck or spammed agent must not fill the disk)."""
        pending_dir = self._events_dir(agent_id, "pending")
        pending_dir.mkdir(parents=True, exist_ok=True)
        if len(list(pending_dir.glob("*.json"))) >= PENDING_CAP:
            raise RuntimeError(
                f"pending event queue for '{agent_id}' is full ({PENDING_CAP})")
        now = now_iso()
        due = due_at or now
        due_dt = _parse_iso(due) or datetime.now()
        event = {
            "id": f"evt_{uuid.uuid4().hex[:8]}",
            "ts": now,
            "due_at": due,
            "type": type,
            "source": source,
            "payload": payload if payload is not None else {},
            "repeat_s": int(repeat_s or 0),
            "reacted": False,
            "reacted_at": None,
            "reaction": None,
        }
        fname = f"{due_dt.strftime(_FNAME_TS)}_{event['id'][4:]}.json"
        write_json(pending_dir / fname, event)
        if self.on_append is not None:
            try:
                self.on_append(agent_id)
            except Exception:
                pass
        return event

    # ---------------------------------------------------------------- consume
    def _scan_pending(self, agent_id: str) -> list[tuple[Path, dict]]:
        """(path, event) pairs in delivery order, healing crashed consumes:
        a pending file whose name is already archived was consumed — finish
        the unlink instead of re-delivering it."""
        pending_dir = self._events_dir(agent_id, "pending")
        if not pending_dir.is_dir():
            return []
        archive_dir = self._events_dir(agent_id, "archive")
        out = []
        for f in sorted(pending_dir.glob("*.json")):
            if (archive_dir / f.name).exists():
                f.unlink(missing_ok=True)
                continue
            event = read_json(f)
            if event is None:
                continue
            out.append((f, event))
        return out

    def pending(self, agent_id: str, now: str | None = None) -> list[dict]:
        """Due events (``due_at <= now``), oldest first."""
        now = now or now_iso()
        return [e for _, e in self._scan_pending(agent_id)
                if (e.get("due_at") or "") <= now]

    def pending_count(self, agent_id: str, now: str | None = None) -> int:
        return len(self.pending(agent_id, now))

    def next_due(self, agent_id: str) -> str | None:
        """The earliest due_at among pending events (also future ones), so the
        scheduler can sleep exactly until the next scheduled task."""
        events = self._scan_pending(agent_id)
        return min((e.get("due_at") or "" for _, e in events), default=None) or None

    def consume(self, agent_id: str, ids: list[str], reaction: str | None = None) -> int:
        """Mark events as handled and archive them. ``reaction=None`` means
        "seen, no action taken" (still a legitimate outcome). All events of the
        same wake share one reaction. Repeating events re-queue their next
        occurrence first, so a crash can only cause a repeat, never a skip."""
        wanted = set(ids or [])
        if not wanted:
            return 0
        archive_dir = self._events_dir(agent_id, "archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        done = 0
        for path, event in self._scan_pending(agent_id):
            if event.get("id") not in wanted:
                continue
            if event.get("repeat_s", 0) > 0:
                base = _parse_iso(event.get("due_at") or "") or datetime.now()
                nxt = base + timedelta(seconds=event["repeat_s"])
                # Skip occurrences already in the past (e.g. after a long
                # downtime): fire at most one, don't burst-replay the backlog.
                while nxt < datetime.now():
                    nxt += timedelta(seconds=event["repeat_s"])
                try:
                    self.append(agent_id, type=event.get("type", "schedule"),
                                payload=event.get("payload"),
                                due_at=nxt.isoformat(timespec="seconds"),
                                source=event.get("source", ""),
                                repeat_s=event["repeat_s"])
                except RuntimeError as e:
                    log.warning("could not re-queue repeating event: %s", e)
            archived = {**event, "reacted": True, "reacted_at": now_iso(),
                        "reaction": reaction}
            write_json(archive_dir / path.name, archived)  # durable FIRST
            path.unlink(missing_ok=True)                   # then consume
            done += 1
        self.prune(agent_id)
        return done

    # ------------------------------------------------------------------ misc
    def archive(self, agent_id: str, limit: int = 50) -> list[dict]:
        """Most recent handled events (audit trail), newest first."""
        archive_dir = self._events_dir(agent_id, "archive")
        if not archive_dir.is_dir():
            return []
        files = sorted(archive_dir.glob("*.json"), reverse=True)[:limit]
        return [e for e in (read_json(f) for f in files) if e is not None]

    def prune(self, agent_id: str) -> None:
        archive_dir = self._events_dir(agent_id, "archive")
        if archive_dir.is_dir():
            files = sorted(archive_dir.glob("*.json"))
            for f in files[:max(0, len(files) - ARCHIVE_CAP)]:
                f.unlink(missing_ok=True)
        n = len(list(self._events_dir(agent_id, "pending").glob("*.json"))) \
            if self._events_dir(agent_id, "pending").is_dir() else 0
        if n >= PENDING_CAP:
            log.warning("pending event queue for '%s' is at its cap (%d)",
                        agent_id, n)
