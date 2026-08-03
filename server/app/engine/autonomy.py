"""AutonomyService — runs the due tasks of live agents.

There is exactly ONE thing that makes an agent run unattended: a due Task (see
``app.models.Task`` and ``app.storage.tasks``). No separate heartbeat — a
routine is just a task with a cron expression — so "when does this agent act,
and to do what" is answerable by looking at one list.

An agent with ``live: true`` (and ``enabled``) is autonomous: a single
supervisor loop (5s resolution) scans the agent store fresh on every round —
so toggling ``live`` from the UI takes effect within one scan, with no restart
(``live: false`` IS the kill switch) — and spawns one wake task per agent with
work due. Because ``live`` is persisted in the agent's JSON, a started agent
restarts by itself after a service or machine reboot.

A wake is one normal executor turn against the dedicated named session
``autonomous_<agent_id>``, driven through the LiveRunManager (cancel/timeout/
attach come for free) and recorded with the same helpers as connector chats.
The wake prompt lists the due tasks; a reply of exactly ``NOOP`` with zero tool
calls skips session persistence entirely (a 20-min routine must not append ~70
junk turns/day), but the tasks still count as run, with no ``last_reply`` —
"seen, chose not to act" is a legitimate outcome. Any tool call means the turn
is persisted (actions must be auditable).

Single-wake guarantee, three layers: the ``_wakes`` task map, the
``live.is_active`` check before ``live.start`` (which silently overwrites!),
and the drive taking ``named_sessions.lock(sid)`` — the same lock
``_chat_named`` uses, so a user chatting in the autonomous session and a wake
can never interleave.

Rescheduling belongs to TaskStore.advance and happens only after a successful
wake: a failed run is RETRIED, with a growing delay (see ``RETRY_BACKOFF_BASE``)
and bounded by ``max_wakes_per_hour``. There is no terminal failure state —
``live: false`` is the only stop, because that one is the user's. Several tasks
due at the same moment share ONE wake (and one rate-limit slot), which is also
why a slow local-model turn can never build up a backlog of overlapping runs.
Per-agent runtime state (last wake, error streak, retry_after, wake history) is
persisted to ``<autonomy>/<agent_id>/state.json``, so a restart causes no wake
storm.

``max_consecutive_errors`` says when to TELL THE USER, not when to give up: at
that many failures in a row the service sends one notice through the agent's own
notify target, and one more when wakes start working again.
"""
from __future__ import annotations

import asyncio
import logging
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
# Conversation cap for autonomous sessions of agents WITHOUT memory: recurring
# wakes would otherwise grow the compact history without bound. Agents with
# memory_enabled get real continuity from the compactor instead.
NO_MEMORY_CONV_CAP = 40

# A failed wake waits longer each time, and NEVER stops being retried. The old
# behaviour — auto-pause after max_consecutive_errors, cleared only by a human
# (POST /resume or re-saving the agent) — got the design backwards twice:
#
#  * Most failures are transient (network not up at boot, Ollama not started,
#    a remote 429/5xx), which is exactly what an unattended agent must ride out.
#    The connectors side already holds this line: BaseConnector.start() retries
#    transport errors with the poll loop's own backoff.
#  * Even a "permanent" failure gets fixed OUTSIDE MyAgent. Observed: master
#    auto-paused on "credit balance is too low" and stayed dark for ~20 hours;
#    the cause was fixed by switching the default model, and the agent only came
#    back because an unrelated re-save happened to touch its mtime.
#
# The old code also had no floor BETWEEN consecutive attempts — max_wakes_per_hour
# is a rolling COUNT — so five identical failures burned the whole error budget
# in 1.4 seconds (measured) before anyone could see them.
RETRY_BACKOFF_BASE = 60.0      # after the first failure
RETRY_BACKOFF_MAX = 1800.0     # ceiling: keep trying twice an hour, forever

_NOOP_RE = re.compile(r"^\s*noop[.!]?\s*$", re.IGNORECASE)

