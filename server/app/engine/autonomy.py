"""AutonomyService — wakes live agents on a heartbeat and on queued events.

An agent with ``live: true`` (and ``enabled``) is autonomous: a single
supervisor loop (5s resolution) scans the agent store fresh on every round —
so toggling ``live`` from the UI takes effect within one scan, with no restart
(``live: false`` IS the kill switch) — and spawns one wake task per due agent.
Because ``live`` is persisted in the agent's JSON, a started agent restarts by
itself after a service or machine reboot.

A wake is one normal executor turn against the dedicated named session
``autonomous_<agent_id>``, driven through the LiveRunManager (cancel/timeout/
attach come for free) and recorded with the same helpers as connector chats.
The wake prompt lists the due events and the standing instructions; a reply of
exactly ``NOOP`` with zero tool calls skips session persistence entirely (a
30-min heartbeat must not append ~50 junk turns/day), but the events are still
marked ``reacted`` with ``reaction: null`` — "seen, chose not to act" is a
legitimate outcome. Any tool call means the turn is persisted (actions must be
auditable).

Single-wake guarantee, three layers: the ``_wakes`` task map, the
``live.is_active`` check before ``live.start`` (which silently overwrites!),
and the drive taking ``named_sessions.lock(sid)`` — the same lock
``_chat_named`` uses, so a user chatting in the autonomous session and a wake
can never interleave.

Scheduling is fixed-delay-from-completion (+ jitter): ``next_wake = wake end +
interval_s + jitter`` — a slow local-model turn never causes overlapping ticks
or a backlog. Per-agent state (last/next wake, error streak, pause, wake
history) is persisted to ``<autonomy>/<agent_id>/state.json``, so a restart
causes no wake storm.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.engine import prompts
from app.engine.executor import AgentExecutor
from app.engine.memory_compactor import schedule_compaction
from app.models import Agent, AutonomousConfig, ChatMessage
from app.storage.sessions import (memory_context, now_iso, read_json,
                                  record_turn, steps_from, write_json)

log = logging.getLogger(__name__)

# Supervisor loop resolution: an enable/disable/interval change or a rate-limit
# window opening is noticed within this many seconds (a queued event kicks the
# loop immediately instead).
SCAN_INTERVAL = 5.0
# Conversation cap for autonomous sessions of agents WITHOUT deep memory: the
# heartbeat would otherwise grow the compact history without bound. Agents with
# memory_enabled get real continuity from the compactor instead.
NO_MEMORY_CONV_CAP = 40

_NOOP_RE = re.compile(r"^\s*noop[.!]?\s*$", re.IGNORECASE)

# Naming convention of the dedicated per-agent autonomous session. Owned here;
# other layers (the sessions router) must use these helpers, never the literal.
AUTONOMOUS_PREFIX = "autonomous_"


def session_id_for(agent_id: str) -> str:
    return f"{AUTONOMOUS_PREFIX}{agent_id}"


def is_autonomous_session(session_id: str) -> bool:
    return (session_id or "").startswith(AUTONOMOUS_PREFIX)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def build_wake_prompt(agent: Agent, cfg: AutonomousConfig, events: list[dict],
                      granted_tools: set[str] | None = None) -> str:
    # Group wildcards (<category>/*) in agent.tools hide the concrete ids, so
    # the caller passes the expanded set; fall back to the raw list when not.
    granted = granted_tools if granted_tools is not None else set(agent.tools)
    lines = [
        f"[AUTONOMOUS WAKE — {now_iso()}]",
        "You are running unattended; no user is reading this reply.",
    ]
    # Events come BEFORE the standing instructions, and they are phrased as
    # orders rather than as a bulletin. Measured failure: with the instructions
    # first and the events as a bare bullet list, master ran the (imperative,
    # specific) routine, ignored a scheduled reminder holding a message for the
    # user, and replied NOOP — after which the event was archived as handled and
    # was gone. A one-shot task loses a priority contest against a recurring one
    # every time, so it must not be in the same list at all.
    if events:
        lines.append(f"\nTasks queued for you ({len(events)}) — handle every one "
                     "of them in THIS wake:")
        for i, e in enumerate(events, 1):
            payload = e.get("payload") or {}
            text = payload.get("text") if isinstance(payload, dict) else None
            if not text:
                text = json.dumps(payload, ensure_ascii=False)
            due = (e.get("due_at") or "")[11:16]
            src = e.get("source") or ""
            tag = e.get("type", "event") + (f", due {due}" if due else "") \
                + (f", from {src}" if src else "")
            lines.append(f"{i}. [{tag}] {_short(text, 300)}")
        # "Last chance" is literally true (consume() archives them after a
        # successful turn) and it is what stops the model from deferring.
        lines.append(
            "Each one was queued earlier, usually by you on the user's behalf. "
            "If a task carries or asks for something the user should see, send it "
            "with notify_user now: once this wake ends the task is archived and "
            "you will never be shown it again.")
        if "schedule_task" in granted:
            # Observed: given "handle it now", the model re-queued the very task
            # it was handed (three wakes for one proverb) — schedule_task is an
            # available action and "later" is the cheapest plan. It is due; the
            # only correct time is now.
            lines.append("Do NOT use schedule_task on a task already listed above: "
                         "it is due now, so carry it out in this wake.")
        lines.append("(After a failure, events may be delivered again.)")
    else:
        lines.append("\nNo pending tasks — this is a periodic heartbeat.")
    if cfg.instructions.strip():
        scope = ", on top of the tasks above" if events else ""
        lines += [f"\nStanding instructions (the routine for every wake{scope}):",
                  cfg.instructions.strip()]
    if "notify_user" in granted:
        lines.append("\nTo contact the user, use the notify_user tool. "
                     "Your reply text is only logged.")
    if "schedule_task" in granted:
        lines.append("You can schedule future work for yourself with the "
                     "schedule_task tool.")
    if "call_agent" in granted:
        # Load-bearing, not a courtesy: a router-style agent typically holds no
        # tool that can observe anything (master has none), so delegation is its
        # ONLY way to learn a fact. And its system prompt is written for
        # interactive use — "the user's current request", "ask the user for
        # clarification" — which describes nothing that exists during a
        # heartbeat. Without this line the model reliably skips call_agent and
        # invents the answer instead, while happily using the two tools named
        # above. Keep it symmetrical with them.
        lines.append("To find something out, delegate with call_agent to one of "
                     "the agents listed in your system prompt: there is no user "
                     "to ask, so gather the facts yourself before reporting. "
                     "Never state anything you have not verified this way.")
    if events:
        # NOT the bare "reply NOOP" line here: offered as the closing option, it
        # reads as permission to skip the tasks above. Asking for a summary also
        # makes the archived `reaction` say what happened to them.
        lines.append("When every task above is handled, close with one short line "
                     "saying what you did (logged, not sent). Reply exactly NOOP "
                     "only if there was genuinely nothing to do for any of them.")
    else:
        lines.append("If there is nothing to do, reply exactly: NOOP")
    return "\n".join(lines)


class AutonomyService:
    def __init__(self, stores, tool_registry, named_sessions,
                 live, events, base_dir: Path):
        self.stores = stores
        self.tool_registry = tool_registry
        # Rotation/archival of the autonomous sessions is the named store's own
        # policy (save_rotating), so no web SessionStore handle is needed here.
        self.named = named_sessions
        self.live = live
        self.events = events
        self.base = Path(base_dir)
        self._loop_task: asyncio.Task | None = None
        self._wakes: dict[str, asyncio.Task] = {}
        self._kick = asyncio.Event()
        self._states: dict[str, dict] = {}  # agent_id -> persisted state (cached)
        events.on_append = self.notify

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())
            log.info("Autonomy scheduler started")

    async def aclose(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        # Stop in-flight wakes (their drives persist the partial turn).
        for agent_id, task in list(self._wakes.items()):
            if not task.done():
                await self.live.stop(session_id_for(agent_id))
        pending = [t for t in self._wakes.values() if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)
        self._wakes.clear()

    def notify(self, agent_id: str) -> None:
        """An event was queued: wake the supervisor loop immediately."""
        self._kick.set()

    # ------------------------------------------------------------ state file
    def _state_path(self, agent_id: str) -> Path:
        return self.base / agent_id / "state.json"

    def _state(self, agent_id: str) -> dict:
        st = self._states.get(agent_id)
        if st is None:
            st = read_json(self._state_path(agent_id)) or {}
            self._states[agent_id] = st
        return st

    def _save_state(self, agent_id: str, st: dict) -> None:
        self._states[agent_id] = st
        path = self._state_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, st)

    def _agent_mtime(self, agent_id: str) -> float:
        return self.stores.agents.mtime(agent_id)

    # ------------------------------------------------------------- main loop
    async def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("autonomy scan failed")
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=SCAN_INTERVAL)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()

    def _wakes_last_hour(self, st: dict) -> list[str]:
        floor = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        return [t for t in st.get("wake_history", []) if t >= floor]

    def _tick(self) -> None:
        now = now_iso()
        for data in self.stores.agents.list_all():
            if not (data.get("enabled", True) and data.get("live")):
                continue
            try:
                agent = Agent(**data)
            except Exception:
                continue
            aid = agent.id
            task = self._wakes.get(aid)
            if task is not None and not task.done():
                continue  # one wake at a time (layer 1)
            cfg = agent.autonomous or AutonomousConfig()
            st = self._state(aid)
            if st.get("paused"):
                # Re-saving the agent clears the pause: editing the config is
                # an explicit sign of intent (the alternative is POST /resume).
                if self._agent_mtime(aid) > st.get("paused_agent_mtime", float("inf")):
                    st["paused"] = False
                    st["consecutive_errors"] = 0
                    self._save_state(aid, st)
                else:
                    continue
            due_heartbeat = cfg.interval_s > 0 and now >= (st.get("next_wake") or "")
            due_events = self.events.pending_count(aid, now) > 0
            if not (due_heartbeat or due_events):
                continue
            if len(self._wakes_last_hour(st)) >= cfg.max_wakes_per_hour:
                continue  # rate-limited: events wait, nothing is lost
            self._wakes[aid] = asyncio.create_task(self._wake(agent, cfg))

    # ------------------------------------------------------------- one wake
    async def _wake(self, agent: Agent, cfg: AutonomousConfig,
                    manual: bool = False) -> None:
        aid = agent.id
        sid = session_id_for(aid)
        started = now_iso()
        st = self._state(aid)
        st["last_wake"] = started
        st["wake_history"] = self._wakes_last_hour(st) + [started]
        self._save_state(aid, st)
        result = {"reply": "", "tool_calls": 0, "error": None}
        timed_out = False
        stopped = False
        wake_events: list[dict] = []
        try:
            # Layer 2: live.start would silently overwrite an active run.
            if self.live.is_active(sid):
                return
            wake_events = self.events.pending(aid)
            executor = await AgentExecutor.create_for_agent(
                aid, self.tool_registry, self.stores)
            prompt = build_wake_prompt(
                agent, cfg, wake_events,
                set(self.tool_registry.expand_tool_ids(agent.tools)))
            drive = self._make_autonomous_drive(agent, executor, sid, prompt, result)
            run = self.live.start(sid, drive)
            try:
                await asyncio.wait_for(asyncio.shield(run.task), cfg.wake_timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                await self.live.stop(sid)  # drive persists the partial turn
                try:
                    await run.task
                except (asyncio.CancelledError, Exception):
                    pass
            stopped = run.stopped and not timed_out
        except Exception as e:
            log.exception("wake of '%s' failed", aid)
            result["error"] = str(e) or type(e).__name__
        finally:
            self._finish_wake(agent, cfg, wake_events, result, timed_out,
                              stopped, manual)
            self._wakes.pop(aid, None)
            self._kick.set()  # re-evaluate immediately (more due agents?)

    def _finish_wake(self, agent: Agent, cfg: AutonomousConfig,
                     wake_events: list[dict], result: dict,
                     timed_out: bool, stopped: bool, manual: bool) -> None:
        aid = agent.id
        st = self._state(aid)
        failed = timed_out or result["error"] is not None
        if failed:
            # Events stay pending (reacted: false) for redelivery.
            st["consecutive_errors"] = st.get("consecutive_errors", 0) + 1
            st["last_result"] = "timeout" if timed_out else "error"
            st["last_error"] = "wake timed out" if timed_out else result["error"]
            if st["consecutive_errors"] >= cfg.max_consecutive_errors:
                st["paused"] = True
                st["paused_agent_mtime"] = self._agent_mtime(aid)
                log.warning("agent '%s' auto-paused after %d consecutive errors",
                            aid, st["consecutive_errors"])
        elif stopped:
            st["last_result"] = "stopped"  # user intervention: not an error
        else:
            noop = result["tool_calls"] == 0 and _NOOP_RE.match(result["reply"] or "")
            reaction = None
            if not noop:
                tools = f" [tools: {result['tool_calls']}]" if result["tool_calls"] else ""
                reaction = _short(result["reply"]) + tools
            if wake_events:
                self.events.consume(aid, [e["id"] for e in wake_events], reaction)
            st["consecutive_errors"] = 0
            st["last_result"] = "noop" if noop else "acted"
            st.pop("last_error", None)
        if cfg.interval_s > 0:
            jitter = random.uniform(0, min(30.0, cfg.interval_s * 0.1))
            nxt = datetime.now() + timedelta(seconds=cfg.interval_s + jitter)
            st["next_wake"] = nxt.isoformat(timespec="seconds")
        else:
            st["next_wake"] = ""  # events only
        self._save_state(aid, st)
        log.info("wake of '%s' finished: %s%s", aid, st["last_result"],
                 " (manual)" if manual else "")

    def _make_autonomous_drive(self, agent: Agent, executor, sid: str,
                               prompt: str, result: dict):
        """Mirror of routers.chat._make_drive on the named-session store,
        plus the NOOP contract. The session is loaded INSIDE named.lock(sid)
        (layer 3), so a user turn in the same session can't interleave.
        Recording goes through the same storage helpers as channel chats, so
        the autonomous session stays byte-compatible with them."""
        # None means "all defaults", as everywhere else in this module.
        cfg = agent.autonomous or AutonomousConfig()

        async def drive(run):
            async with self.named.lock(sid):
                session = await asyncio.to_thread(self.named.get, sid, agent.id)
                session.setdefault("channel", sid)
                session["source"] = "autonomous"
                session["agent_id"] = agent.id
                if not session.get("title"):
                    session["title"] = f"{agent.name} (live)"
                # Only the tail: see AutonomousConfig.history_messages for why a
                # wake must NOT get the interactive-sized window. The full
                # conversation stays on disk (audit trail) and, with memory on,
                # keeps being archived into the digest that is injected below.
                stored = session.get("conversation", [])
                if cfg.history_messages > 0:
                    stored = stored[-cfg.history_messages:]
                else:
                    stored = []
                prior = [ChatMessage(**m) for m in stored]
                tool_events: list[dict] = []
                reply_text = ""
                recorded = False
                try:
                    async for event in executor.run_stream(prompt, prior, None,
                                                           memory_context(session)):
                        et = event.get("type")
                        if et == "tool_result":
                            tool_events.append(event.get("data", {}))
                        elif et == "token":
                            reply_text += event.get("data", "")
                        elif et == "error":
                            result["error"] = str(event.get("data", "")) or "error"
                        elif et == "done":
                            data = event.get("data", {})
                            reply = data.get("reply") or reply_text
                            result["reply"] = reply
                            result["tool_calls"] = len(tool_events)
                            noop = not tool_events and _NOOP_RE.match(reply or "")
                            if not noop:
                                session["messages"].append({
                                    "role": "user", "text": prompt,
                                    "autonomous": True, "agent_id": agent.id,
                                    "ts": now_iso(),
                                })
                                steps = steps_from(data.get("trace"), tool_events)
                                conv = data.get("conversation")
                                record_turn(session, steps, reply, conv)
                                if not agent.memory_enabled:
                                    session["conversation"] = \
                                        session.get("conversation", [])[-NO_MEMORY_CONV_CAP:]
                                await asyncio.shield(asyncio.to_thread(
                                    self.named.save_rotating, sid, session))
                            recorded = True
                        run.emit(event)
                except asyncio.CancelledError:
                    # Stop/timeout mid-generation: keep the partial turn.
                    if not recorded:
                        session["messages"].append({
                            "role": "user", "text": prompt, "autonomous": True,
                            "agent_id": agent.id, "ts": now_iso(),
                        })
                        partial = (f"{reply_text}\n\n{prompts.INTERRUPTED}" if reply_text
                                   else prompts.INTERRUPTED)
                        record_turn(session, steps_from(None, tool_events),
                                    partial, None)
                        self.named.save_rotating(sid, session)
                    raise
            if result["tool_calls"] and agent.memory_enabled:
                schedule_compaction(executor, sid, named=self.named)
        return drive

    # ------------------------------------------------------------ public API
    async def wake_now(self, agent_id: str) -> bool:
        """Manual trigger (UI/tests): bypasses heartbeat schedule, rate limit
        and pause — an explicit user action. Returns False when the agent is
        unknown/disabled or a wake is already running."""
        data = self.stores.agents.get(agent_id)
        if data is None or not data.get("enabled", True):
            return False
        try:
            agent = Agent(**data)
        except Exception:
            return False
        task = self._wakes.get(agent_id)
        if (task is not None and not task.done()) or \
                self.live.is_active(session_id_for(agent_id)):
            return False
        cfg = agent.autonomous or AutonomousConfig()
        self._wakes[agent_id] = asyncio.create_task(
            self._wake(agent, cfg, manual=True))
        return True

    async def stop(self, agent_id: str) -> bool:
        return await self.live.stop(session_id_for(agent_id))

    async def resume(self, agent_id: str) -> None:
        st = self._state(agent_id)
        st["paused"] = False
        st["consecutive_errors"] = 0
        self._save_state(agent_id, st)
        self._kick.set()

    def drop_agent(self, agent_id: str) -> None:
        """Forget a deleted agent: cached runtime state AND its on-disk
        directory (state.json + event queues). This module owns that layout —
        callers (the delete endpoint) must not reach into it themselves."""
        self._states.pop(agent_id, None)
        self._wakes.pop(agent_id, None)
        agent_dir = self.base / agent_id
        if agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)

    def status(self) -> dict:
        """Per-agent runtime status for the UI/API."""
        out = {}
        now = now_iso()
        for data in self.stores.agents.list_all():
            aid = data.get("id")
            live_flag = bool(data.get("enabled", True) and data.get("live"))
            st = self._state(aid)
            if not live_flag and not st:
                continue  # never-autonomous agent: keep the payload small
            try:
                cfg = AutonomousConfig(**(data.get("autonomous") or {}))
            except Exception:
                cfg = AutonomousConfig()
            task = self._wakes.get(aid)
            running = task is not None and not task.done()
            if not live_flag:
                state = "disabled"
            elif running:
                state = "running"
            elif st.get("paused"):
                state = "paused"
            elif len(self._wakes_last_hour(st)) >= cfg.max_wakes_per_hour:
                state = "rate_limited"
            elif st.get("consecutive_errors", 0) > 0:
                state = "error"
            else:
                state = "idle"
            out[aid] = {
                "state": state,
                "live": live_flag,
                "last_wake": st.get("last_wake", ""),
                "last_result": st.get("last_result", ""),
                "last_error": st.get("last_error", ""),
                "next_wake": st.get("next_wake", "") if cfg.interval_s > 0 else "",
                "consecutive_errors": st.get("consecutive_errors", 0),
                "wakes_last_hour": len(self._wakes_last_hour(st)),
                "pending_events": self.events.pending_count(aid, now),
                "session_id": session_id_for(aid),
            }
        return out
