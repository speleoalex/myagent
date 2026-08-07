"""Internal tool handlers that require Python executor context."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

# Module-level on purpose (no import cycle: executor -> registry, and neither
# imports this module; main.py wires the handlers after both are loaded).
from app.engine import cron as cron_parser
from app.engine.executor import AgentExecutor
from app.models import Task
from app.storage.sessions import now_iso

log = logging.getLogger(__name__)

MAX_AGENT_DEPTH = 5

# Sub-agent events forwarded live to the parent's stream. `done` is dropped
# (its payload is the returned reply + record_sub_trace) and `notice` never
# fires below depth 0. `error` IS forwarded so the user sees a sub-agent fail
# while it happens, not only in the parent's tool result.
_FORWARDED_SUB_EVENTS = {"token", "reasoning", "clear_tokens",
                         "tool_start", "tool_result", "error"}


def _forward_sub_event(executor, agent_id: str, event: dict) -> None:
    """Wrap one sub-agent event as agent_event and push it onto the parent's
    live queue. An event that is already an agent_event (from a deeper
    delegation) is re-rooted — its path gets this sub-agent prepended — never
    double-wrapped, so consumers see one envelope shape at any depth."""
    et = event.get("type")
    if et == "agent_event":
        data = event.get("data") or {}
        executor.push_sub_event({"type": "agent_event", "data": {
            "path": [agent_id] + list(data.get("path") or []),
            "event": data.get("event"),
        }})
    elif et in _FORWARDED_SUB_EVENTS:
        executor.push_sub_event({"type": "agent_event", "data": {
            "path": [agent_id], "event": event,
        }})


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
        # The event_sink streams the sub-agent's activity (tokens + tools)
        # into the parent's SSE stream WHILE it runs — the parent's tool loop
        # drains the queue concurrently (see executor._execute_streaming).
        response = await sub_executor.run(
            message,
            attachments=forwarded or None,
            event_sink=lambda ev: _forward_sub_event(executor, agent_id, ev),
        )

        # Hand the sub-agent's full trace to the parent so the whole flow is
        # persisted recursively (consumed by the parent's call_agent step).
        executor.record_sub_trace(
            response.trace or {
                "agent_id": agent_id,
                "iterations": response.iterations,
                "reply": response.reply,
                "steps": [],
            }
        )

        return response.reply
    except Exception as e:
        return f"ERROR: Failed to call agent '{agent_id}': {e}"


# Values a model types when the schema says "omit this to use the default" — it
# helpfully writes the word instead of leaving the argument out. Observed live:
# a wake passed binding_id="default", which is truthy, so it beat the agent's
# configured target and the send failed with 409 Binding is not running. Treating
# these as "not supplied" is safe: none of them is a plausible binding or chat id.
# "user" and friends belong here for the same reason: the schema says the target
# is the user's chat, so a model writes the word. It is never a valid chat id, and
# one seed agent shipped with a configured target of "user", which failed at send.
# NOT in this set: "all", which is now a recipient (broadcast).
_PLACEHOLDER_ARGS = frozenset({
    "default", "none", "null", "nil", "undefined", "n/a", "na", "-", "...",
    "binding_id", "chat_id", "<binding_id>", "<chat_id>",
    "user", "utente", "me", "the user", "<user>", "user_id",
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


def _split_recipients(raw) -> list[str]:
    """One or more configured recipients, comma- or semicolon-separated.

    Deliberately NOT _split_chat_ids: that one also splits on whitespace, which
    is right for ids and fatal for people — "Alessandro Vernassa" would arrive
    at the address book as two unknown names. Same placeholder and duplicate
    handling, since the value comes from the same field.
    """
    parts = [p.strip() for p in re.split(r"[,;]+", str(raw or ""))]
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


def notify_targets(state) -> dict:
    """The address book, shaped for a tool schema: ``{contacts, channels, broadcast}``.

    Empty when the connectors plugin is absent — which is a normal installation,
    so this degrades to "no enum" instead of raising: it runs while a turn's tool
    definitions are being built, and nothing there may take the turn down. Read at
    call time because the plugin registers itself into app state later.
    """
    services = getattr(state, "connectors", None) if state is not None else None
    provider = getattr(services, "notify_targets", None) if services is not None else None
    if provider is None:
        return {}
    try:
        return provider() or {}
    except Exception as e:
        log.warning("notify_user: could not read the address book: %s", e)
        return {}


def _resolve_by_name(state, to: str, channel: str, binding_id: str = ""):
    """Ask the connectors plugin to turn a person's name into recipients.

    Returns ``(binding_id, recipients, note, error)``. The plugin owns the address
    book and the channel labels, so the resolution lives there; this only bridges
    it to the tool's arguments. ``binding_id`` is passed IN as well: with two bots
    the plugin cannot pick one on its own, and its own error says to name one.
    """
    services = getattr(state, "connectors", None) if state is not None else None
    if services is None:
        return "", [], "", ("ERROR: the connectors plugin is not installed on this "
                            "server, so there is no messaging channel to notify.")
    resolver = getattr(services, "resolve_recipients", None)
    if resolver is None:
        return "", [], "", ("ERROR: this connectors plugin cannot look recipients up "
                            "by name; pass binding_id and chat_id.")
    try:
        result = resolver(to, channel, binding_id)
    except TypeError:
        # A plugin predating binding_id and the note: core and plugin are
        # installed separately, so degrade rather than crash. A TypeError raised
        # inside the resolver still surfaces — the retry raises it again.
        result = resolver(to, channel)
    found, error, *rest = result
    note = rest[0] if rest else ""
    if error or not found:
        return "", [], "", f"ERROR: {error or 'no recipient resolved'}"
    return found[0].binding_id, list(found), note, ""


def _resolve_configured(state, raw, channel: str, binding_id: str):
    """The agent's configured recipients, through the same resolver as ``to``.

    Returns ``(binding_id, targets, note, error)``. Each entry is resolved on its
    own, so one bad name cannot silence a notification the others can still
    carry: what resolves is delivered, and what does not is NAMED in the note —
    the treatment ``_broadcast`` gives a contact with no handle, for the same
    reason (the reader is a model that then reports this to a human). Only when
    NOTHING resolves is it an error, and then it is the resolver's own, which
    lists the contacts that do exist so the configuration can be corrected.
    """
    targets, unknown, notes, first_error = [], [], [], ""
    for one in _split_recipients(raw):
        found_binding, found, note, error = _resolve_by_name(
            state, one, channel, binding_id)
        if error:
            first_error = first_error or error
            unknown.append(one)
            continue
        binding_id = found_binding or binding_id
        targets += [(r.chat_id, r.display) for r in found]
        if note:
            notes.append(note)
    if not targets:
        return binding_id, [], "", first_error
    if unknown:
        notes.append(f"the agent's configured target names nobody reachable: "
                     f"{', '.join(unknown)}")
    return binding_id, targets, "; ".join(notes), ""


async def notify_user_handler(
    text: str = "", binding_id: str = "", chat_id: str = "",
    to: str = "", channel: str = "",
    executor=None, _named=None, _state=None, **kwargs,
) -> str:
    """Push a message to a person through a messaging channel.

    Three ways to say where it goes, in this order of precedence:

    1. ``to`` (+ optional ``channel``) — a name from the address book, resolved
       by the connectors plugin. This is what makes *"message Alessandro on
       Telegram"* work without the model knowing any numeric id.
    2. ``chat_id`` passed in this call — the model already knows the id.
    3. the agent's configured ``notify_to`` — a FALLBACK for when the caller
       named nobody. It goes through the SAME resolver as (1), so it may hold a
       person's name and not only a number: one definition of "who is the
       recipient", and a default that survives the person changing chat id.

    The order matters, and it used to be the reverse: the configured default was
    applied first, so an agent with a default target delivered every notification
    there and silently dropped ``to``. Observed live — the model was asked to
    message Sylvia, the message reached Alessandro, and the tool's own answer
    ("sent to 123456789") gave nothing away. Hence also the recipient DISPLAY names
    in the result: a raw id is unauditable by the one reader who could catch this.

    The connector is reached in-process; ``_state`` is the app state, read at
    call time, so the plugin can be registered after this handler is bound."""
    if not (text or "").strip():
        return "ERROR: 'text' is required"
    cfg = getattr(getattr(executor, "agent", None), "autonomous", None)
    binding_id = _or_configured(binding_id, cfg and cfg.notify_binding_id)
    to = _or_configured(to, "")
    channel = _or_configured(channel, "")

    note = ""
    ids = _split_chat_ids(chat_id)
    if to:
        binding_id, found, note, error = _resolve_by_name(
            _state, to, channel, binding_id)
        if error:
            return error
        targets = [(r.chat_id, r.display) for r in found]
    elif ids:
        # The model already knows the id, so its own id is all the display there
        # is — there is no name to report.
        targets = [(one, one) for one in ids]
    else:
        binding_id, targets, note, error = _resolve_configured(
            _state, cfg and cfg.notify_to, channel, binding_id)
        if error:
            return error
    # The same person can be reached twice — two contacts sharing a handle, or a
    # broadcast overlapping an explicit id. One send per chat, always.
    targets = list({one: (one, display) for one, display in targets}.values())
    if not binding_id or not targets:
        return ("ERROR: no notification target. Pass 'to' with a contact name "
                "(optionally 'channel'), or binding_id and chat_id, or set "
                "notify_binding_id / notify_to in the agent's autonomous "
                "configuration.")

    connector, error = _connector_for(_state, binding_id)
    if connector is None:
        return error

    # One send per recipient. Never hand a comma-joined string to the transport:
    # Telegram ACCEPTS "123456789,987654321", parses the leading digits,
    # delivers to the first chat only and returns ok:true. No error, no warning,
    # second recipient silently dropped (verified against the live API).
    sent, failed = [], []
    for one, display in targets:
        try:
            # A connector's send() is best-effort for inbound replies, so it
            # REPORTS failure instead of raising. Ignoring that return told the
            # agent "message sent" over a dead token.
            delivered = await connector.send(one, text)
        except Exception as e:
            # Detail to the log, not to the model: a transport error can quote a
            # URL that embeds the bot token.
            log.warning("notify_user: send to %s failed: %s", one, e)
            failed.append(f"{display} ({type(e).__name__})")
            continue
        if delivered is False:
            failed.append(f"{display} (the channel rejected it — see the server log)")
            continue
        # The session key is asked of the connector: it derives from the
        # binding's session_prefix, and that connector is the only thing that
        # knows how to build it.
        logged = await _log_to_channel(
            connector.session_id_for(one), text, executor, _named)
        sent.append(display if logged else f"{display} (not added to its history)")

    if not sent:
        return f"ERROR: nothing was sent via binding '{binding_id}': {'; '.join(failed)}"
    out = f"Message sent via binding '{binding_id}' to: {', '.join(sent)}."
    if failed:
        out += f" FAILED for: {'; '.join(failed)}."
    # Who the address book could NOT reach on this channel. Last, and never folded
    # into the sent list: "everyone was told" is exactly the sentence this prevents.
    if note:
        out += f" Note: {note}."
    return out


class _AgentOnly:
    """The only thing notify_user_handler reads off an executor: ``.agent``."""

    __slots__ = ("agent",)

    def __init__(self, agent):
        self.agent = agent


async def notify_agent_owner(agent, text: str, named=None, state=None) -> str:
    """Send *text* to an agent's configured notify target, with no agent turn.

    Used by the autonomy scheduler to report its OWN state — repeated wake
    failures, and recovery — which the agent cannot report itself, because a
    failing agent never gets to run a turn.

    Deliberately routed through ``notify_user_handler`` rather than calling a
    connector directly: recipient resolution by name, the configured-target
    fallback, one send per recipient and the append to the chat's own
    conversation all live there, each for a reason that was a bug first. A
    second implementation would drift from all four.
    """
    return await notify_user_handler(
        text=text, executor=_AgentOnly(agent), _named=named, _state=state)


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


# Words a model writes for "myself" when the schema says the parameter is
# optional — same failure mode as _PLACEHOLDER_ARGS, different vocabulary.
_SELF_ARGS = frozenset({"self", "myself", "own", "mine", "same", "io"})


def _target_agent(executor, agent_id) -> tuple[str, str]:
    """Which agent a scheduling call acts on: ``(agent_id, error)``.

    Empty/placeholder ⇒ the caller itself, which is the whole behaviour when
    ``Agent.schedule_others`` is off (the executor then never advertises the
    parameter). Anything else is checked against
    ``executor.scheduling_target_ids()`` — the SAME list the injected enum
    offers, so the tool can only be talked into what the schema promised. The
    error names the candidates: the reader is a model that must be able to pick
    a valid one on its next call, and an id it cannot guess is an id it will
    invent."""
    me = executor.agent.id
    wanted = str(agent_id or "").strip()
    if not wanted or wanted == me:
        return me, ""
    if wanted.lower() in _PLACEHOLDER_ARGS or wanted.lower() in _SELF_ARGS:
        return me, ""
    allowed = executor.scheduling_target_ids()
    if not allowed:
        return "", (f"you may only act on yourself (agent '{me}'): scheduling for "
                    "other agents is not enabled for you. Omit agent_id.")
    if wanted not in allowed:
        return "", (f"you may not act on agent {wanted!r}. Allowed: "
                    f"{', '.join(allowed)} — or omit agent_id for yourself.")
    return wanted, ""


async def autonomy_control_handler(
    action: str = "", agent_id: str = "", executor=None, _autonomy=None, **kwargs,
) -> str:
    """Let the CALLING agent switch its own autonomous mode on/off and report
    it — the chat-level "start yourself" / "stop yourself" / "are you active?".

    Acts on ``Agent.live``, the persisted single switch the scheduler re-reads
    on every scan, so the effect survives restarts. 'stop' also cancels a wake
    in flight (the background autonomous session only — chat sessions like the
    one this call came from are untouched). ``_autonomy`` is bound at
    registration time, never by the model.

    With ``schedule_others`` the caller may pass ``agent_id`` and act on another
    agent instead (same gate as manage_tasks — see _target_agent); every line of
    output then names that agent, because "Autonomous mode is now ON" read back
    to the user as being about the agent they were talking to."""
    if _autonomy is None:
        return "ERROR: autonomy service not available"
    agent = getattr(executor, "agent", None)
    if agent is None:
        return "ERROR: No executor context available"
    aid, err = _target_agent(executor, agent_id)
    if err:
        return f"ERROR: {err}"
    act = (action or "").strip().lower()
    if act not in ("start", "stop", "status"):
        return f"ERROR: invalid action {action!r} (use start, stop or status)"
    # Prefix, not a rewording per branch: one variable keeps every message
    # honest about whose switch was flipped.
    subject = "Autonomous mode" if aid == agent.id else f"Agent '{aid}': autonomous mode"

    store = executor.stores.agents
    data = store.get(aid)
    if data is None:
        return f"ERROR: agent '{aid}' not found"

    if act == "status":
        st = _autonomy.status().get(aid) or {}
        live_flag = bool(data.get("enabled", True) and data.get("live"))
        lines = [f"{subject}: {'ON' if live_flag else 'OFF'}"
                 f" (state: {st.get('state', 'disabled')})"]
        if st.get("last_wake"):
            lines.append(f"Last wake: {st['last_wake']} -> {st.get('last_result') or '?'}"
                         + (f" ({st['last_error']})" if st.get("last_error") else ""))
        if st.get("next_wake"):
            lines.append(f"Next scheduled wake: {st['next_wake']}")
        lines.append(f"Scheduled tasks: {st.get('tasks', 0)} "
                     f"({st.get('due_tasks', 0)} due now); "
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
        # WHEN it will run is the task list, not a setting here: an agent with
        # no task is live and idle forever, which is worth saying out loud
        # rather than letting the user wait for a wake that cannot come.
        st = _autonomy.status().get(aid) or {}
        n = st.get("tasks", 0)
        how = (f"{n} scheduled task(s), next at {st['next_wake']}"
               if n and st.get("next_wake") else
               "no scheduled task yet — nothing will happen until one is added")
        if already:
            return f"{subject} was already ON ({how})."
        return (f"{subject} is now ON ({how}). The scheduler picks it up "
                "within a few seconds; the setting persists across restarts.")

    # stop
    was_live = bool(data.get("live"))
    data["live"] = False
    store.save(aid, data)
    cancelled = await _autonomy.stop(aid)
    msg = (f"{subject} is now OFF." if was_live
           else f"{subject} was already OFF.")
    if cancelled:
        msg += " The wake that was in flight has been cancelled."
    return msg


_CRON_HINT = ("Examples: '*/20 * * * *' = every 20 minutes, '0 9 * * 1' = "
              "Mondays at 09:00, '30 7 * * 1-5' = weekdays at 07:30.")

# Every rejection of an add/update opens with this. Measured twice with a small
# local model: handed a plain "ERROR: ...", it abandoned the call and told the
# user the task was scheduled — the one outcome worse than failing, because the
# user then waits for something that will never happen. The error has to say
# that NOTHING exists yet and that the fix is to call again now.
_NOT_SCHEDULED = "ERROR: nothing was scheduled (no task exists yet)."
_RETRY_NOW = ("Call manage_tasks again with the correction in THIS turn — do not "
              "tell the user it is scheduled until a call succeeds and returns a "
              "task id.")


def _when_summary(data: dict) -> str:
    """The human half of a confirmation: an ISO timestamp alone reads as
    correct even when it is a day off."""
    nxt = data.get("next_at")
    if not nxt:
        return "no future run scheduled"
    try:
        dt = datetime.fromisoformat(nxt)
    except ValueError:
        return nxt
    delta = dt - datetime.now()
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        rel = f"in {max(0, mins)} min"
    elif mins < 60 * 24:
        rel = f"in {mins // 60}h{mins % 60:02d}"
    else:
        rel = f"in {delta.days} days"
    return f"{dt.strftime('%a %d %b %H:%M')} ({rel})"


def _task_line(t: dict) -> str:
    when = f"cron {t['cron']}" if t.get("cron") else "once"
    # Spelled out, not raw ISO: handed "2026-08-03" the model narrated it to the
    # user as "Saturday 3 August" (it is a Monday). It cannot do calendar
    # arithmetic, so it must not be asked to.
    nxt = (_when_summary(t) if t.get("next_at")
           else ("done" if not t.get("enabled", True) else "not scheduled"))
    line = f"- {t['id']} | next: {nxt} | {when} | {t.get('prompt', '')}"
    if t.get("last_run"):
        line += (f"\n    last run {t['last_run']}: {t.get('last_result') or '?'}"
                 + (f" — {t['last_reply']}" if t.get("last_reply") else ""))
    return line


async def manage_tasks_handler(
    action: str = "", prompt: str = "", cron: str = "", in_s=None, at: str = "",
    task_id: str = "", agent_id: str = "", executor=None, _tasks=None, **kwargs,
) -> str:
    """List/add/update/delete scheduled tasks — the caller's own by default.

    Scoped to the caller unless it holds ``schedule_others``, in which case
    ``agent_id`` selects another agent from the set the schema offered (see
    _target_agent); without the grant that parameter does not exist in the schema
    and is refused here too. ``_tasks`` is bound at registration time
    (functools.partial), never by the model."""
    if _tasks is None:
        return "ERROR: task store not available"
    agent = getattr(executor, "agent", None)
    if agent is None:
        return "ERROR: No executor context available"
    act = (action or "").strip().lower()
    if act not in ("list", "add", "update", "delete"):
        return f"ERROR: invalid action {action!r} (use list, add, update or delete)"
    aid, err = _target_agent(executor, agent_id)
    if err:
        # add/update must fail with the "nothing was scheduled" opener like every
        # other rejection, or the model reports success it never got.
        return (f"{_NOT_SCHEDULED} {err} {_RETRY_NOW}"
                if act in ("add", "update") else f"ERROR: {err}")
    mine = aid == agent.id
    # Whose tasks these are, in the second or third person: told "your tasks" for
    # someone else's list, the model relays it to the user as its own schedule.
    owner = "Your" if mine else f"Agent '{aid}': its"
    doer = "your future self" if mine else f"agent '{aid}'"
    holder = "you have" if mine else f"agent '{aid}' has"
    # From the STORE, not from executor.agent: that object was loaded when the
    # turn started, and autonomy_control may have switched live ON earlier in
    # this same turn — reading the stale copy made one reply say "scheduled for
    # Monday" and "your autonomous mode is OFF" in consecutive sentences.
    stored_agent = executor.stores.agents.get(aid) or {}
    is_live = bool(stored_agent.get("enabled", True) and stored_agent.get("live"))
    # A task on an agent that is not live never runs, and for someone ELSE that
    # fact is invisible from the chat — so say how to fix it, not just that it is off.
    off_note = "your autonomous mode is OFF" if mine else f"agent '{aid}' is not live"
    off_fix = "" if mine else (f" Start it with autonomy_control (action 'start', "
                               f"agent_id '{aid}').")

    if act == "list":
        rows = _tasks.list_all(aid)
        if not rows:
            if mine:
                return ("You have no scheduled tasks. Use action 'add' with a prompt "
                        "and either in_s (one-off) or cron (recurring) to create one.")
            return (f"Agent '{aid}' has no scheduled tasks. Use action 'add' with "
                    f"agent_id '{aid}', a prompt and either in_s (one-off) or cron "
                    "(recurring) to create one.")
        head = f"{owner} scheduled tasks ({len(rows)}):"
        if not is_live:
            head += (f" NOTE: {off_note}, so none of them will run until it is "
                     f"started.{off_fix}")
        return head + "\n" + "\n".join(_task_line(t) for t in rows)

    # -- the task being changed must exist and belong to the target agent
    current = None
    if act in ("update", "delete"):
        if not (task_id or "").strip():
            return f"ERROR: 'task_id' is required for {act} (use action 'list' to see the ids)"
        current = _tasks.get(task_id.strip())
        if current is None or current.get("agent_id") != aid:
            return (f"ERROR: {holder} no task with id {task_id!r} "
                    "(use action 'list'"
                    + ("" if mine else f" with agent_id '{aid}'") + ")")

    if act == "delete":
        _tasks.delete(current["id"])
        return f"Task {current['id']} cancelled ({current.get('prompt', '')})."

    # -- add / update share the schedule parsing
    schedule: dict = {}
    if (cron or "").strip():
        try:
            cron_parser.parse(cron.strip())
        except ValueError as e:
            return (f"{_NOT_SCHEDULED} Invalid cron expression: {e}. "
                    f"{_CRON_HINT} {_RETRY_NOW}")
        schedule = {"cron": cron.strip(), "at": ""}
    elif (at or "").strip():
        try:
            when = datetime.fromisoformat(at.strip())
        except ValueError:
            return (f"{_NOT_SCHEDULED} 'at' must be an ISO timestamp "
                    f"(e.g. 2026-07-30T18:15), got: {at!r}. {_RETRY_NOW}")
        # A past 'at' is almost always a hallucinated clock: the model has no
        # reliable "now" and reuses a time it saw earlier in the conversation
        # (observed: it echoed the 16:23 from its own previous reply at 16:42).
        # Left alone the task is due on the spot, so it fires immediately
        # instead of when asked — while the reply the user reads still promises
        # the later time. Refuse, and hand back the real clock so the retry can
        # be right. The tolerance covers minute-rounding and skew.
        now = datetime.now()
        if when < now - timedelta(seconds=90):
            return (f"{_NOT_SCHEDULED} 'at' is in the past "
                    f"({when.isoformat(timespec='minutes')}); it is now "
                    f"{now.isoformat(timespec='seconds')}. Pass a future timestamp, "
                    f"or use in_s for a delay relative to now. {_RETRY_NOW}")
        schedule = {"at": when.isoformat(timespec="seconds"), "cron": ""}
    elif in_s is not None:
        try:
            seconds = max(0, int(in_s))
        except (TypeError, ValueError):
            return (f"{_NOT_SCHEDULED} 'in_s' must be a number of seconds, "
                    f"got: {in_s!r}. {_RETRY_NOW}")
        due = datetime.now() + timedelta(seconds=seconds)
        schedule = {"at": due.isoformat(timespec="seconds"), "cron": ""}
    elif act == "add":
        return (f"{_NOT_SCHEDULED} No schedule given: pass in_s (a delay in "
                f"seconds), at (an ISO timestamp) or cron (recurring). "
                f"{_CRON_HINT} {_RETRY_NOW}")

    if act == "add":
        if not (prompt or "").strip():
            # Observed: the model put the task's NAME in task_id and left prompt
            # empty. Name the parameter it actually needs, and show the shape.
            return (f"{_NOT_SCHEDULED} The 'prompt' parameter is missing: it is "
                    f"the instruction {doer} will be given (task_id is "
                    "not a name, it identifies an EXISTING task). Example: "
                    "action='add', prompt='check the disk space', "
                    f"cron='0 9 * * 1'. {_RETRY_NOW}")
        data = {"id": _tasks.new_id(), "agent_id": aid, "prompt": prompt.strip(),
                "source": "agent", **schedule}
    else:
        data = {**current, **schedule, "enabled": True}
        if (prompt or "").strip():
            data["prompt"] = prompt.strip()

    try:
        saved = _tasks.save(Task(**data))
    except ValueError as e:
        return f"{_NOT_SCHEDULED} {e} {_RETRY_NOW}"
    verb = "created" if act == "add" else "updated"
    out = f"Task {saved['id']} {verb}"
    if not mine:
        # Named in the confirmation, not only in the call: this line is what the
        # model repeats to the user, and "task created" for someone else's agent
        # reads as its own.
        out += f" for agent '{aid}'"
    out += f": {saved['prompt']}\nNext run: {_when_summary(saved)}"
    if saved.get("cron"):
        out += f" (recurring, cron {saved['cron']})"
    if not is_live:
        out += f"\nNOTE: {off_note}, so this will not run until it is started.{off_fix}"
    return out