# What the service says when it needs the user. Plain sentences, no emoji: this
# can be spoken by a voice satellite, and Raspberry Pi OS has no emoji font.
_ALERT_TEXT = (
    "Scheduler notice for agent '{name}': {n} scheduled runs failed in a row. "
    "Last error: {reason}. Retrying every {mins} minutes — nothing is lost, the "
    "tasks stay due. Fix the cause, or switch the agent off if that is what you "
    "want."
)
_RECOVERED_TEXT = (
    "Scheduler notice for agent '{name}': scheduled runs are working again "
    "(after {n} failures in a row)."
)


def _retry_delay(errors: int) -> float:
    """Seconds to wait before retrying, after *errors* consecutive failures."""
    if errors <= 0:
        return 0.0
    return min(RETRY_BACKOFF_BASE * 2 ** (errors - 1), RETRY_BACKOFF_MAX)


def _iso_in(seconds: float) -> str:
    """A timestamp *seconds* from now, in ``now_iso``'s exact shape.

    ``timespec="seconds"`` is not cosmetic here: ``retry_after`` is compared
    against ``now_iso()`` LEXICOGRAPHICALLY (see _tick), so a different
    precision would silently order wrong.
    """
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")

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


def _when(task: dict) -> str:
    """How a task's schedule reads in the prompt: the cron expression is what
    the model can act on (it may be asked to change it), the clock time alone
    is not."""
    if task.get("cron"):
        return f"recurring, cron {task['cron']}"
    return "one-off" + (f", due {(task.get('next_at') or '')[11:16]}"
                        if task.get("next_at") else "")


def build_wake_prompt(agent: Agent, tasks: list[dict],
                      granted_tools: set[str] | None = None,
                      next_task: dict | None = None) -> str:
    # Group wildcards (<category>/*) in agent.tools hide the concrete ids, so
    # the caller passes the expanded set; fall back to the raw list when not.
    granted = granted_tools if granted_tools is not None else set(agent.tools)
    lines = [
        f"[AUTONOMOUS WAKE — {now_iso()}]",
        "You are running unattended; no user is reading this reply.",
    ]
    # The due tasks are phrased as orders, not as a bulletin. Measured failure
    # (when standing instructions were a separate concept, listed first): the
    # model ran the imperative, specific routine, ignored a scheduled reminder
    # holding a message for the user, and replied NOOP — after which the event
    # was archived as handled and was gone. Everything is one list now, but the
    # phrasing is what stopped that, so keep it.
    if tasks:
        lines.append(f"\nTasks due now ({len(tasks)}) — handle every one "
                     "of them in THIS wake:")
        for i, t in enumerate(tasks, 1):
            lines.append(f"{i}. [{_when(t)}] {_short(t.get('prompt') or '', 300)}")
        # "Last chance" is literally true (a successful turn advances the task
        # to its next occurrence) and it is what stops the model from deferring.
        lines.append(
            "Each one was scheduled earlier, usually by you on the user's behalf. "
            "If a task carries or asks for something the user should see, send it "
            "with notify_user now: once this wake ends the task moves on and you "
            "will not be shown it again.")
        if "manage_tasks" in granted:
            # Observed: given "handle it now", the model re-queued the very task
            # it was handed (three wakes for one proverb) — scheduling is an
            # available action and "later" is the cheapest plan. It is due; the
            # only correct time is now.
            lines.append("Do NOT schedule a new task for anything listed above: "
                         "it is due now, so carry it out in this wake.")
        lines.append("(After a failure, a task may be presented again.)")
    else:
        lines.append("\nNo task is due — this is a manual wake.")
    if next_task:
        lines.append(f"\nNext scheduled: {next_task.get('next_at') or '?'} — "
                     f"{_short(next_task.get('prompt') or '', 120)}")
    if "notify_user" in granted:
        lines.append("\nTo contact the user, use the notify_user tool. "
                     "Your reply text is only logged.")
    if "manage_tasks" in granted:
        line = ("You can schedule future work for yourself, and review or "
                "cancel what is already scheduled, with the manage_tasks tool.")
        # Kept in step with the injected agent_id parameter (executor
        # _with_scheduling_targets): unannounced, a model that holds the grant
        # does not reach for it during a wake, where there is no user to suggest it.
        if getattr(agent, "schedule_others", False):
            line += (" Pass its agent_id to schedule work for another agent, and "
                     "autonomy_control's agent_id to start one.")
        lines.append(line)
    if "call_agent" in granted:
        # Load-bearing, not a courtesy: a router-style agent typically holds no
        # tool that can observe anything (master has none), so delegation is its
        # ONLY way to learn a fact. And its system prompt is written for
        # interactive use — "the user's current request", "ask the user for
        # clarification" — which describes nothing that exists during a
        # wake. Without this line the model reliably skips call_agent and
        # invents the answer instead, while happily using the two tools named
        # above. Keep it symmetrical with them.
        lines.append("To find something out, delegate with call_agent to one of "
                     "the agents listed in your system prompt: there is no user "
                     "to ask, so gather the facts yourself before reporting. "
                     "Never state anything you have not verified this way.")
    if tasks:
        # NOT the bare "reply NOOP" line here: offered as the closing option, it
        # reads as permission to skip the tasks above. Asking for a summary also
        # makes the task's recorded `last_reply` say what happened to them.
        lines.append("When every task above is handled, close with one short line "
                     "saying what you did (logged, not sent). Reply exactly NOOP "
                     "only if there was genuinely nothing to do for any of them.")
    else:
        lines.append("If there is nothing to do, reply exactly: NOOP")
    return "\n".join(lines)


