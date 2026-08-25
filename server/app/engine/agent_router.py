"""Auto-route classifier: map one user message to the best agent id.

Backs the chat UI's "Auto" entry in the agent selector (routers/chat.py's
``_route_auto``): while it is selected, every web-chat send makes ONE bare LLM
call — no executor, no tools, the ``memory_compactor.summarize`` shape — that
reads a compact agent directory and answers with a single agent id, or
"unknown". The caller falls back to the chat's last-used agent when this
returns None, so every failure mode here (no model, timeout, garbage reply)
is simply None, never an exception.

Candidates are the agents ``enabled`` AND ``callable`` — auto-routing is
machine selection, so it honors the same opt-out as call_agent (tool-manager
and agent-manager stay a deliberate, human pick) — PLUS ``master`` even though
its seed says ``callable: false``: that flag exists to keep master out of
delegation loops, and as an ENTRYPOINT it is the right target for the general
questions (reminders, orchestration) no specialist covers.

Guard test: tests/test_auto_agent.py (pure functions + short-circuits, no
network).
"""

from __future__ import annotations

import asyncio
import logging
import re

from app import config
from app.engine import prompts
from app.engine.default_model import resolve_default
from app.engine.executor import AgentExecutor, Stores, directory_entry
from app.engine.llm_provider import LLMProvider
from app.engine.reasoning import strip_reasoning
from app.models import ChatMessage, ModelConfig
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# The routing sentinel an agent selector (web chat combo, a connector binding)
# sends instead of a real agent id. Resolved by resolve_auto BEFORE
# create_for_agent, so everything downstream sees only a concrete agent.
AUTO = "auto"

_ROUTE_TEMP = 0.0
# A router that stalls is worse than a fallback: the user is waiting for the
# ANSWER, and the classification is pure overhead on top of it.
_ROUTE_TIMEOUT = 25.0
# Chars of the user message shown to the classifier — routing needs the
# question's topic, not an essay, and this call is paid on every send.
_MESSAGE_CAP = 2000
# The id master's seed uses; kept a candidate despite callable: false (see
# module docstring).
_MASTER_ID = "master"


def route_candidates(stores: Stores) -> list[dict]:
    """Raw agent dicts the classifier may pick: enabled AND callable
    (missing flags default to True, same as executor._agent_can_call),
    plus ``master`` even when not callable."""
    out = []
    for a in stores.agents.list_all():
        if not a.get("enabled", True):
            continue
        if not a.get("callable", True) and a.get("id") != _MASTER_ID:
            continue
        out.append(a)
    return out


def parse_pick(text: str, candidate_ids: list[str]) -> str | None:
    """Forgiving parse of the classifier reply → a candidate id or None.

    Never returns an id outside ``candidate_ids``. Reasoning is stripped
    first (the same guard _validate_summary uses): this call streams straight
    into a string, so a thinking model's chain-of-thought is still inline —
    and it typically NAMES several agents while weighing them."""
    if not text:
        return None
    text = strip_reasoning(text).strip()
    if not text:
        return None
    # The happy path: the model obeyed and answered with the bare id
    # (tolerate quotes/backticks/trailing punctuation around it).
    bare = text.strip().strip("\"'`").rstrip(".").strip()
    lowered = bare.lower()
    for cid in candidate_ids:
        if lowered == cid.lower():
            return cid
    if lowered == "unknown":
        return None
    # Prose around the pick: scan for candidate ids as whole words, longest
    # ids first so "web-researcher" is matched before a hypothetical "web";
    # among the survivors the EARLIEST occurrence wins (the pick is usually
    # stated first, caveats later).
    best: tuple[int, str] | None = None
    for cid in sorted(candidate_ids, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(cid)}\b", text, re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), cid)
    return best[1] if best else None


async def pick_agent(message: str, stores: Stores, tool_registry: ToolRegistry,
                     model_override: str | None = None,
                     last_agent_id: str | None = None) -> str | None:
    """One bare LLM call → best agent id, or None (unknown / failed / skipped).

    ``last_agent_id`` (the agent that answered the chat's previous turn) is
    named in the prompt with an explicit "follow-ups stay with it" rule: a
    retry-shaped message ("riprova", "più dettagli") names no topic, and the
    sticky pick is exactly what such a message means.

    The model is the same one the turn itself would use on the default: the
    chat's ``model_override`` when set (loaded directly, mirroring
    create_for_agent), else ``resolve_default``. When neither resolves we
    return None WITHOUT raising — the fallback agent's own create_for_agent
    is where the readable "backend down" error belongs."""
    if not message or not message.strip():
        # Attachments-only send: the text tells the classifier nothing.
        return None
    candidates = route_candidates(stores)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]["id"]

    model_data = stores.models.get(model_override) if model_override else None
    if model_data is not None:
        model_config = ModelConfig(**model_data)
    else:
        try:
            model_config, _note = await resolve_default(
                stores.models, config.settings.default_model_id)
        except ValueError:
            return None

    directory = "\n".join(directory_entry(a, tool_registry) for a in candidates)
    # Only a hint the model can act on: an id outside the candidate list would
    # tell it to prefer an agent it is not allowed to answer with.
    last_line = ""
    if last_agent_id and any(a["id"] == last_agent_id for a in candidates):
        last_line = prompts.AUTO_ROUTE_LAST_AGENT.format(agent_id=last_agent_id)
    provider = LLMProvider(model_config)

    async def _collect() -> str:
        out = ""
        async for chunk in provider.chat_completion_stream(
            messages=[
                {"role": "system",
                 "content": prompts.AUTO_ROUTE_INSTRUCTION.format(
                     directory=directory, last_agent=last_line)},
                {"role": "user", "content": message[:_MESSAGE_CAP]},
            ],
            tools=None,
            temperature=_ROUTE_TEMP,
        ):
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                out += delta["content"]
        return out

    try:
        # wait_for, not asyncio.timeout: the floor is Python 3.10.
        out = await asyncio.wait_for(_collect(), _ROUTE_TIMEOUT)
    except Exception as e:
        # repr, not str: a bare TimeoutError stringifies to "" and the log
        # line would name no cause at all.
        log.warning("auto-route classification failed: %r", e)
        return None
    finally:
        await provider.close()
    return parse_pick(out, [a["id"] for a in candidates])


