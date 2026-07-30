"""Internal tool handlers that require Python executor context."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

# Module-level on purpose (no import cycle: executor -> registry, and neither
# imports this module; main.py wires the handlers after both are loaded).
from app.engine.executor import AgentExecutor
from app.models import AutonomousConfig
from app.storage.sessions import now_iso

log = logging.getLogger(__name__)

MAX_AGENT_DEPTH = 5


def _resolve_attachments(executor, indices) -> list[dict]:
    """Map attachment_indices (from a call_agent tool-call) to the parent turn's
    actual attachments. The tool-call only carries small integer indices, so the
    model never has to reproduce base64 blobs. Accepts an int, a list, or a
    loose string (e.g. "[0, 1]" / "0,1") since text-based tool calls vary."""
    pool = executor.turn_attachments if executor is not None else []
    if not pool or indices is None:
        return []
    if isinstance(indices, str):
        indices = [p for p in re.split(r"[,\s]+", indices.strip().strip("[]")) if p]
    elif isinstance(indices, int):
        indices = [indices]
    if not isinstance(indices, (list, tuple)):
        return []

    resolved: list[dict] = []
    seen: set[int] = set()
    for raw in indices:
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(pool) and i not in seen:
            seen.add(i)
            resolved.append(pool[i])
    return resolved


async def call_agent_handler(
    agent_id: str, message: str, executor=None, attachment_indices=None, **kwargs
) -> str:
    """Call another agent and return its response.

    ``attachment_indices`` optionally forwards a subset of the current user
    turn's attachments (referenced by 0-based index) to the sub-agent, so a
    router agent can hand an image/file to a specialized sub-agent."""
    if executor is None:
        return "ERROR: No executor context available for agent chaining"

    # An empty message is never a real delegation: it is what a small model
    # produces when the prompt pushes it to delegate and it has nothing to say
    # (or when the text-tool-call parser could not find a message). Refuse here,
    # before spawning a sub-agent — the sub-agent would just answer "send me
    # something". The error goes back as the tool result, so the model gets a
    # chance to retry properly or answer the user itself.
    if not (message or "").strip() and not _resolve_attachments(executor, attachment_indices):
        return (
            "ERROR: 'message' is empty. call_agent needs the actual request for "
            f"'{agent_id}' (or an attachment to forward via attachment_indices). "
            "Send the user's request as 'message', or answer the user yourself."
        )

    depth = executor.depth
    if depth >= MAX_AGENT_DEPTH:
        return f"ERROR: Maximum agent chaining depth ({MAX_AGENT_DEPTH}) reached"

    try:
        # Access gate: the caller may only delegate to agents it's allowed to
        # reach (target enabled + callable + in the caller's allowlist). This is
        # the real enforcement — the system-prompt directory is only advisory.
        target = executor.stores.agents.get(agent_id)
        if target is None:
            return f"ERROR: agent '{agent_id}' not found"
        if not executor.can_call(target):
            return f"ERROR: agent '{agent_id}' is not callable from '{executor.agent.id}'"

        sub_executor = await AgentExecutor.create_for_agent(
            agent_id,
            executor.tool_registry,
            executor.stores,
            depth=depth + 1,
        )
        forwarded = _resolve_attachments(executor, attachment_indices)
        response = await sub_executor.run(message, attachments=forwarded or None)

        # Hand the sub-agent's full trace to the parent so the whole flow is
        # persisted recursively (consumed by the parent's call_agent step), and
        # forward its tool activity to the parent's SSE stream.
        executor.record_sub_trace(
            response.trace or {
                "agent_id": agent_id,
                "iterations": response.iterations,
                "reply": response.reply,
                "steps": [],
            }
        )
        executor.emit_sub_events(agent_id, response.tool_results)

        return response.reply
    except Exception as e:
        return f"ERROR: Failed to call agent '{agent_id}': {e}"


# Values a model types when the schema says "omit this to use the default" — it
# helpfully writes the word instead of leaving the argument out. Observed live:
# a wake passed binding_id="default", which is truthy, so it beat the agent's
# configured target and the send failed with 409 Binding is not running. Treating
# these as "not supplied" is safe: none of them is a plausible binding or chat id.
_PLACEHOLDER_ARGS = frozenset({
    "default", "none", "null", "nil", "undefined", "n/a", "na", "-", "...",
    "binding_id", "chat_id", "<binding_id>", "<chat_id>",
})


def _or_configured(supplied, configured: str | None) -> str:
    """An explicitly supplied id, else the agent's configured one."""
    value = str(supplied or "").strip()
    if value.lower() in _PLACEHOLDER_ARGS:
        value = ""
    return value or (configured or "")


