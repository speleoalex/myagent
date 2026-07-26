"""Server-side live chat generation, decoupled from any client connection.

A chat turn runs in a background asyncio task (a ``LiveRun``) that keeps going
even if the browser closes the SSE connection. Every SSE event it produces is
buffered, so a client that connects (or reconnects) mid-flight can replay what
happened so far and then follow the live tail. This is what lets the user leave
the chat during a response and still see it continue when they return, and what
makes the Stop button able to cancel generation server-side.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class LiveRun:
    """One in-flight generation for a session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.events: list[dict] = []          # buffered events (for replay)
        self.done = False
        self.stopped = False
        self.task: asyncio.Task | None = None
        self._subs: list[asyncio.Queue] = []  # live subscriber queues

    def emit(self, event: dict) -> None:
        """Buffer an event and fan it out to current subscribers."""
        self.events.append(event)
        for q in self._subs:
            q.put_nowait(event)

    def _close(self) -> None:
        self.done = True
        for q in self._subs:
            q.put_nowait(None)  # sentinel: stream ended

    async def subscribe(self):
        """Async-generate buffered events, then live ones until the run ends.

        Relies on asyncio being single-threaded: capturing ``len(events)`` and
        appending the queue happen without an await in between, so no event is
        lost or duplicated across the replay/live boundary."""
        if self.done:
            for ev in self.events:
                yield ev
            return
        q: asyncio.Queue = asyncio.Queue()
        n = len(self.events)      # events[:n] already buffered
        self._subs.append(q)      # events[n:] will arrive via the queue
        try:
            for ev in self.events[:n]:
                yield ev
            while True:
                ev = await q.get()
                if ev is None:
                    break
                yield ev
        finally:
            if q in self._subs:
                self._subs.remove(q)


class LiveRunManager:
    """Tracks at most one active generation per session id."""

    def __init__(self):
        self.runs: dict[str, LiveRun] = {}

    def get(self, session_id: str) -> LiveRun | None:
        return self.runs.get(session_id)

    def is_active(self, session_id: str) -> bool:
        r = self.runs.get(session_id)
        return r is not None and not r.done

    def start(self, session_id: str, drive) -> LiveRun:
        """Start a background run. ``drive`` is an async callable(run) that
        produces the turn's events via ``run.emit(...)`` and persists it."""
        run = LiveRun(session_id)
        self.runs[session_id] = run
        run.task = asyncio.create_task(self._drive(run, drive))
        return run

    async def _drive(self, run: LiveRun, drive) -> None:
        try:
            await drive(run)
        except asyncio.CancelledError:
            # Stop button: drive() already persisted the partial answer.
            run.stopped = True
            run.emit({"type": "stopped"})
        except Exception as e:  # defensive: never let a run die silently
            log.exception("Live run failed: %s", e)
            run.emit({"type": "error", "data": str(e) or type(e).__name__})
        finally:
            run._close()
            # Completed turns are persisted to the session; drop the run so a
            # returning client reads it from disk instead of re-attaching.
            if self.runs.get(run.session_id) is run:
                self.runs.pop(run.session_id, None)

    async def stop(self, session_id: str) -> bool:
        run = self.runs.get(session_id)
        if run and run.task and not run.task.done():
            run.task.cancel()
            return True
        return False