def _usable(stores: Stores, agent_id: str | None) -> bool:
    if not agent_id or agent_id == AUTO:
        return False
    data = stores.agents.get(agent_id)
    return data is not None and data.get("enabled", True)


async def resolve_auto(agent_id: str, message: str, session: dict, stores: Stores,
                       tool_registry: ToolRegistry,
                       model_override: str | None = None) -> tuple[str, str | None]:
    """Resolve the AUTO sentinel to a concrete agent id for one turn.

    Returns ``(agent_id, note)``: the id unchanged (note None) when it is not
    the sentinel or a REAL agent named "auto" exists (literal beats sentinel —
    no behavior change for an install that already had one); otherwise the
    classifier's pick, or the fallback with a human-readable note — a silent
    fallback is a bug you find months later, same rule as the default-model
    notice. Fallback order: the session's last-used agent (``session
    ["agent_id"]``, self-maintaining because record_user_turn stamps the
    resolved id every turn) → master → first enabled agent; none at all →
    ValueError. The last-used agent is also the classifier's HINT: a
    retry-shaped message names no topic, and continuing with whoever just
    answered is what it means.

    Shared by the web chat router and the connectors plugin (a binding whose
    agent is "auto"): one resolution, two entry points."""
    if agent_id != AUTO or stores.agents.get(AUTO) is not None:
        return agent_id, None
    last = session.get("agent_id")
    fallback = last if _usable(stores, last) else None
    if fallback is None and _usable(stores, "master"):
        fallback = "master"
    if fallback is None:
        enabled = sorted(a["id"] for a in stores.agents.list_all()
                         if a.get("enabled", True))
        fallback = enabled[0] if enabled else None
    if fallback is None:
        raise ValueError("No enabled agents to route to")
    picked = await pick_agent(message, stores, tool_registry,
                              model_override=model_override,
                              last_agent_id=last if _usable(stores, last) else None)
    if picked:
        return picked, None
    return fallback, (f"Auto agent selection could not classify this message; "
                      f"answering with '{fallback}'.")


def mark_foreign(prior: list[ChatMessage], session: dict, agent_id: str) -> None:
    """Flag (ChatMessage.foreign) the history turns another agent answered.

    The executor keeps flagged turns out of the message list and quotes them
    in the system prompt — as assistant-role messages a small model imitates
    their direct answers and makes no tool calls (measured 0/3 with them
    inline, 3/3 without; a warning note alone changed nothing, and a prefix
    on those messages got imitated instead) — while still handing them back
    in the conversation it returns, so the stored history stays whole. The
    flag is prompt-time only; nothing here is persisted.

    Attribution: record_user_turn stamps each user message with the resolved
    agent, and conversation[] groups as one user entry + its answers. The two
    lists are aligned by the user TEXT, walking messages[] forward: the
    conversation is a window/splice of the full dialogue, and a turn stopped
    or errored before its reply exists only in messages[], so a positional
    alignment (from either end) attributes turns to the wrong agent. The
    channel prefix ("[sender]\n") makes it a containment test, not equality.
    A user entry that matches nothing means a damaged session (an older
    build's leftovers): flag nothing rather than guess. Callers decide WHEN to
    call this (the web router only in Auto mode — a manual agent switch keeps
    its verbatim history; a channel turn only when its binding is on Auto)."""
    users = [(m.get("text") or "", m.get("agent_id"))
             for m in session.get("messages", []) if m.get("role") == "user"]
    flags: list[bool] = []
    j = 0
    turn_foreign = False
    for m in prior:
        if m.role == "user" and not AgentExecutor.is_scaffolding_message(
                "user", m.content):
            # (text-protocol "TOOL RESULTS:" turns are user-role too, and
            # have no counterpart in messages[])
            content = m.content if isinstance(m.content, str) else ""
            while j < len(users) and not (users[j][0] and users[j][0] in content):
                j += 1
            if j >= len(users):
                return  # no counterpart: leave the history untouched
            a = users[j][1]
            j += 1
            turn_foreign = bool(a) and a not in (AUTO, agent_id)
        flags.append(turn_foreign)
    for m, f in zip(prior, flags):
        if f:
            m.foreign = True