class AutonomyService:
    def __init__(self, stores, tool_registry, named_sessions,
                 live, tasks, base_dir: Path):
        self.stores = stores
        self.tool_registry = tool_registry
        # Rotation/archival of the autonomous sessions is the named store's own
        # policy (save_rotating), so no web SessionStore handle is needed here.
        self.named = named_sessions
        self.live = live
        self.tasks = tasks
        self.base = Path(base_dir)
        self._loop_task: asyncio.Task | None = None
        self._wakes: dict[str, asyncio.Task] = {}
        self._kick = asyncio.Event()
        self._states: dict[str, dict] = {}  # agent_id -> persisted state (cached)
        # How the SERVICE speaks to the user, set in main.py with a closure over
        # app.state — the connectors plugin that delivers registers itself later,
        # exactly like ToolRegistry.notify_targets. None = no channel installed.
        self.send_notification = None
        tasks.on_change = self.notify

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
        """A task changed: wake the supervisor loop immediately."""
        self._kick.set()

    # ------------------------------------------------------------ state file
    def _state_path(self, agent_id: str) -> Path:
        return self.base / agent_id / "state.json"

    def _state(self, agent_id: str) -> dict:
        st = self._states.get(agent_id)
        if st is None:
            st = read_json(self._state_path(agent_id)) or {}
            # Legacy: the terminal auto-pause that RETRY_BACKOFF_* replaced.
            # Dropped on read, and the error streak with it — nothing clears
            # that flag any more, so an upgrade would otherwise leave an agent
            # stopped forever, and the streak would start the backoff at its
            # ceiling and skip the notification.
            if st.pop("paused", None) is not None:
                st.pop("paused_agent_mtime", None)
                st.pop("retry_after", None)
                st["consecutive_errors"] = 0
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
            running = self._wakes.get(aid)
            if running is not None and not running.done():
                continue  # one wake at a time (layer 1)
            cfg = agent.autonomous or AutonomousConfig()
            st = self._state(aid)
            retry_after = st.get("retry_after") or ""
            if retry_after and now < retry_after:
                continue  # error backoff still running; the tasks stay due
            if not self.tasks.due(aid, now):
                continue
            if len(self._wakes_last_hour(st)) >= cfg.max_wakes_per_hour:
                continue  # rate-limited: tasks wait, nothing is lost
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
        wake_tasks: list[dict] = []
        try:
            # Layer 2: live.start would silently overwrite an active run.
            if self.live.is_active(sid):
                return
            wake_tasks = self.tasks.due(aid, started)
            due_ids = {t["id"] for t in wake_tasks}
            # What comes AFTER this wake, so the agent can answer "what's next?"
            # without a tool call. Skipping the ones running right now: they are
            # in the list above and will have moved on by the time it matters.
            upcoming = next((t for t in self.tasks.list_all(aid)
                             if t.get("enabled", True) and t.get("next_at")
                             and t["id"] not in due_ids), None)
            executor = await AgentExecutor.create_for_agent(
                aid, self.tool_registry, self.stores)
            prompt = build_wake_prompt(
                agent, wake_tasks,
                set(self.tool_registry.expand_tool_ids(agent.tools)),
                next_task=upcoming)
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
            self._finish_wake(agent, cfg, wake_tasks, result, timed_out,
                              stopped, manual)
            self._wakes.pop(aid, None)
            self._kick.set()  # re-evaluate immediately (more due agents?)

    def _finish_wake(self, agent: Agent, cfg: AutonomousConfig,
                     wake_tasks: list[dict], result: dict,
                     timed_out: bool, stopped: bool, manual: bool) -> None:
        aid = agent.id
        st = self._state(aid)
        # How many failures in a row before the user is told. 0 is storable and
        # means "never tell me" — it must silence the recovery notice too, or a
        # `>= 0` test would fire one after every successful wake.
        threshold = cfg.max_consecutive_errors
        failed = timed_out or result["error"] is not None
        if failed:
            # The tasks keep their next_at, so they stay due and are retried —
            # but they DO record the attempt, or the Tasks page would keep
            # showing the last success while nothing is getting done.
            reason = "wake timed out" if timed_out else result["error"]
            for t in wake_tasks:
                self.tasks.advance(t["id"], "timeout" if timed_out else "error",
                                   _short(reason or "", 120), reschedule=False)
            errors = st.get("consecutive_errors", 0) + 1
            st["consecutive_errors"] = errors
            st["last_result"] = "timeout" if timed_out else "error"
            st["last_error"] = reason
            delay = _retry_delay(errors)
            st["retry_after"] = _iso_in(delay)
            log.warning("agent '%s' wake failed (%d in a row), retrying in %ds: %s",
                        aid, errors, int(delay), _short(reason or "", 120))
            # Exactly AT the threshold, so the notice is sent once and not on
            # every later retry: a channel that repeats the same alert every
            # 30 minutes is a channel the user mutes.
            if threshold > 0 and errors == threshold:
                self._announce(agent, _ALERT_TEXT.format(
                    name=agent.name, n=errors, reason=_short(reason or "", 160),
                    mins=max(1, int(_retry_delay(errors + 1) // 60))))
        elif stopped:
            st["last_result"] = "stopped"  # user intervention: not an error
        else:
            noop = result["tool_calls"] == 0 and _NOOP_RE.match(result["reply"] or "")
            reply = ""
            if not noop:
                tools = f" [tools: {result['tool_calls']}]" if result["tool_calls"] else ""
                reply = _short(result["reply"]) + tools
            # Only now do the tasks move on: same run-once-on-success contract
            # the event queue had. All tasks of one wake share one outcome.
            for t in wake_tasks:
                self.tasks.advance(t["id"], "noop" if noop else "acted", reply)
            recovered = st.get("consecutive_errors", 0)
            st["consecutive_errors"] = 0
            st["last_result"] = "noop" if noop else "acted"
            st.pop("last_error", None)
            st.pop("retry_after", None)
            # Only if the user was told it was broken. Silence after an alert
            # reads as "still broken", and the whole point of alerting is that
            # nobody is watching the badge.
            if threshold > 0 and recovered >= threshold:
                self._announce(agent, _RECOVERED_TEXT.format(
                    name=agent.name, n=recovered))
        self._save_state(aid, st)
        log.info("wake of '%s' finished: %s%s", aid, st["last_result"],
                 " (manual)" if manual else "")

    def _announce(self, agent: Agent, text: str) -> None:
        """Tell the user out-of-band about the SCHEDULER's own state.

        An agent whose wakes keep failing is precisely the one that cannot
        report it: it never gets to run, so ``notify_user`` is never called.
        The old auto-pause only wrote a log line, and the agent went dark for
        ~20 hours with nobody told — found by noticing a badge.

        Fire-and-forget: this runs inside _finish_wake's bookkeeping, and a
        notice that could not be delivered must not turn into a second failure.
        """
        sender = self.send_notification
        if sender is None:
            return          # no connectors plugin: the log line is all there is

        async def deliver() -> None:
            try:
                result = await sender(agent, text)
            except Exception as e:
                log.warning("could not announce state of '%s': %s", agent.id, e)
                return
            if isinstance(result, str) and result.startswith("ERROR"):
                log.warning("could not announce state of '%s': %s", agent.id, result)

        asyncio.create_task(deliver())

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
                reasoning_text = ""
                recorded = False
                try:
                    async for event in executor.run_stream(prompt, prior, None,
                                                           memory_context(session)):
                        et = event.get("type")
                        if et == "tool_result":
                            tool_events.append(event.get("data", {}))
                        elif et == "token":
                            reply_text += event.get("data", "")
                        elif et == "reasoning":
                            reasoning_text += event.get("data", "")
                        elif et == "clear_tokens":
                            reply_text = ""
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
                                record_turn(session, steps, reply, conv,
                                            data.get("reasoning") or reasoning_text)
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
                                    partial, None, reasoning_text)
                        self.named.save_rotating(sid, session)
                    raise
            if result["tool_calls"] and agent.memory_enabled:
                schedule_compaction(executor, sid, named=self.named)
        return drive

    # ------------------------------------------------------------ public API
    async def wake_now(self, agent_id: str) -> bool:
        """Manual trigger (UI/tests): runs whatever is due right now, bypassing
        the rate limit and the pause — an explicit user action. With nothing
        due it is still a real turn (the prompt says so), which is what makes
        it a usable smoke test. Returns False when the agent is unknown or
        disabled, or when a wake is already running."""
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
        """Retry now: clear the error backoff and the streak."""
        st = self._state(agent_id)
        st["consecutive_errors"] = 0
        st.pop("retry_after", None)
        st.pop("paused", None)              # legacy field, see _state
        st.pop("paused_agent_mtime", None)
        self._save_state(agent_id, st)
        self._kick.set()

    def drop_agent(self, agent_id: str) -> None:
        """Forget a deleted agent: cached runtime state, its on-disk directory
        (state.json) and its tasks — nothing would ever run them again. This
        module owns that layout: callers (the delete endpoint) must not reach
        into it themselves."""
        self._states.pop(agent_id, None)
        self._wakes.pop(agent_id, None)
        agent_dir = self.base / agent_id
        if agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)
        self.tasks.delete_for_agent(agent_id)

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
            elif len(self._wakes_last_hour(st)) >= cfg.max_wakes_per_hour:
                state = "rate_limited"
            elif st.get("retry_after", "") > now:
                state = "retrying"   # failing, but still trying — not stopped
            elif st.get("consecutive_errors", 0) > 0:
                state = "error"
            else:
                state = "idle"
            agent_tasks = self.tasks.list_all(aid)
            out[aid] = {
                "state": state,
                "live": live_flag,
                "last_wake": st.get("last_wake", ""),
                "last_result": st.get("last_result", ""),
                "last_error": st.get("last_error", ""),
                # Not a stored field any more: the next wake IS the soonest
                # scheduled task, so there is nothing to keep in sync.
                "next_wake": next((t["next_at"] for t in agent_tasks
                                   if t.get("enabled", True) and t.get("next_at")), ""),
                "consecutive_errors": st.get("consecutive_errors", 0),
                # When the error backoff lets the next attempt through. Needed
                # separately from next_wake: a failed task keeps its next_at, so
                # next_wake sits in the PAST while this is what actually gates.
                "retry_after": st.get("retry_after", ""),
                "wakes_last_hour": len(self._wakes_last_hour(st)),
                "tasks": len(agent_tasks),
                "due_tasks": sum(1 for t in agent_tasks if t.get("enabled", True)
                                 and t.get("next_at") and t["next_at"] <= now),
                "session_id": session_id_for(aid),
            }
        return out