def _split_chat_ids(raw) -> list[str]:
    """One or more chat ids, from a comma/semicolon/whitespace-separated string.

    A single field holding several recipients is the natural thing for a user to
    write, and it MUST be split before it reaches the transport — see the note in
    notify_user_handler about Telegram silently delivering only to the first.
    Order is preserved and duplicates dropped, so the same chat is never messaged
    twice in one call."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", str(raw or ""))]
    out, seen = [], set()
    for p in parts:
        if p and p.lower() not in _PLACEHOLDER_ARGS and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _connector_for(state, binding_id: str):
    """The running connector for a binding, or a message explaining why not.

    Returns ``(connector, error)``. The plugin may not be installed at all —
    which is a normal configuration, not a crash — so this has to read as an
    instruction to the model rather than a traceback.
    """
    services = getattr(state, "connectors", None) if state is not None else None
    if services is None:
        return None, ("ERROR: the connectors plugin is not installed on this "
                      "server, so there is no messaging channel to notify.")
    connector = services.manager.get_connector(binding_id)
    if connector is None:
        return None, (f"ERROR: binding '{binding_id}' is not running. Check that "
                      f"it exists, is enabled and has a valid token.")
    return connector, ""


async def notify_user_handler(
    text: str = "", binding_id: str = "", chat_id: str = "",
    executor=None, _named=None, _state=None, **kwargs,
) -> str:
    """Push a message to the user through a messaging channel (Telegram).

    The default target comes from the agent's ``autonomous.notify_binding_id``
    / ``notify_chat_id``; explicit arguments override it. The connector is
    reached in-process — ``_state`` is the app state, read at call time, so the
    plugin can be registered after this handler is bound."""
    if not (text or "").strip():
        return "ERROR: 'text' is required"
    cfg = getattr(getattr(executor, "agent", None), "autonomous", None)
    binding_id = _or_configured(binding_id, cfg and cfg.notify_binding_id)
    chat_id = _or_configured(chat_id, cfg and cfg.notify_chat_id)
    recipients = _split_chat_ids(chat_id)
    if not binding_id or not recipients:
        return ("ERROR: no notification target. Pass binding_id and chat_id, "
                "or set notify_binding_id / notify_chat_id in the agent's "
                "autonomous configuration.")

    connector, error = _connector_for(_state, binding_id)
    if connector is None:
        return error

    # One send per recipient. Never hand a comma-joined string to the transport:
    # Telegram ACCEPTS "471091560,1489486090", parses the leading digits,
    # delivers to the first chat only and returns ok:true. No error, no warning,
    # second recipient silently dropped (verified against the live API).
    sent, failed = [], []
    for one in recipients:
        try:
            await connector.send(one, text)
        except Exception as e:
            # Detail to the log, not to the model: a transport error can quote a
            # URL that embeds the bot token.
            log.warning("notify_user: send to %s failed: %s", one, e)
            failed.append(f"{one} ({type(e).__name__})")
            continue
        # The session key is asked of the connector: it derives from the
        # binding's session_prefix, and that connector is the only thing that
        # knows how to build it.
        logged = await _log_to_channel(
            connector.session_id_for(one), text, executor, _named)
        sent.append(one if logged else f"{one} (not added to its history)")

    if not sent:
        return f"ERROR: nothing was sent via binding '{binding_id}': {'; '.join(failed)}"
    out = f"Message sent via binding '{binding_id}' to: {', '.join(sent)}."
    if failed:
        out += f" FAILED for: {'; '.join(failed)}."
    return out


async def _log_to_channel(sid: str, text: str, executor, named) -> bool:
    """Append a sent notification to the chat's own conversation.

    Without this the message exists only in Telegram: the channel session has no
    record, so the next inbound turn replays a history in which the agent never
    said it — ask "repeat that" and it repeats the message BEFORE the
    notification. Appending it to ``conversation`` (what the model replays) and to
    ``messages`` (what the UI shows) closes that gap.

    The session id comes from the connectors server, which owns the naming; with
    no id there is nothing to append to. Best-effort by design: the message has
    already been delivered, so a bookkeeping failure must never turn a successful
    send into an error — it only annotates the result.
    """
    if not sid or named is None:
        return False
    try:
        agent_id = getattr(getattr(executor, "agent", None), "id", "") or ""
        async with named.lock(sid):
            session = await asyncio.to_thread(named.get, sid, agent_id)
            # Marked so the UI (and a human reading the log) can tell an
            # unprompted push from a reply to something the user asked.
            session.setdefault("messages", []).append(
                {"role": "assistant", "text": text, "ts": now_iso(),
                 "notification": True})
            session.setdefault("conversation", []).append(
                {"role": "assistant", "content": text})
            # save_rotating, not save: a notification-only channel never gets a
            # normal chat turn, so this append is its ONLY chance to rotate.
            await asyncio.to_thread(named.save_rotating, sid, session)
        return True
    except Exception as e:
        log.warning("notify_user: could not append to session %s: %s", sid, e)
        return False


async def autonomy_control_handler(
    action: str = "", executor=None, _autonomy=None, **kwargs,
) -> str:
    """Let the CALLING agent switch its own autonomous mode on/off and report
    it — the chat-level "start yourself" / "stop yourself" / "are you active?".

    Acts on ``Agent.live``, the persisted single switch the scheduler re-reads
    on every scan, so the effect survives restarts. 'stop' also cancels a wake
    in flight (the background autonomous session only — chat sessions like the
    one this call came from are untouched). ``_autonomy`` is bound at
    registration time, never by the model."""
    if _autonomy is None:
        return "ERROR: autonomy service not available"
    agent = getattr(executor, "agent", None)
    if agent is None:
        return "ERROR: No executor context available"
    aid = agent.id
    act = (action or "").strip().lower()
    if act not in ("start", "stop", "status"):
        return f"ERROR: invalid action {action!r} (use start, stop or status)"

    store = executor.stores.agents
    data = store.get(aid)
    if data is None:
        return f"ERROR: agent '{aid}' not found"

    if act == "status":
        st = _autonomy.status().get(aid) or {}
        live_flag = bool(data.get("enabled", True) and data.get("live"))
        lines = [f"Autonomous mode: {'ON' if live_flag else 'OFF'}"
                 f" (state: {st.get('state', 'disabled')})"]
        if st.get("last_wake"):
            lines.append(f"Last wake: {st['last_wake']} -> {st.get('last_result') or '?'}"
                         + (f" ({st['last_error']})" if st.get("last_error") else ""))
        if st.get("next_wake"):
            lines.append(f"Next scheduled wake: {st['next_wake']}")
        lines.append(f"Pending events: {st.get('pending_events', 0)}; "
                     f"wakes in the last hour: {st.get('wakes_last_hour', 0)}")
        return "\n".join(lines)

    if act == "start":
        if not data.get("enabled", True):
            return (f"ERROR: agent '{aid}' is disabled — enable it first "
                    "(the scheduler only wakes enabled agents)")
        already = bool(data.get("live"))
        data["live"] = True
        store.save(aid, data)
        # Clears any error-pause and kicks the scheduler for immediate pickup.
        await _autonomy.resume(aid)
        cfg = data.get("autonomous") or {}
        # The real default lives on the model — never restate the number here.
        interval = cfg.get("interval_s", AutonomousConfig().interval_s)
        how = f"heartbeat every {interval}s" if interval > 0 else "wakes on events only"
        if already:
            return f"Autonomous mode was already ON ({how})."
        return (f"Autonomous mode is now ON ({how}). The scheduler picks it up "
                "within a few seconds; the setting persists across restarts.")

    # stop
    was_live = bool(data.get("live"))
    data["live"] = False
    store.save(aid, data)
    cancelled = await _autonomy.stop(aid)
    msg = ("Autonomous mode is now OFF." if was_live
           else "Autonomous mode was already OFF.")
    if cancelled:
        msg += " The wake that was in flight has been cancelled."
    return msg


async def schedule_task_handler(
    message: str = "", in_s=None, at: str = "", repeat_s=0,
    executor=None, _events=None, **kwargs,
) -> str:
    """Queue a future event for the CALLING agent itself (self-scheduling:
    reminders and recurring tasks). The event's text comes back in a future
    wake prompt. ``_events`` is bound at registration time (functools.partial),
    never by the model."""
    if _events is None:
        return "ERROR: event queue not available"
    agent = getattr(executor, "agent", None)
    if agent is None:
        return "ERROR: No executor context available"
    if not (message or "").strip():
        return "ERROR: 'message' is required"

    due = ""
    now = datetime.now()
    if (at or "").strip():
        try:
            when = datetime.fromisoformat(at.strip())
        except ValueError:
            return f"ERROR: 'at' must be an ISO timestamp (e.g. 2026-07-29T08:00), got: {at!r}"
        # A past 'at' is almost always a hallucinated clock: the model has no
        # reliable "now" and reuses a time it saw earlier in the conversation
        # (observed: it echoed the 16:23 from its own previous reply at 16:42).
        # Left alone the event is due on the spot, so it fires a wake
        # immediately instead of when asked — while the reply the user reads
        # still promises the later time. Refuse, and hand back the real clock so
        # the retry can be right. The tolerance covers minute-rounding and skew.
        if when < now - timedelta(seconds=90):
            return (f"ERROR: 'at' is in the past ({when.isoformat(timespec='minutes')}); "
                    f"it is now {now.isoformat(timespec='seconds')}. Pass a future "
                    "timestamp, or use in_s for a delay relative to now.")
        due = when.isoformat(timespec="seconds")
    elif in_s is not None:
        try:
            seconds = int(in_s)
        except (TypeError, ValueError):
            return f"ERROR: 'in_s' must be a number of seconds, got: {in_s!r}"
        due = (now + timedelta(seconds=max(0, seconds))).isoformat(timespec="seconds")
    try:
        repeat = max(0, int(repeat_s or 0))
    except (TypeError, ValueError):
        return f"ERROR: 'repeat_s' must be a number of seconds, got: {repeat_s!r}"

    try:
        event = _events.append(
            agent.id,
            type="schedule" if repeat else "reminder",
            payload={"text": message.strip()},
            due_at=due,
            source="tool:schedule_task",
            repeat_s=repeat,
        )
    except (RuntimeError, ValueError) as e:
        return f"ERROR: {e}"
    out = f"Scheduled for {event['due_at']}"
    if repeat:
        out += f", repeating every {repeat}s"
    if not getattr(agent, "live", False):
        out += ". Note: this agent is not live, so the event will wait until it is started."
    return out + f" (event {event['id']})."
