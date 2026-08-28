from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.engine import embedding, prompts, trace
from app.models import Agent, ChatMessage, ChatResponse, ModelConfig
from app.engine.default_model import resolve_default
from app.engine.llm_provider import LLMProvider
from app.engine.reasoning import ReasoningSplitter
from app.engine.toolcall_parser import parse_tool_calls_from_text
from app.tools import resources as resource_channel
from app.tools.registry import ToolRegistry
from app.storage.attachments import store_attachment
from app.storage.memory import MemoryStore
from app.storage.sessions import DELEGATION_TOOL, now_iso as _now_iso
from app.storage.store import JsonStore

log = logging.getLogger(__name__)


# How many of a delegatable agent's tools the "Available Agents" directory names
# before summarizing the rest. An MCP wildcard (mcp:<server>/*) can expand to
# dozens, and this block goes into a router agent's prompt on every single turn.
_DIRECTORY_TOOLS_PER_AGENT = 12

# How many contact names notify_user's 'to' parameter lists as an enum. Above this
# the enum is dropped entirely and the model goes back to discovering names from the
# tool's error, which lists candidates too: the text protocol renders an enum
# truncated to 20 values with no sign that it was cut (see _build_tools_prompt), and
# a silently shortened address book is worse than none — the missing name is
# unaskable rather than merely unknown.
_NOTIFY_TARGETS_MAX = 20

# The tools that act on an agent's own schedule, and therefore the ones that gain
# an ``agent_id`` parameter when Agent.schedule_others is on. Both, not just
# manage_tasks: a task on an agent whose live switch is off never runs, so being
# able to schedule for someone without being able to start them is half a
# capability. Their tool.json says "your own" — _with_scheduling_targets amends
# the description in the same pass that adds the parameter, so the schema and the
# prose can never disagree.
_SCHEDULING_TOOLS = ("manage_tasks", "autonomy_control")

# Cap (chars) on the quoted transcript of history turns another agent answered
# (see _split_history). Generous on purpose: the user's rule is that the agent
# sees the whole conversation, and this is the channel those turns travel on.
_FOREIGN_CONTEXT_MAX = 6000

# Every injected string lives in app.engine.prompts — see its docstring for why
# the scaffolding markers in particular must have exactly one definition.
_FORCE_ANSWER_PROMPT = prompts.FORCE_ANSWER
_MALFORMED_CALL_PROMPT = prompts.MALFORMED_CALL
_REPEATED_CALLS_PROMPT = prompts.REPEATED_CALLS
#: After this many calls to the SAME tool in one turn, each further result
#: carries prompts.REPEATED_TOOL_NOTE. 3 because two pages of a document is
#: normal reading and ten is the loop this exists for.
_REPEAT_TOOL_NUDGE_AT = 3
_MAX_MALFORMED_RETRIES = 2


def _tool_call_args(tc: dict) -> dict:
    """A tool call's arguments as a dict — {} when they don't parse."""
    raw = tc["function"].get("arguments", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return args if isinstance(args, dict) else {}


def _step_summary(step: dict) -> dict:
    """The lightweight projection of a trace step used for SSE tool_result
    events and ChatResponse.tool_results (full results stay in the trace).
    `resources` rides along when present: it IS the lightweight form (path +
    metadata, never content) and the UI renders it live from this event."""
    out = {k: step[k] for k in ("tool", "arguments", "result_preview")}
    if step.get("resources"):
        out["resources"] = step["resources"]
    return out


def _tool_call_key(tc: dict) -> tuple[str, str]:
    """Normalize a tool call into a hashable (name, args) key for dedup."""
    name = tc["function"]["name"]
    raw = tc["function"].get("arguments", "{}")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
        # For call_agent, dedup by (agent_id, message): the SAME agent may be
        # called again with a DIFFERENT message (multi-step orchestration), but
        # an identical repeat is still skipped (prevents model looping).
        if name == "call_agent" and isinstance(args, dict) and "agent_id" in args:
            return (name, json.dumps(
                {"agent_id": args["agent_id"], "message": args.get("message", "")},
                sort_keys=True,
            ))
        return (name, json.dumps(args, sort_keys=True))
    except (json.JSONDecodeError, TypeError):
        return (name, str(raw))


def directory_entry(a: dict, tool_registry: ToolRegistry) -> str:
    """One ``- {id}: {desc} [tools: ...]`` line for a raw agent dict — shared
    by the delegation directory (_build_agents_directory) and the auto-route
    classifier (engine.agent_router), so the two never describe the same agent
    differently. Tool resolution goes through ``get_definitions_for_agent()``,
    the same call the agent's own turn uses: disabled or uninstalled tools drop
    out, and MCP wildcards are expanded — a tool the agent cannot actually run
    is not a capability."""
    desc = a.get("description") or a.get("name", "")
    declared = a.get("tools") or []
    defs = tool_registry.get_definitions_for_agent(declared)
    if defs:
        # Capped: one MCP wildcard can expand to dozens of tools.
        # The reader only needs enough to choose an agent.
        shown = [d["id"] for d in defs[:_DIRECTORY_TOOLS_PER_AGENT]]
        extra = len(defs) - len(shown)
        can = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
    elif declared:
        # Declared but unresolvable: an MCP server that is down, or a
        # deleted tool folder. Say so instead of claiming it has none.
        can = "none reachable right now"
    else:
        can = "no tools — answers from its own model"
    return f"- {a['id']}: {desc} [tools: {can}]"


@dataclass
class Stores:
    agents: JsonStore
    models: JsonStore
    # Per-agent long-term memory (None = memory subsystem not wired, e.g. in tests).
    # Carried here so sub-agents inherit the pointer via create_for_agent —
    # access is still gated per-agent by Agent.memory_enabled.
    memory: MemoryStore | None = None


class AgentExecutor:
    """Executes an agent: LLM call -> tool calls -> feed results -> repeat.

    run_stream() is the single implementation of the loop (an async generator
    of SSE-ready event dicts); run() drains it and returns the final
    ChatResponse for non-streaming callers (POST /api/chat, call_agent).
    """

    def __init__(
        self,
        agent: Agent,
        model_config: ModelConfig,
        tool_registry: ToolRegistry,
        stores: Stores,
        depth: int = 0,
    ):
        self.agent = agent
        self.model_config = model_config
        self.provider = LLMProvider(model_config)
        self.tool_registry = tool_registry
        self.stores = stores
        self.depth = depth
        # Live sub-agent events (agent_event envelopes pushed by
        # call_agent_handler's event_sink); drained by _execute_streaming's
        # race WHILE the call_agent tool task runs, so the user watches the
        # sub-agent work instead of a frozen spinner.
        self._sub_events: asyncio.Queue[dict] = asyncio.Queue()
        # Full traces of sub-agents invoked via call_agent this turn, in call
        # order. Consumed (popped) when building the parent's trace steps so the
        # whole flow is captured recursively.
        self._sub_traces: list[dict] = []
        # Attachments of the CURRENT user turn, kept so call_agent can forward
        # them to a sub-agent by index (see call_agent_handler). The tool-call
        # JSON only carries small indices — never the base64 blobs themselves.
        self._turn_attachments: list[dict] = []
        # What sub-agents reported in the PREVIOUS turns of this chat
        # (storage.sessions.delegation_history), handed in per run by whoever
        # owns the session — the executor stays session-agnostic, exactly like
        # it does for memory_context. Quoted verbatim in the system prompt and
        # readable in full through recall_delegation.
        self._delegations: list[dict] = []
        # What THIS agent's own tools returned in the PREVIOUS turns
        # (storage.sessions.tool_history). Same reason as _delegations one
        # level down: a `tool` message never survives into conversation[], so
        # without this the model answers a follow-up from its own past PROSE.
        self._tool_results: list[dict] = []
        # Turn-scoped system-prompt suffix (memory digest + findings +
        # attachments manifest), re-appended when the no-tools fallback
        # rebuilds the prompt.
        self._system_suffix: str = ""
        # Set by create_for_agent when the turn is running on a fallback model
        # instead of the configured default. Emitted once, as an SSE `notice`:
        # a silent substitution is a bug the user discovers months later.
        self.notice: str | None = None
        # The chat's per-request model pick (ChatRequest.model_override),
        # carried so call_agent propagates it to "default" sub-agents.
        self.model_override: str | None = None

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _build_trace(self, reply: str, iterations: int, steps: list[dict]) -> dict:
        """Assemble this agent's full execution trace."""
        # The RESOLVED model, not agent.model_id: every seed agent ships with
        # the "default" sentinel, so recording the agent's field would tell
        # the reader of a trace nothing about what actually served the turn.
        return {
            "agent_id": self.agent.id,
            "model_id": self.model_config.id,
            "iterations": iterations,
            "reply": reply,
            "steps": steps,
        }

    @staticmethod
    def _is_degenerate_reply(text: str) -> bool:
        """A final reply that is empty or just one of the plumbing markers means
        the model looped on tool calls (or echoed a marker) instead of answering.

        One of the two places that must recognize the markers the text-protocol
        path writes; both read them from app.engine.prompts."""
        t = (text or "").strip()
        return (not t) or t.startswith(prompts.ASSISTANT_MARKER_PREFIXES)

    @staticmethod
    def _reasoning_events(pieces: tuple[str, str, bool], answer: str, sep: str):
        """Turn one ReasoningSplitter output into SSE events.

        Returns ``(events, answer, reasoning_added, sep)``. Shared by the two
        streaming loops (a normal round and the forced synthesis) so they can't
        drift apart. ``sep`` separates the thinking of consecutive rounds, and
        is consumed by the first reasoning piece that actually appears."""
        answer_piece, reason_piece, reclassify = pieces
        events: list[dict] = []
        if reclassify and answer:
            # The chat template prefilled `<think>`: what we already streamed as
            # the answer was the model still thinking. Take it back.
            events.append({"type": "clear_tokens"})
            reason_piece = answer + reason_piece
            answer = ""
        added = ""
        if reason_piece:
            added = sep + reason_piece
            sep = ""
            events.append({"type": "reasoning", "data": added})
        if answer_piece:
            answer += answer_piece
            events.append({"type": "token", "data": answer_piece})
        return events, answer, added, sep

    def _synthesis_messages(self, messages: list[dict],
                            trace_steps: list[dict] | None = None) -> list[dict]:
        """Messages for a forced, no-tool answer: drop empty/dangling turns and
        append the instruction to answer from the tool results above.

        Tool results are RESTORED from `trace_steps`, which always holds the full
        text (_make_step), because a turn that looped on tools is both the one
        that triggers _fit_context and the one that needs this pass — writing the
        final answer from our own stubs would trade an expensive correct answer
        for a cheap vague one, in exactly the case this was built for. There is
        room: dropping `tools` frees the whole schema reserve.
        """
        full = {}
        for st in trace_steps or []:
            if isinstance(st.get("result"), str):
                full.setdefault(st.get("tool"), []).append(st["result"])
        pending = {k: list(v) for k, v in full.items()}
        clean = []
        for m in messages:
            if not (m.get("content") or m.get("tool_calls")):
                continue
            if (trace_steps and LLMProvider.is_tool_scaffolding(m)
                    and isinstance(m.get("content"), str)
                    and "… [cut at " in m["content"]):
                # Restore in order: the steps were recorded in the order their
                # results were appended, so the n-th stub of a tool maps to the
                # n-th step of that tool.
                tool = next((t for t in pending if t and t in m["content"]), None)
                if tool and pending[tool]:
                    restored = pending[tool].pop(0)
                    prefix = (prompts.TOOL_RESULTS_PREFIX + "\n"
                              if m.get("role") == "user" else "")
                    m = {**m, "content": prefix + restored}
            clean.append(m)
        return clean + [{"role": "user", "content": _FORCE_ANSWER_PROMPT}]

    def _synthesis_temp(self):
        return (self.agent.response_temperature
                if self.agent.response_temperature is not None else self.agent.temperature)

    def _make_step(self, tool: str, arguments, result: str,
                   resources: list[dict] | None = None) -> dict:
        """Build one rich trace step (full result + nested sub-agent trace for
        call_agent). Consumes the next queued sub-trace when tool == call_agent."""
        step = {
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "result_preview": (result or "")[:200],
            "ts": _now_iso(),
        }
        if resources:
            step["resources"] = resources
        if tool == "call_agent" and self._sub_traces:
            step["sub_trace"] = self._sub_traces.pop(0)
        return step

    def _debug_log(self, msg: str):
        """Append one line to the trace file, when tracing is on.

        config.debug_enabled() is asked EVERY time rather than cached: the
        switch lives in Settings and must take effect on the next turn, without
        a restart — a debug switch that needs one is useless exactly when you
        reach for it.
        """
        trace.log(msg)

    def _debug_reset(self, user_message: str):
        """Open a new TURN block in the narrative log.

        Appends — it used to truncate, so the file could only answer "what did
        the LAST turn do", while tracing is mostly opened to explain something
        that already happened.
        """
        if self.depth != 0:
            return
        trace.begin_turn(
            f"TURN  agent '{self.agent.id}'"
            f"  model {getattr(self.model_config, 'id', '?')}"
            f" ({getattr(self.model_config, 'model', '?')})",
            f"User: {trace.content(user_message)}")

    @classmethod
    async def create_for_agent(
        cls,
        agent_id: str,
        tool_registry: ToolRegistry,
        stores: Stores,
        depth: int = 0,
        model_override: str | None = None,
    ) -> AgentExecutor:
        agent_data = stores.agents.get(agent_id)
        if agent_data is None:
            raise ValueError(f"Agent not found: {agent_id}")
        agent = Agent(**agent_data)

        # Resolve the model. The sentinel "default" (or an unset model_id) means
        # "use the default model configured in Settings". Read config.settings
        # live — it's reassigned when settings are updated.
        #
        # ``model_override`` is a per-chat substitute for the DEFAULT only (the
        # web UI's chat selector): an agent pinned to a specific model keeps it
        # — pinning is the stronger, per-agent choice. The override is loaded
        # directly, no resolve_default fallback and no notice: it is an explicit
        # pick, and silently swapping an explicit pick is exactly what the
        # notice machinery exists to prevent.
        #
        # An agent on the default otherwise goes through resolve_default, which
        # falls back to a reachable local model when the configured one is down
        # or gone — otherwise a fresh install (default: llama.cpp on :8080)
        # fails on the first message even with Ollama running. It never writes
        # settings, so the user's choice comes back the moment their backend
        # does.
        model_id = agent.model_id
        notice: str | None = None
        if model_id in ("", "default") and model_override:
            model_data = stores.models.get(model_override)
            if model_data is None:
                known = ", ".join(sorted(
                    d.get("id", "") for d in stores.models.list_all())) or "none"
                raise ValueError(
                    f"Model '{model_override}' (picked for this chat) no longer "
                    f"exists. Pick another model, or set the chat back to the "
                    f"default (available: {known})."
                )
            model_config = ModelConfig(**model_data)
        elif model_id in ("", "default"):
            model_config, notice = await resolve_default(
                stores.models, config.settings.default_model_id)
        else:
            model_data = stores.models.get(model_id)
            if model_data is None:
                known = ", ".join(sorted(
                    d.get("id", "") for d in stores.models.list_all())) or "none"
                raise ValueError(
                    f"Model '{model_id}' no longer exists. Point this agent at "
                    f"an existing model, or set it back to the default "
                    f"(available: {known})."
                )
            model_config = ModelConfig(**model_data)

        executor = cls(agent, model_config, tool_registry, stores, depth)
        executor.notice = notice
        # Carried even when THIS agent is pinned (and the override didn't
        # apply): call_agent passes it down, so a "default" sub-agent behind a
        # pinned router still follows the chat's model choice.
        executor.model_override = model_override
        return executor

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tools_prompt(tool_defs: list[dict]) -> str:
        """Build a text description of available tools for models that don't
        support native tool calling. Instructs the model to respond with JSON."""
        if not tool_defs:
            return ""
        lines = [
            prompts.SECTION_TOOLS + prompts.TOOLS_PROTOCOL
        ]
        for td in tool_defs:
            schema = td.get("parameters", {})
            params = schema.get("properties", {})
            required = set(schema.get("required", []))
            parts = []
            for pname, pinfo in params.items():
                opt = "" if pname in required else ", optional"
                desc = pinfo.get("description", "")
                # Enums are constraints the model must see in text mode (the
                # native path carries them in the schema itself).
                if isinstance(pinfo.get("enum"), list) and pinfo["enum"]:
                    values = "|".join(str(v) for v in pinfo["enum"][:20])
                    desc = (desc + " " if desc else "") + f"(one of: {values})"
                parts.append(f'{pname} ({pinfo.get("type", "string")}{opt}): {desc}')
            params_desc = ", ".join(parts)
            # Concrete example using this tool's real parameter names so small
            # models don't copy a generic placeholder key like "param".
            example_keys = list(required) or list(params.keys())[:1]
            example_args = {k: "..." for k in example_keys}
            example = json.dumps({"name": td["id"], "arguments": example_args}, ensure_ascii=False)
            lines.append(
                f"- **{td['id']}**: {td.get('description', '')}. "
                f"Parameters: {params_desc}\n  Example: {example}"
            )
        return "\n".join(lines)

    @staticmethod
    def _agent_can_call(caller_agent, target: dict) -> bool:
        """Whether ``caller_agent`` may delegate to ``target`` (a raw agent dict).

        Single source of truth for the delegation gate: the target must be
        enabled, marked ``callable`` (an absolute opt-out — even an explicit
        allowlist entry can't override it), not the caller itself, and either the
        caller allows all (``["*"]``) or lists the target's id explicitly."""
        if not target.get("enabled", True):
            return False
        if not target.get("callable", True):
            return False
        if target.get("id") == caller_agent.id:
            return False
        # Missing attribute ⇒ default ["*"] (back-compat). An *explicit* empty
        # list means "delegate to nobody" — don't coalesce it to ["*"].
        allow = getattr(caller_agent, "callable_agents", None)
        if allow is None:
            allow = ["*"]
        return "*" in allow or target.get("id") in allow

    def _delegation_targets(self) -> list[dict]:
        """Raw agent dicts this agent is allowed to delegate to."""
        return [a for a in self.stores.agents.list_all() if self._agent_can_call(self.agent, a)]

    @staticmethod
    def _agent_can_schedule(caller_agent, target: dict) -> bool:
        """Whether ``caller_agent`` may manage ``target``'s tasks and live switch.

        Single source of truth for the scheduling gate, the way _agent_can_call is
        for delegation: the caller must hold ``schedule_others``, and the target
        must be enabled, ``callable`` and someone else. ``callable`` is honored
        because scheduling a task IS making that agent run later — the same
        absolute opt-out delegation respects. Deliberately NOT filtered through
        ``callable_agents``: that list is the delegation allowlist, edited in
        another tab next to the switch that grants call_agent, and reusing it
        would make this flag silently inert for every agent that delegates to
        nobody (the default for agents created in the UI)."""
        if not getattr(caller_agent, "schedule_others", False):
            return False
        if not target.get("enabled", True):
            return False
        if not target.get("callable", True):
            return False
        return target.get("id") != caller_agent.id

    def _scheduling_targets(self) -> list[dict]:
        """Raw agent dicts whose schedule this agent may manage."""
        return [a for a in self.stores.agents.list_all()
                if self._agent_can_schedule(self.agent, a)]

    def _granted_tools(self) -> set[str]:
        """The agent's tool grants with group wildcards (``<category>/*``)
        expanded — the wildcard-aware form of ``x in self.agent.tools``."""
        return set(self.tool_registry.expand_tool_ids(self.agent.tools))

    def tool_env_overrides(self) -> dict[str, str]:
        """Per-agent environment for tool subprocesses.

        ONLY what the AGENT decides, never what the install decides: the
        resolved runtime paths come from config.tool_env() once at startup,
        and MYAGENT_WORKSPACE deliberately stays global — the resource channel
        (tools/resources.py, routers/files.py) has exactly one root, and a
        second one would make every [[resource:...]] marker written outside it
        vanish silently.

        An agent with nothing set returns {}, so its tools see the same
        environment they see today, key for key.

        The registry calls this once per tool execution (it holds executor=self
        already), which is also why the value is always fresh: nothing here is
        cached in the process-wide _tool_env.
        """
        env: dict[str, str] = {}
        folder = getattr(self.agent, "folder", None)
        if folder is not None and folder.path:
            env["MYAGENT_AGENT_DIR"] = folder.path
            if folder.index_ocr:
                env["MYAGENT_INDEX_OCR"] = "1"
        # Local-only, resolved per turn: app.engine.embedding owns that rule,
        # and IndexService reads the SAME one when it builds the index.
        env.update(embedding.resolve_embed_env(self.stores.models))
        return env


    # ------------------------------------------------------------------
    # Public surface for internal tool handlers (app.tools.internal).
    # Handlers receive executor=self on dispatch; these methods are their
    # contract — they must never reach into _private state directly.
    # ------------------------------------------------------------------

    @property
    def turn_attachments(self) -> list[dict]:
        """The current user turn's attachments (what call_agent may forward)."""
        return self._turn_attachments

    @property
    def delegations(self) -> list[dict]:
        """Past turns' sub-agent replies (what recall_delegation serves)."""
        return self._delegations

    def can_call(self, target: dict) -> bool:
        """Whether THIS agent may delegate to ``target`` (a raw agent dict)."""
        return self._agent_can_call(self.agent, target)

    def scheduling_target_ids(self) -> list[str]:
        """Ids of the agents whose tasks/live switch this agent may manage —
        exactly the enum the schema advertises, so manage_tasks and
        autonomy_control enforce what the model was offered."""
        return sorted(a["id"] for a in self._scheduling_targets())

    def record_sub_trace(self, trace: dict) -> None:
        """Queue a called agent's full trace; consumed by the next call_agent
        step so the whole multi-agent flow is persisted recursively."""
        self._sub_traces.append(trace)

    def push_sub_event(self, event: dict) -> None:
        """Queue one wrapped sub-agent event ({"type": "agent_event", ...})
        for live forwarding; drained by _execute_streaming while the
        call_agent tool task runs (the envelope is built in
        internal._forward_sub_event, the only producer)."""
        self._sub_events.put_nowait(event)

    async def _execute_streaming(self, func_name: str, func_args: dict):
        """Run one tool concurrently and yield ("event", ev) for every
        sub-agent event pushed while it runs; the final item is
        ("result", str).

        tool_registry.execute() never raises (its outer guard returns an
        ERROR string), so task.result() is safe. Cancellation (Stop button →
        GeneratorExit/CancelledError at our await) must reach the tool task,
        or a delegated sub-agent would keep generating headless after the
        user stopped the turn — hence the finally.
        """
        task = asyncio.create_task(
            self.tool_registry.execute(func_name, func_args, executor=self))
        try:
            while True:
                getter = asyncio.create_task(self._sub_events.get())
                done, _ = await asyncio.wait(
                    {task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    # If task finished in the same wait, the next iteration's
                    # wait returns immediately — no event is ever lost.
                    yield ("event", getter.result())
                    continue
                getter.cancel()
                try:
                    await getter
                except asyncio.CancelledError:
                    pass
                break
            # Flush events queued between the last wait wake-up and now.
            while not self._sub_events.empty():
                yield ("event", self._sub_events.get_nowait())
            yield ("result", task.result())
        finally:
            if not task.done():
                task.cancel()

    def _build_agents_directory(self) -> str:
        """Compact directory of the agents this one may call: one line per
        agent — description + tool ids, nothing else. An agent can only act
        through the tools it holds, so that list is the part of "what it can
        do" the caller can rely on (a description is hand-written and may be
        vague, stale or empty). Line formatting (including tool resolution)
        lives in the module-level ``directory_entry``, shared with the
        auto-route classifier. This block lands in the router's prompt on
        every turn, so it stays as small as it can be."""
        entries = [directory_entry(a, self.tool_registry)
                   for a in self._delegation_targets()]
        if not entries:
            return ""
        return "\n".join([
            prompts.SECTION_AGENTS.rstrip("\n"),
            prompts.AGENTS_PREAMBLE,
            *entries,
        ])

    def _with_delegation_targets(self, tool_defs: list[dict]) -> list[dict]:
        """Pin call_agent's ``agent_id`` to the ids this agent may reach.

        The prompt directory is advisory; constraining the schema is what keeps a
        model from inventing an id and burning an iteration on "agent not found".
        Definitions are cached by the registry, so the entry is deep-copied."""
        ids = sorted(a["id"] for a in self._delegation_targets())
        if not ids:
            return tool_defs
        out = []
        for td in tool_defs:
            if td.get("id") != "call_agent":
                out.append(td)
                continue
            td = copy.deepcopy(td)
            prop = td.get("parameters", {}).get("properties", {}).get("agent_id")
            if isinstance(prop, dict):
                prop["enum"] = ids
                # The ids live in the enum and in the Available Agents block —
                # repeating them here would be a third copy on every turn.
                prop["description"] = "Agent to call (see Available Agents)"
            out.append(td)
        return out

    def _with_scheduling_targets(self, tool_defs: list[dict]) -> list[dict]:
        """Add the optional ``agent_id`` to manage_tasks / autonomy_control when
        this agent may act on others, pinned to the ids it may reach.

        The parameter is INJECTED rather than declared in tool.json: without the
        grant the schema must not even mention it, or every agent would be told
        about a capability its handler refuses — and a small model reads a
        parameter as an invitation. With the grant, the enum is what keeps it from
        inventing an id, exactly as for call_agent. Definitions are cached by the
        registry, so each entry is deep-copied before it is touched."""
        ids = self.scheduling_target_ids()
        if not ids:
            return tool_defs
        out = []
        for td in tool_defs:
            if td.get("id") not in _SCHEDULING_TOOLS:
                out.append(td)
                continue
            td = copy.deepcopy(td)
            props = td.setdefault("parameters", {}).setdefault("properties", {})
            props["agent_id"] = {
                "type": "string",
                "enum": ids,
                # What it is FOR, first, and the trigger phrase second. Measured
                # with a local model: a description opening with "Omit to act on
                # yourself" got exactly that — asked to "schedule X for agent
                # worker" it dropped the parameter and created the task on itself,
                # then reported success. A small model acts on the head of a
                # sentence, so the condition has to come before the exception.
                "description": "Which agent this call is about. Required whenever "
                               "the request names another agent ('schedule … for "
                               "agent X', 'start X'); leave it out only when the "
                               "request is about yourself.",
            }
            # The tool.json prose says "your own"; left alone it contradicts the
            # parameter that was just added.
            td["description"] = (td.get("description", "").rstrip()
                                 + " If the request names another agent, pass its "
                                   "id as agent_id — without it this acts on you.")
            out.append(td)
        return out

    def _with_notify_targets(self, tool_defs: list[dict]) -> list[dict]:
        """Pin notify_user's ``to`` to the names in the address book.

        Same trade as ``_with_delegation_targets``: a handful of names in the schema
        costs ~15 tokens and lands the message in one call, where the alternatives
        cost more. A separate "list the contacts" tool would carry its own
        definition in every payload — more tokens than the enum it replaces — plus
        an iteration a small model tends to skip; a prompt section costs more than
        the enum for the same names and constrains nothing.

        The names come from a plugin, through the registry, and may be absent: no
        contacts (or too many) leaves the schema exactly as it is, and the tool's
        error still lists candidates. Definitions are cached by the registry, so the
        entry is deep-copied before it is touched.
        """
        provider = getattr(self.tool_registry, "notify_targets", None)
        if provider is None:
            return tool_defs
        targets = provider() or {}
        names = [n for n in (targets.get("contacts") or []) if n]
        channels = [c for c in (targets.get("channels") or []) if c]
        if not names or len(names) > _NOTIFY_TARGETS_MAX:
            names = []
        if not names and not channels:
            return tool_defs
        # Broadcast last: given a list, a small model reaches for the first value,
        # and "tell everyone" is the one choice that must be asked for.
        broadcast = targets.get("broadcast") or ""
        if names and broadcast and len(names) > 1:
            names = [*names, broadcast]
        out = []
        for td in tool_defs:
            if td.get("id") != "notify_user":
                out.append(td)
                continue
            td = copy.deepcopy(td)
            props = td.get("parameters", {}).get("properties", {})
            if names and isinstance(props.get("to"), dict):
                props["to"]["enum"] = names
            if channels and isinstance(props.get("channel"), dict):
                props["channel"]["enum"] = channels
            out.append(td)
        return out

    @staticmethod
    def _flatten_content(content):
        """Collapse a multimodal content list to a plain string. Text parts are
        kept; images become a '[image]' marker so base64 blobs are never carried
        into conversation history (avoids resending them every turn)."""
        if not isinstance(content, list):
            return content
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif p.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(x for x in parts if x)

    # --------------------------------------------------------- attachment files
    @staticmethod
    def _decode_attachment_data(data: str) -> bytes | None:
        """Decode an attachment's ``data`` (a ``data:...;base64,`` URI or bare
        base64) to raw bytes. Returns None if it can't be decoded as binary."""
        if not data:
            return None
        b64 = data
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            return base64.b64decode(b64, validate=False)
        except Exception:
            return None

    @staticmethod
    def _prune_attachment_files(workdir, max_age: int = 86400) -> None:
        """Best-effort cleanup: drop materialized files older than max_age (24h)
        so the workspace doesn't grow without bound."""
        try:
            now = time.time()
            for f in workdir.iterdir():
                try:
                    if f.is_file() and now - f.stat().st_mtime > max_age:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _materialize_attachments(self, attachments: list[dict] | None):
        """Write each binary attachment (image/audio/other) to a file under the
        workspace and stamp its workspace-relative ``path`` on the dict, so the
        model can hand that path to a tool (e.g. document_extract) regardless of
        whether it can read the content inline. Text attachments stay inline-only
        (no path needed). Files are content-addressed (md5) so the same upload
        reused across turns keeps a stable path and isn't rewritten."""
        if not attachments:
            return attachments
        workdir = config.WORKSPACE_DIR / "_attachments"
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("cannot create attachments dir: %s", e)
            return attachments
        self._prune_attachment_files(workdir)

        out: list[dict] = []
        for att in attachments:
            att = dict(att or {})
            kind = att.get("kind")
            data = att.get("data") or ""
            # Already materialized (e.g. forwarded to a sub-agent) or plain text.
            if kind == "text" or not data or (att.get("path") and (config.WORKSPACE_DIR / att["path"]).exists()):
                out.append(att)
                continue
            raw = self._decode_attachment_data(data)
            if raw is None:
                out.append(att)
                continue
            name = att.get("name") or (kind or "file")
            path = store_attachment(raw, name, kind or "file")
            if path:
                att["path"] = path  # relative to the tool workspace cwd
            out.append(att)
        return out

    def _build_user_content(self, message: str, attachments: list[dict] | None):
        """Build the OpenAI 'content' for a user turn from message + attachments.

        Content is inlined only for modalities THIS model can read natively
        (images if supports_vision, audio if supports_audio); otherwise the file
        is left out of the content and reached via its workspace path (listed in
        the attachment manifest) using a tool. Text attachments are always folded
        in as labeled blocks. Returns a plain string when nothing is inlined, or
        a multimodal parts list otherwise."""
        if not attachments:
            return message

        vision = getattr(self.model_config, "supports_vision", True)
        audio_ok = getattr(self.model_config, "supports_audio", False)

        text_blocks: list[str] = []
        media_parts: list[dict] = []
        for att in attachments:
            kind = att.get("kind")
            name = att.get("name") or "file"
            data = att.get("data") or ""
            if not data:
                continue
            if kind == "image":
                if vision:
                    media_parts.append({"type": "image_url", "image_url": {"url": data}})
                # else: not inlined — model reads it via path (manifest)
            elif kind == "audio":
                if audio_ok:
                    b64, fmt = self._audio_inline(data, att.get("mime"))
                    if b64:
                        media_parts.append(
                            {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
                        )
                # else: not inlined — model transcribes it via path (manifest)
            elif kind == "text":
                text_blocks.append(f"[File: {name}]\n{data}")
            # other/binary kinds (e.g. pdf): never inlined — path-only

        combined = message or ""
        if text_blocks:
            combined = (combined + "\n\n" if combined else "") + "\n\n".join(text_blocks)

        if not media_parts:
            return combined

        parts: list[dict] = []
        if combined:
            parts.append({"type": "text", "text": combined})
        parts.extend(media_parts)
        return parts

    @staticmethod
    def _audio_inline(data: str, mime: str | None) -> tuple[str | None, str]:
        """Return (base64_without_prefix, format) for an OpenAI input_audio part.
        ``format`` is derived from the mime/data URI (wav, mp3, ogg, ...)."""
        b64 = data
        fmt = ""
        if b64.startswith("data:") and "," in b64:
            head, b64 = b64.split(",", 1)
            m = re.search(r"audio/([A-Za-z0-9.+-]+)", head)
            if m:
                fmt = m.group(1).lower()
        if not fmt and mime:
            m = re.search(r"audio/([A-Za-z0-9.+-]+)", mime)
            if m:
                fmt = m.group(1).lower()
        fmt = {"oga": "ogg", "x-wav": "wav", "vnd.wave": "wav", "mpeg": "mp3"}.get(fmt, fmt or "wav")
        return (b64 or None), fmt

    @classmethod
    def is_scaffolding_message(cls, role: str, content, tool_calls=None) -> bool:
        """True when a conversation entry is tool-call plumbing rather than a
        turn of the dialogue: the tool results we hand back as a user turn, the
        '(used tool: ...)' assistant marker, native tool_calls/tool messages.

        Two callers must agree on this: history injection (below, which drops
        them so the model doesn't mimic the format in later turns) and the
        session rewind endpoint (which counts real user turns to cut the stored
        history at the right place).

        Every literal comes from app.engine.prompts, the same module the writer
        side uses — a marker reworded here but not there (or vice versa) fails
        silently: the plumbing leaks into the next turn's history and rewind cuts
        in the wrong place, with nothing raised anywhere."""
        text = cls._flatten_content(content) or ""
        if role == "user":
            return (
                text.startswith((prompts.TOOL_RESULTS_PREFIX, prompts.MALFORMED_PREFIX))
                or any(s in text for s in prompts.LEGACY_USER_SUBSTRINGS)
            )
        if role == "assistant":
            return (bool(tool_calls)
                    or text.startswith(prompts.ASSISTANT_MARKER_PREFIXES
                                       + prompts.LEGACY_ASSISTANT_PREFIXES))
        return role == "tool"

    @staticmethod
    def _split_history(history: list[dict]) -> tuple[list[dict], str | None]:
        """Divide the cleaned history into (messages to send, foreign transcript).

        Entries flagged ``foreign`` (ChatMessage.foreign, set by the chat
        router in Auto mode on turns another agent answered) leave the
        message list and come back as a ``role: content`` transcript for
        prompts.SECTION_FOREIGN_HISTORY. The flag is POPPED from every entry
        here — it must reach neither the LLM payload nor the stored
        conversation. Transcript capped at _FOREIGN_CONTEXT_MAX chars, tail
        kept (a follow-up refers to the latest thing said), cut declared."""
        own: list[dict] = []
        quoted: list[str] = []
        for m in history:
            if m.pop("foreign", None):
                quoted.append(f"{m.get('role')}: {m.get('content') or ''}")
            else:
                own.append(m)
        if not quoted:
            return own, None
        transcript = "\n".join(quoted)
        if len(transcript) > _FOREIGN_CONTEXT_MAX:
            transcript = "[oldest turns omitted]\n" + transcript[-_FOREIGN_CONTEXT_MAX:]
        return own, transcript

    @classmethod
    def _clean_conversation(cls, conversation: list[ChatMessage], max_messages: int = 10) -> list[dict]:
        """Remove internal tool-call artifacts from conversation history.
        Keeps only clean user/assistant exchanges so the LLM doesn't
        mimic the '[Called tool ...]' format in follow-up turns.
        Also limits to last max_messages to avoid context overflow."""
        cleaned = []
        for msg in conversation:
            content = cls._flatten_content(msg.content) or ""
            if cls.is_scaffolding_message(msg.role, content, msg.tool_calls):
                continue
            data = msg.model_dump(exclude_none=True)
            data["content"] = content  # store flattened (string) content
            cleaned.append(data)
        # Sliding window: keep only the last N messages
        if len(cleaned) > max_messages:
            cleaned = cleaned[-max_messages:]
        return cleaned

    def _system_prompt_with_tools(self, tool_defs: list[dict]) -> str:
        """Return the system prompt, appending the text-protocol tool
        instructions ONLY when this run uses the text protocol
        (supports_tools is False). In native/auto mode the `tools` payload is
        the tool documentation — injecting the JSON instructions on top made
        models emit JSON as prose (double protocol) and cost ~650 tokens per
        turn. A server that accepts `tools` but never calls one is handled by
        the per-model override (ModelConfig.supports_tools = False)."""
        prompt = self.agent.system_prompt
        # Inject agent directory if agent can call other agents
        if "call_agent" in self._granted_tools():
            prompt += self._build_agents_directory()
        if tool_defs and self.provider.supports_tools is False:
            prompt += self._build_tools_prompt(tool_defs)
        return prompt

    def _build_attachments_manifest(self, attachments: list[dict]) -> str:
        """List this turn's attachments with index, kind, name and workspace
        PATH, plus how to reach each: read inline (if it was sent inline), read
        the file via a tool (document_extract on its path), or forward to a
        sub-agent by index (call_agent's attachment_indices)."""
        vision = getattr(self.model_config, "supports_vision", True)
        audio_ok = getattr(self.model_config, "supports_audio", False)
        granted = self._granted_tools()
        has_extract = "document_extract" in granted
        has_call = "call_agent" in granted

        hints = ["Items marked 'inline' are already included in the message — "
                 "read them directly."]
        if has_extract:
            hints.append("For an item marked 'file only' (audio, PDF, a binary "
                         "document, or an image you can't see), call `document_extract` "
                         "with its `path` to get the text or transcription.")
        if has_call:
            hints.append("You may forward one or more items to a sub-agent via "
                         "call_agent's `attachment_indices` (0-based), e.g. attachment_indices=[0].")

        lines = [prompts.SECTION_ATTACHMENTS,
                 "The user attached the item(s) below. " + " ".join(hints)]
        for i, a in enumerate(attachments or []):
            a = a or {}
            kind = a.get("kind") or "file"
            name = a.get("name") or "unnamed"
            path = a.get("path")
            inlined = (
                (kind == "image" and vision)
                or (kind == "audio" and audio_ok)
                or kind == "text"
            )
            tag = "inline" if inlined else "file only"
            extra = f" — path: {path}" if path else ""
            lines.append(f"- [{i}] {kind}: {name} ({tag}){extra}")
        return "\n".join(lines)

    def _build_memory_section(self, memory_context: list | None) -> str:
        """The '## Memory' block injected into the system prompt: the agent's
        memory.md (profile prose + Notes/Recent indexes) plus the summaries of
        this session's archived turns (session["memory"].context, filled by
        the memory compactor). Kept OUT of conversation[] so the scaffolding
        predicate and the rewind endpoint never see it. Empty unless the agent
        opted into memory."""
        if not self.agent.memory_enabled or self.stores.memory is None:
            return ""
        parts = []
        try:
            md = self.stores.memory.get_memory_md(self.agent.id)
        except Exception:
            md = ""
        if md:
            parts.append(md)
        lines = [
            f"- [{(c.get('ts') or '')[:10]}] {c['summary']}"
            for c in (memory_context or []) if c.get("summary")
        ]
        if lines:
            parts.append("### Earlier in this conversation (summarized)\n" + "\n".join(lines))
        if not parts:
            return ""
        if self._granted_tools() & {"memory_search", "memory_read"}:
            parts.append("Details are retrievable with the memory_search / memory_read tools.")
        return prompts.SECTION_MEMORY + "\n\n".join(parts)

    # How many of the past turns' delegations are quoted in the prompt, and
    # how much of each. VERBATIM and not summarized on purpose: a sub-agent
    # reply is ALREADY a summary written for this agent, and
    # summaries-of-summaries lose the facts (the reason the memory tree was
    # removed in 2026-07). Deterministic, so the model never has to decide to
    # go looking for what it was just told — that decision is where the bug
    # lived: a router that had the data answered "I have no data" the turn
    # after, because a `tool` message never survives into conversation[].
    _FINDINGS_WINDOW = 3
    # Recency-weighted budget (~1200 chars ≈ 300 tokens, whatever the window
    # holds): the question is almost always about the LAST thing an agent
    # reported, so that one gets room to arrive whole — a real sub-agent reply
    # measured 1080-1250 chars. Cut to 300 the model answered with the visible
    # half and called the rest "not detailed" instead of fetching it, twice on
    # Qwen3-VL-4B. The older ones stay one-liners: they are reminders that the
    # fact EXISTS, and recall_delegation serves them in full.
    _FINDINGS_CHARS = 800
    _FINDINGS_CHARS_OLDER = 200

    def _build_findings_section(self) -> str:
        """The '## Agent findings' block: what sub-agents reported in the
        PREVIOUS turns of this chat, quoted verbatim with any truncation
        declared (a silent cut reads as "that is all there was").

        NOT gated on memory_enabled: this is session state, not long-term
        memory — an agent that delegates needs it whether or not it keeps a
        memory. Like ## Memory it rides on the SYSTEM prompt and never enters
        conversation[], so is_scaffolding_message and the rewind endpoint are
        untouched.
        """
        window = self._delegations[-self._FINDINGS_WINDOW:]
        can_recall = "recall_delegation" in self._granted_tools()
        lines = []
        for pos, d in enumerate(reversed(window)):  # newest first, as ## Recent
            reply = " ".join((d.get("reply") or "").split())
            if not reply:
                continue
            budget = self._FINDINGS_CHARS if pos == 0 else self._FINDINGS_CHARS_OLDER
            if len(reply) > budget:
                # The way out goes AT the cut, not only in the note below: cut
                # short, the model reported the rest as "not detailed" instead
                # of fetching it (measured on Qwen3-VL-4B).
                more = (f" — recall_delegation id={d.get('id')} for the rest"
                        if can_recall else "")
                reply = (reply[:budget].rstrip()
                         + f"… [cut at {budget} of {len(reply)} chars{more}]")
            lines.append(f"- [{d.get('id')}] {d.get('agent_id') or 'agent'}: {reply}")
        if not lines:
            return ""
        hidden = len(self._delegations) - len(window)
        if hidden > 0:
            lines.append(
                f"({hidden} earlier delegation(s) not shown"
                + (" — recall_delegation lists them all)" if can_recall else ")")
            )
        notes = [prompts.FINDINGS_NOTE]
        if can_recall:
            notes.append(prompts.FINDINGS_RECALL_NOTE)
        return prompts.SECTION_FINDINGS + "\n".join(lines) + "\n" + " ".join(notes)

    # The same shape as the findings window, one level down. Smaller budget on
    # purpose: a tool result is RAW output (a search listing, a page of a PDF,
    # a command's stdout), not a reply written for this agent, so the useful
    # part is usually its head — and unlike a delegation it can be had again
    # for the price of one call.
    _TOOL_RESULTS_WINDOW = 4
    _TOOL_RESULTS_CHARS = 600
    _TOOL_RESULTS_CHARS_OLDER = 180

    # ------------------------------------------------------------------
    # In-turn demotion of tool results (occupancy-triggered)
    #
    # A turn's tool results are appended to `messages` and re-sent on EVERY
    # iteration, so the cost is quadratic: measured on one real turn, 12 tool
    # calls (2 searches plus 10 local_read pages of one PDF) cost 22 model calls
    # and ~221k input tokens, and iterations 9-13 each overflowed a 16384-token
    # window, paid an HTTP 400, and were rescued by a trim that _build_payload
    # then threw away.
    #
    # Why HERE and not in LLMProvider._trim_tool_results, which already trims at
    # the wire: the provider sees only {role, tool_call_id, content}. It does not
    # know WHICH tool produced a result or with what arguments, so all it can do
    # is head-slice a blob. The executor knows both, so its stub can say "call
    # local_read(id=…) again for the rest" — actionable instead of merely
    # shorter. The wire trim stays as the last-resort guard.
    # ------------------------------------------------------------------

    #: Newest tool results never demoted. Measured: keeping 1 gives 3.0x fewer
    #: tokens and keeping 2 gives 2.2x, but a `search -> read -> read(offset)`
    #: chain routinely needs the previous page, so 2 is the honest default.
    _CTX_KEEP_VERBATIM = 2
    #: Head left in a stub. Deliberately _TOOL_RESULTS_CHARS_OLDER, reused: the
    #: across-turn and within-turn recency policies must not drift apart.
    _CTX_DEMOTE_CHARS = _TOOL_RESULTS_CHARS_OLDER
    #: Below this saving, leave it alone — same rule as
    #: llm_provider._MIN_TRIM_CHARS: a cut that frees nothing still DECLARES
    #: itself, marking a complete result as partial. That is a lie for free.
    _CTX_MIN_DEMOTE = 400
    #: How far below the trigger one pass takes us. Hysteresis, not politeness:
    #: without a gap the very next tool result crosses the line again and we
    #: demote one message per iteration forever.
    _CTX_DEMOTE_MARGIN = 0.20
    #: Never demoted. The asymmetry is already recorded for the across-turn
    #: window: re-running a search costs one call, re-running a DELEGATION costs
    #: a whole sub-agent turn — and within this turn that reply exists nowhere
    #: else in the payload (recall_delegation serves PREVIOUS turns).
    #: DELEGATION_TOOL, not the literal "call_agent": storage.sessions owns that
    #: name and the across-turn recall keys on it too.
    _CTX_NEVER_DEMOTE = (DELEGATION_TOOL,)

    def _tool_result_index(self, messages: list[dict],
                           start: int) -> list[dict]:
        """``[{i, tool, arguments, key}]`` for each tool result of THIS turn,
        oldest first.

        A `role: tool` message carries only a `tool_call_id`, so the tool name
        and arguments are recovered from the nearest preceding assistant message
        that requested it. In the text protocol one `role: user` message carries
        the whole round, so it resolves to that round's calls as a group.
        """
        pending: dict[str, dict] = {}
        round_calls: list[dict] = []
        out: list[dict] = []
        for i in range(start, len(messages)):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                round_calls = list(m["tool_calls"])
                for tc in m["tool_calls"]:
                    pending[tc.get("id") or ""] = tc
                continue
            if not LLMProvider.is_tool_scaffolding(m):
                continue
            if m.get("role") == "tool":
                tc = pending.get(m.get("tool_call_id") or "") or {}
                calls = [tc] if tc else []
            else:
                calls = round_calls          # text protocol: one batched message
            names, args, keys = [], {}, []
            for tc in calls:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    names.append(fn["name"])
                    keys.append(_tool_call_key(tc))
                    if not args:
                        args = _tool_call_args(tc) or {}
            out.append({"i": i, "tool": ", ".join(names) or "?",
                        "names": names, "arguments": args, "keys": keys})
        return out

    def _demote_stub(self, content: str, tool: str, arguments: dict) -> str:
        """One demoted tool result, in the marker style of
        _build_tool_results_section — so the model meets ONE wording for "there
        was more of this, ask again"."""
        flat = " ".join((content or "").split())
        head = flat[:self._CTX_DEMOTE_CHARS].rstrip()
        hint = ""
        if isinstance(arguments, dict):
            raw = (arguments.get("query") or arguments.get("id")
                   or arguments.get("path") or "")
            hint = " ".join(str(raw).split())[:80]
        label = f"{tool}({hint})" if hint else str(tool)
        return (f"{head}… [cut at {len(head)} of {len(flat)} chars — call "
                f"{label} again for the rest]")

    async def _fit_context(self, messages: list[dict], openai_tools,
                           history_end: int, recallable: dict,
                           pinned: set) -> dict:
        """Demote the OLDEST tool results in place until the payload fits, and
        return the context snapshot either way (the UI gauge wants it always).

        Below the threshold NOTHING is touched — that is the point: a short turn
        pays nothing for this.
        """
        state = await self.provider.context_state(messages, openai_tools)
        fit = state["fit"]
        try:
            trigger = float(getattr(config.settings, "context_compact_at", 0.90))
        except (TypeError, ValueError):
            trigger = 0.90
        trigger = min(0.95, max(0.5, trigger))
        state["threshold"] = trigger
        if state["used"] <= fit * trigger:
            return state

        target = fit * max(0.1, trigger - self._CTX_DEMOTE_MARGIN)
        entries = self._tool_result_index(messages, history_end)
        # The newest few are what the model is reasoning FROM, and a pinned one
        # was just re-fetched on our own advice — demoting it again would make
        # the pair oscillate.
        candidates = entries[:max(0, len(entries) - self._CTX_KEEP_VERBATIM)]
        used, demoted = state["used"], 0
        ratio = self.provider.token_ratio()
        for e in candidates:
            if used <= target:
                break
            if any(n in self._CTX_NEVER_DEMOTE for n in e["names"]):
                continue
            if any(k in pinned for k in e["keys"]):
                continue
            m = messages[e["i"]]
            content = m.get("content")
            if not isinstance(content, str) or "… [cut at " in content:
                continue
            stub = self._demote_stub(content, e["tool"], e["arguments"])
            # The text protocol's batched result is recognised BY ITS PREFIX
            # (is_scaffolding_message): lose it and the stub becomes a real user
            # turn — it leaks into the next turn's history AND the rewind
            # endpoint miscounts user turns. Two silent failures.
            if m.get("role") == "user":
                stub = prompts.TOOL_RESULTS_PREFIX + "\n" + stub
            if len(content) - len(stub) < self._CTX_MIN_DEMOTE:
                continue
            # Rewrite `content` ONLY. Never delete a role:tool message: the
            # assistant turn above it carries `tool_calls`, and every
            # OpenAI-compatible endpoint 400s on a call with no paired result.
            messages[e["i"]] = {**m, "content": stub}
            used -= int((self.provider._estimate_tokens(content)
                         - self.provider._estimate_tokens(stub)) * ratio)
            demoted += 1
            # The stub says "call it again". The dedup would swallow exactly
            # that call (key already in executed_calls) and, if it was the only
            # one, end the turn mute. Grant ONE re-run per demotion: dedup
            # exists to stop loops, and a re-fetch is only a loop if the model
            # still HAS the answer in context — it no longer does.
            for k in e["keys"]:
                recallable[k] = recallable.get(k, 0) + 1
        if demoted:
            state["used"] = max(0, used)
            state["demoted"] = demoted
            self._debug_log(
                f"  CONTEXT: demoted {demoted} tool result(s) to fit "
                f"{state['used']}/{fit} tokens (window {state['window']})")
            log.info("Agent '%s': demoted %d tool result(s) to fit the context",
                     self.agent.id, demoted)
        return state

    def _build_tool_results_section(self) -> str:
        """The '## What your tools returned earlier in this chat' block.

        The twin of _build_findings_section, and it exists for the same bug:
        is_scaffolding_message drops `tool` messages from conversation[], so
        from the second turn on the model can see only its own prose. Observed
        on a real chat over a folder of medical records — the agent found the
        report, then said "yes it exists", then "I cannot find it", never
        searching again in either turn.

        Deliberately NO recall_* companion, unlike delegations: re-running a
        search costs one call, re-running a delegation costs a whole sub-agent
        turn. So a cut points at the tool itself (R3: the truncated branch
        carries the retry instruction), and the payload stays one section
        lighter.

        Not gated on memory_enabled — this is session state, not long-term
        memory — and it rides on the SYSTEM prompt like ## Memory, so
        is_scaffolding_message and the rewind endpoint are untouched.
        """
        window = self._tool_results[-self._TOOL_RESULTS_WINDOW:]
        lines = []
        for pos, t in enumerate(reversed(window)):      # newest first
            result = " ".join((t.get("result") or "").split())
            if not result:
                continue
            budget = (self._TOOL_RESULTS_CHARS if pos == 0
                      else self._TOOL_RESULTS_CHARS_OLDER)
            if len(result) > budget:
                result = (result[:budget].rstrip()
                          + f"… [cut at {budget} of {len(result)} chars — call "
                            f"{t.get('tool')} again for the rest]")
            args = t.get("arguments") or {}
            # The QUERY is what makes an entry recognisable ("did I already
            # look for the report?"), and it is short. The rest of the
            # arguments is not worth its tokens here.
            hint = args.get("query") or args.get("id") or args.get("path") or ""
            hint = " ".join(str(hint).split())[:80]
            label = f"{t.get('tool')}({hint})" if hint else str(t.get("tool"))
            lines.append(f"- [{t.get('id')}] {label}: {result}")
        if not lines:
            return ""
        hidden = len(self._tool_results) - len(window)
        if hidden > 0:
            lines.append(f"({hidden} earlier tool call(s) not shown — run the "
                         f"tool again if you need them)")
        return (prompts.SECTION_TOOL_RESULTS + "\n".join(lines) + "\n"
                + prompts.TOOL_RESULTS_NOTE)

    def _prepare_turn(self, tool_defs: list[dict], attachments: list[dict] | None,
                      memory_context: list | None = None,
                      transcribed: bool = False,
                      foreign_context: str | None = None):
        """Return (system_content, tool_defs, openai_tools) for this turn.

        When the user attaches files, append a manifest so the model knows what
        it received, which items are inline vs available only as a workspace file
        (with its `path`, to hand to document_extract), and — for router agents —
        how to forward them to a sub-agent by index."""
        if tool_defs and any(d["id"] == "call_agent" for d in tool_defs):
            tool_defs = self._with_delegation_targets(tool_defs)
        if tool_defs and any(d["id"] == "notify_user" for d in tool_defs):
            tool_defs = self._with_notify_targets(tool_defs)
        if tool_defs and getattr(self.agent, "schedule_others", False) \
                and any(d["id"] in _SCHEDULING_TOOLS for d in tool_defs):
            tool_defs = self._with_scheduling_targets(tool_defs)
        openai_tools = ToolRegistry.to_openai_format(tool_defs) if tool_defs else None
        # Turn-scoped additions (memory digest, attachments manifest) are kept
        # as a suffix on self too: the no-tools fallback rebuilds the system
        # prompt mid-loop from _system_prompt_with_tools alone and would
        # otherwise silently drop them (llama.cpp starts in that mode).
        suffix = self._build_memory_section(memory_context)
        suffix += self._build_findings_section()
        suffix += self._build_tool_results_section()
        has_attachment = bool(attachments) and any(
            (a or {}).get("data") or (a or {}).get("path") for a in attachments
        )
        if has_attachment:
            suffix += self._build_attachments_manifest(attachments)
        if transcribed:
            suffix += prompts.SECTION_VOICE
        if foreign_context:
            suffix += prompts.SECTION_FOREIGN_HISTORY.format(
                transcript=foreign_context)
        self._system_suffix = suffix
        system_content = self._system_prompt_with_tools(tool_defs) + suffix
        return system_content, tool_defs, openai_tools

    # A reply that carries the shape of a tool call: a JSON-ish object mentioning
    # the call keys or a fenced json block. Deliberately narrow — it only gates
    # the "resend that call" retry, and prose must never match.
    _CALL_SHAPE = re.compile(r'"name"\s*:|"arguments"\s*:|"parameters"\s*:|```json', re.I)

    def _looks_like_tool_call(self, content: str, tool_defs: list[dict]) -> bool:
        """True when the model clearly ATTEMPTED a tool call that failed to parse:
        the text has the call shape and names one of this agent's tools."""
        text = (content or "").strip()
        if "{" not in text or not self._CALL_SHAPE.search(text):
            return False
        return any(d["id"] in text for d in tool_defs)

    def _parse_text_tool_calls(self, content: str, tool_defs: list[dict]) -> list[dict] | None:
        """Fallback parsing of tool calls from plain text (delegates to
        toolcall_parser with this executor's context).

        Scoped to THIS agent's definitions: that is the only way a text-mode
        model can reach an MCP tool (they are not in get_all_definitions), and it
        stops such a model from invoking tools its agent was never given."""
        definitions = {d["id"]: d for d in tool_defs}
        agent_ids_provider = None
        if "call_agent" in definitions:
            agent_ids_provider = lambda: {
                a["id"] for a in self.stores.agents.list_all()
                if self._agent_can_call(self.agent, a)
            }
        return parse_tool_calls_from_text(content, definitions, agent_ids_provider)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run(
        self,
        user_message: str,
        conversation: list[ChatMessage] | None = None,
        attachments: list[dict] | None = None,
        memory_context: list | None = None,
        event_sink=None,
        transcribed: bool = False,
        delegations: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ) -> ChatResponse:
        """Non-streaming entry point: drain run_stream() and return the final
        ChatResponse (single implementation of the loop lives there).

        ``event_sink`` (optional callable) receives every non-``done`` event
        as it happens — how call_agent forwards a sub-agent's live activity
        to the parent's stream. ``error`` is both sinked (shown live) and
        captured (drives the degenerate-reply repair below)."""
        response: ChatResponse | None = None
        error: str | None = None
        async for event in self.run_stream(user_message, conversation, attachments,
                                           memory_context, transcribed=transcribed,
                                           delegations=delegations,
                                           tool_results=tool_results):
            et = event.get("type")
            if et == "done":
                response = ChatResponse(**event.get("data", {}))
                continue
            if et == "error":
                error = str(event.get("data") or "unknown error")
            if event_sink:
                event_sink(event)

        if response is None:
            # Defensive: run_stream always ends with 'done', but never return None
            reply = f"ERROR: LLM call failed: {error}" if error else "ERROR: no response"
            return ChatResponse(reply=reply, trace=self._build_trace(reply, 0, []))

        if error and self._is_degenerate_reply(response.reply):
            response.reply = f"ERROR: LLM call failed: {error}"
            if response.trace is not None:
                response.trace["reply"] = response.reply
        return response

    async def run_stream(
        self,
        user_message: str,
        conversation: list[ChatMessage] | None = None,
        attachments: list[dict] | None = None,
        memory_context: list | None = None,
        transcribed: bool = False,
        delegations: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ):
        """The agent loop, as an async generator of SSE-compatible event dicts.

        ``transcribed`` marks a message that arrived as speech and was
        machine-transcribed (voice satellite, Telegram voice note): the system
        prompt gains a turn-scoped note (prompts.SECTION_VOICE) telling the
        model to ask briefly for a repeat instead of guessing at a garbled
        transcript.

        History entries marked ``ChatMessage.foreign`` (turns answered by
        OTHER agents — the chat's Auto selector routes per message) are kept
        out of the message list and quoted in the system prompt instead
        (prompts.SECTION_FOREIGN_HISTORY): as assistant-role messages they
        read as direct answers (the history strips tool scaffolding) and a
        small model imitates them instead of using its tools — measured 0/3
        tool calls with them inline vs 3/3 without, and a warning note alone
        changed nothing. They ARE still returned in ``conversation`` (in
        place, flag stripped), so the caller's stored history stays whole.

        Event types: token, clear_tokens, tool_start, tool_result, notice,
        error, done, plus agent_event — a delegated sub-agent's live activity,
        wrapped as {"path": [agent ids from the top level down], "event":
        {inner event}}. The path is flattened (never nested envelopes): each
        delegation level prepends its sub-agent's id.
        """
        try:
            # Depth 0 only: every sub-agent resolves the same fallback from the
            # same memo, so a delegating turn would otherwise emit one identical
            # notice per sub-agent.
            if self.notice and self.depth == 0:
                yield {"type": "notice", "data": self.notice}
            async for event in self._run_stream_inner(user_message, conversation,
                                                      attachments, memory_context,
                                                      transcribed=transcribed,
                                                      delegations=delegations,
                                           tool_results=tool_results):
                yield event
        finally:
            # Runs on normal completion AND on generator abort (Stop button /
            # client gone -> GeneratorExit), so the httpx client never leaks.
            await self.provider.close()

    async def _run_stream_inner(
        self,
        user_message: str,
        conversation: list[ChatMessage] | None,
        attachments: list[dict] | None,
        memory_context: list | None = None,
        transcribed: bool = False,
        delegations: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ):
        self._debug_reset(user_message)
        # What sub-agents reported in the PREVIOUS turns (this turn's own
        # delegations are still in `messages` below, so they need no recall).
        # Sub-agents get nothing: call_agent_handler runs them without this
        # argument — between agents only message/reply travel.
        self._delegations = list(delegations or [])
        # Same contract: handed in per run by whoever owns the session, so the
        # executor stays session-agnostic. Sub-agents get nothing here either.
        self._tool_results = list(tool_results or [])
        # Write attachments to workspace files (stamping a `path` on each) so the
        # model can reach any file via a tool, independently of what it can read
        # inline. Done once here; forwarded sub-agent attachments carry the path.
        attachments = self._materialize_attachments(attachments)
        # Expose this turn's attachments so call_agent can forward them by index.
        self._turn_attachments = attachments or []

        messages: list[dict] = []

        # Build OpenAI-format tool definitions (dropped if answering an attachment directly).
        # ensure_mcp() lazily connects the MCP servers this agent's tools live on:
        # discovery is async while the definition lookup below is sync. This is the
        # single funnel for every caller (streaming, non-streaming and sub-agents,
        # which run their own _run_stream_inner), and it never raises.
        await self.tool_registry.ensure_mcp(self.agent.tools)
        tool_defs = self.tool_registry.get_definitions_for_agent(self.agent.tools)

        # Prior conversation (cleaned of tool-call artifacts). Memory-enabled
        # agents get a wide safety net: their real limit is the token-based
        # compactor (and _truncate_messages remains the last defense).
        # `history` is the whole window and is what goes back to the caller;
        # only the turns this agent may see as messages are sent (see
        # _split_history for the foreign ones, quoted in the system prompt).
        history: list[dict] = []
        if conversation:
            history = self._clean_conversation(
                conversation,
                max_messages=200 if self.agent.memory_enabled else 10,
            )
        own_history, foreign_context = self._split_history(history)

        system_content, tool_defs, openai_tools = self._prepare_turn(
            tool_defs, attachments, memory_context, transcribed=transcribed,
            foreign_context=foreign_context)

        # System prompt (tool descriptions injected if model doesn't support native tools)
        messages.append({"role": "system", "content": system_content})
        messages.extend(own_history)
        # Where the injected history ends: everything from here on is THIS
        # turn (user message included) — the slice the caller gets back.
        history_end = len(messages)

        # New user message (with attachments folded in / images as content parts)
        messages.append({"role": "user", "content": self._build_user_content(user_message, attachments)})
        # Boundary between injected history and messages produced THIS turn. The
        # final-reply search must not reach back into a prior turn's answer, or
        # an LLM failure would silently echo the previous reply.
        turn_start = len(messages)

        iterations = 0
        total_tool_calls = 0
        max_tool_calls = self.agent.max_tool_calls  # hard limit per run
        trace_steps: list[dict] = []  # rich recursive trace (full results + sub-agents)
        executed_calls: set[tuple] = set()  # (name, args) keys already run
        # Keys the demotion pass has granted ONE re-execution to, because their
        # result is no longer in the payload (see _fit_context), plus the keys
        # that already used their grant and must not be demoted again.
        recallable: dict[tuple, int] = {}
        pinned: set[tuple] = set()
        ctx_state: dict = {}      # last context snapshot, for ChatResponse.context
        ctx_peak = 0              # the turn's high-water mark, which is what we report
        ctx_demoted = 0
        tool_repeats: dict[str, int] = {}  # calls per tool name, this turn
        repeat_nudged: set[str] = set()   # tools we already warned about repeating
        dedup_nudges = 0          # bounded "you already ran those" retries
        malformed_retries = 0  # text-mode tool calls we asked the model to resend
        full_reasoning = ""  # chain-of-thought of every round, kept out of the answer
        tools_downgrade_retried = False  # one free redo when the endpoint rejects `tools` mid-run
        use_response_temp = False  # switch to response_temperature after tool results
        stream_error: str | None = None  # last LLM failure, surfaced if no reply

        while iterations < self.agent.max_iterations:
            iterations += 1

            # Choose temperature: tool-calling vs final response
            current_temp = self.agent.temperature
            if use_response_temp and self.agent.response_temperature is not None:
                current_temp = self.agent.response_temperature
                self._debug_log(f"  Using response_temperature: {current_temp}")

            # Update system prompt if provider fell back to no-tools mode
            # (keeping the turn-scoped suffix: memory + attachments manifest)
            if self.provider.supports_tools is False and tool_defs:
                new_prompt = self._system_prompt_with_tools(tool_defs) + self._system_suffix
                if messages[0]["content"] != new_prompt:
                    messages[0]["content"] = new_prompt

            # Debug log: dump the COMPLETE payload sent to the LLM (system
            # prompt included — this is where "Available Agents" shows up).
            # Everything needed to replay the call by hand: who, on what, with
            # which sampling, which tool schemas, and the exact messages.
            self._debug_log(
                f"\n--- iteration {iterations}  agent '{self.agent.id}'"
                f"  depth {self.depth}"
                f"  {datetime.now().isoformat(timespec='seconds')} ---")
            self._debug_log(
                f"Request: model={getattr(self.model_config, 'model', '?')} "
                f"temperature={current_temp} "
                f"protocol={'native' if self.provider.supports_tools is not False else 'text'} "
                f"tools_sent={len(openai_tools or [])} "
                f"messages={len(messages)}  (full payload in api.log)")
            # The payload itself is NOT dumped here: LLMProvider writes every
            # call to api.log, so repeating it would double the biggest thing
            # in the trace and let the two copies disagree. This file stays the
            # narrative — which iteration, which decision, which tool result.
            self.provider.trace_label = (
                f"agent '{self.agent.id}' iteration {iterations} depth {self.depth}")

            # Fit the payload BEFORE sending it. Doing this here rather than only
            # at the wire is the whole point: _build_payload's trim works on a
            # COPY, so the next iteration rebuilt from the full results and
            # overflowed again — five wasted 400s in one measured turn.
            ctx_state = await self._fit_context(
                messages, openai_tools, turn_start, recallable, pinned)
            ctx_peak = max(ctx_peak, ctx_state.get("used", 0))
            just_demoted = ctx_state.get("demoted", 0)
            ctx_demoted += just_demoted
            # Declare it, ONCE per turn and only at depth 0 — same rule as the
            # default-model fallback notice: a sub-agent would emit its own
            # identical line, and every cap in this codebase says so out loud.
            # A compressed context is exactly what explains a thinner answer.
            if just_demoted and ctx_demoted == just_demoted and self.depth == 0:
                yield {"type": "notice",
                       "data": prompts.CONTEXT_COMPACTED_NOTICE.format(
                           window=ctx_state.get("window", 0))}

            # Stream LLM response token by token
            pre_tools = self.provider.supports_tools  # to detect a mid-call downgrade
            try:
                full_content = ""
                tool_calls_accum: dict[int, dict] = {}
                # Chain-of-thought is a SEPARATE channel: it never enters
                # full_content, so it reaches neither conversation[] (and thus
                # the next round's prompt) nor the reply the channels deliver
                # and a satellite speaks. One splitter per LLM call.
                splitter = ReasoningSplitter()
                # Rounds are concatenated into one block; separate them.
                pending_sep = "\n\n" if full_reasoning else ""

                async for chunk in self.provider.chat_completion_stream(
                    messages=messages,
                    tools=openai_tools,
                    temperature=current_temp,
                ):
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    events, full_content, added, pending_sep = self._reasoning_events(
                        splitter.feed(delta), full_content, pending_sep)
                    full_reasoning += added
                    for ev in events:
                        yield ev

                    if delta.get("tool_calls"):
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_accum:
                                tool_calls_accum[idx] = {
                                    "id": tc_delta.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc_delta.get("id"):
                                tool_calls_accum[idx]["id"] = tc_delta["id"]
                            func = tc_delta.get("function", {})
                            if func.get("name"):
                                tool_calls_accum[idx]["function"]["name"] += func["name"]
                            if func.get("arguments"):
                                tool_calls_accum[idx]["function"]["arguments"] += func["arguments"]

                # Release whatever was held back as a possibly-cut-off tag.
                events, full_content, added, pending_sep = self._reasoning_events(
                    (*splitter.finish(), False), full_content, pending_sep)
                full_reasoning += added
                for ev in events:
                    yield ev

            except Exception as e:
                # Ask the provider to explain it: only it knows which backend it
                # was talking to and at which URL (see LLMProvider.explain_error).
                err_msg = self.provider.explain_error(e)
                log.error("LLM stream failed: %s", err_msg)
                self._debug_log(f"LLM stream FAILED: {err_msg}")
                stream_error = err_msg
                # Break out of the loop; the post-loop code decides whether to
                # salvage a partial answer (tool results this turn) or surface
                # the error. Either way the error is reported if no reply exists.
                break

            tool_calls = [tool_calls_accum[i] for i in sorted(tool_calls_accum)] if tool_calls_accum else None

            # Debug log LLM response (complete)
            if full_reasoning:
                self._debug_log(f"LLM reasoning: {len(full_reasoning)} chars (see api.log)")
            self._debug_log(f"LLM response content: {full_content or ''}")
            if tool_calls:
                self._debug_log(f"  Structured tool_calls: {json.dumps(tool_calls, ensure_ascii=False)}")

            # Fallback: parse tool calls from text content. In NATIVE mode a
            # structured call is how the model calls tools, so prose is only
            # scavenged when it carries an explicit JSON call shape
            # (_looks_like_tool_call) — a final answer that merely MENTIONS
            # tool names (e.g. «retrieve it with memory_read(s-000014)») must
            # survive as the answer, not get eaten and replayed as junk calls.
            # In text mode the reply is the only call channel, so parse always.
            if not tool_calls and tool_defs and full_content and (
                    self.provider.supports_tools is False
                    or self._looks_like_tool_call(full_content, tool_defs)):
                parsed = self._parse_text_tool_calls(full_content, tool_defs)
                if parsed:
                    log.info("Stream fallback: parsed %d tool call(s) from text", len(parsed))
                    self._debug_log(f"  Parsed from text: {json.dumps(parsed, ensure_ascii=False)}")
                    tool_calls = parsed
                    full_content = ""
                    # Tell frontend to clear the streamed JSON text
                    yield {"type": "clear_tokens"}
                elif (self.provider.supports_tools is False
                        and malformed_retries < _MAX_MALFORMED_RETRIES
                        and self._looks_like_tool_call(full_content, tool_defs)):
                    # The model decided on a tool but its JSON doesn't parse — one
                    # stray quote inside a message is enough. Treating that as the
                    # final answer silently drops the step it had decided on (and
                    # ends a multi-step chain), so show it the problem and ask for
                    # the call again. Bounded, and only in text mode: a native
                    # tool_call is structured by construction.
                    malformed_retries += 1
                    log.info("Malformed text tool call, asking the model to resend")
                    self._debug_log("  MALFORMED tool call -> asking for a resend")
                    yield {"type": "clear_tokens"}
                    messages.append({"role": "assistant",
                                     "content": f"{prompts.UNPARSED_CALL_PREFIX} unparsed)\n{full_content}"})
                    messages.append({"role": "user", "content": _MALFORMED_CALL_PROMPT})
                    continue

            # The endpoint rejected `tools` mid-call (auto probe -> text
            # protocol downgrade): this reply was produced WITHOUT the
            # text-protocol instructions in the prompt, so its lack of a tool
            # call means nothing. Redo the round once — the loop head rebuilds
            # the system prompt now that supports_tools is False.
            if (not tool_calls and tool_defs and not tools_downgrade_retried
                    and pre_tools is None and self.provider.supports_tools is False):
                tools_downgrade_retried = True
                iterations -= 1  # the model was flying blind; don't charge the round
                yield {"type": "clear_tokens"}
                self._debug_log("  Endpoint rejected tools -> redoing round with text-protocol prompt")
                continue

            # Deduplicate within same response + skip cross-iteration repeats
            if tool_calls:
                seen = set()
                unique = []
                for tc in tool_calls:
                    key = _tool_call_key(tc)
                    repeat = key in executed_calls
                    if repeat and recallable.get(key):
                        # Its result was demoted to a stub that said "call it
                        # again": honour that once. Without this the model obeys
                        # our own instruction and the call vanishes.
                        recallable.pop(key, None)
                        pinned.add(key)
                        repeat = False
                        self._debug_log(f"  RECALL allowed: {key[0]}({key[1][:80]})")
                    if key in seen or repeat:
                        self._debug_log(f"  DEDUP skipped: {key[0]}({key[1][:80]})")
                        continue
                    seen.add(key)
                    unique.append(tc)
                if len(unique) < len(tool_calls):
                    log.info("Filtered tool calls: %d -> %d", len(tool_calls), len(unique))
                if not unique and not full_content.strip():
                    # EVERY call was a repeat and the model said nothing else.
                    # Falling through leaves an assistant turn with content None
                    # and breaks out mute, which then trips the forced synthesis
                    # — a turn that ends in an apology because we swallowed its
                    # calls without a word. Say so instead, once, in the shape
                    # the malformed-call retry already uses.
                    if dedup_nudges < _MAX_MALFORMED_RETRIES:
                        dedup_nudges += 1
                        self._debug_log("  ALL CALLS DEDUPED -> nudging for an answer")
                        messages.append({"role": "user",
                                         "content": _REPEATED_CALLS_PROMPT})
                        continue
                tool_calls = unique or None

            # Check hard limit on total tool calls
            if tool_calls and total_tool_calls >= max_tool_calls:
                self._debug_log(f"  MAX TOOL CALLS reached ({max_tool_calls}), forcing answer")
                log.warning("Agent '%s' hit max_tool_calls limit (%d)", self.agent.id, max_tool_calls)
                tool_calls = None

            assistant_msg = {"role": "assistant", "content": full_content or None}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
                assistant_msg["content"] = None
            messages.append(assistant_msg)

            if not tool_calls:
                break

            # Execute tool calls and stream results
            call_result_parts = []
            for tc in tool_calls:
                func_name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments", "{}")
                call_id = tc.get("id", "")

                # Malformed arguments: report the error instead of silently
                # running the tool with empty args (which can do the wrong thing,
                # e.g. file_write with no path).
                if isinstance(raw_args, str):
                    try:
                        func_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        func_args = None
                else:
                    func_args = raw_args

                if not isinstance(func_args, dict):
                    result = f"ERROR: Could not parse tool arguments: {raw_args}"
                    total_tool_calls += 1
                    executed_calls.add(_tool_call_key(tc))
                    step = self._make_step(func_name, {}, result)
                    trace_steps.append(step)
                    yield {"type": "tool_start", "data": {"tool": func_name, "arguments": {}}}
                    yield {"type": "tool_result", "data": _step_summary(step)}
                    if self.provider.supports_tools is False:
                        call_result_parts.append(f"- {func_name}: {result}")
                    else:
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                    continue

                yield {"type": "tool_start", "data": {"tool": func_name, "arguments": func_args}}

                # Concurrent execution: agent_event envelopes pushed by a
                # delegated sub-agent stream out HERE, between this step's
                # tool_start and tool_result — exactly where the UI nests them.
                result = ""
                async for kind, item in self._execute_streaming(func_name, func_args):
                    if kind == "event":
                        yield item
                    else:
                        result = item
                total_tool_calls += 1

                # Resource markers come out HERE, before the result flows
                # anywhere: the model (and the text protocol's replay) sees the
                # short note, the UI gets {path, mime, title, size} on the step.
                result, step_resources = resource_channel.extract(
                    result, config.WORKSPACE_DIR
                )

                executed_calls.add(_tool_call_key(tc))
                # The model cannot count its own calls, and ten sequential
                # local_read(offset=…) reads of one manual is the loop this
                # exists for. The note rides the RESULT and not the tool schema,
                # so it costs nothing until it is needed — and it goes on BEFORE
                # _make_step, so the trace and the debug log show the model
                # exactly what the model saw.
                tool_repeats[func_name] = tool_repeats.get(func_name, 0) + 1
                if (tool_repeats[func_name] >= _REPEAT_TOOL_NUDGE_AT
                        and func_name not in repeat_nudged):
                    repeat_nudged.add(func_name)
                    result += prompts.REPEATED_TOOL_NOTE.format(
                        tool=func_name, n=tool_repeats[func_name])
                # Rich trace step (full result + nested sub-agent trace). Built
                # here so the call_agent sub-trace queued by call_agent_handler
                # is consumed in the same order it was produced. The SSE/
                # tool_results summary is a projection of the same step —
                # single source of truth.
                step = self._make_step(func_name, func_args, result,
                                       resources=step_resources)
                trace_steps.append(step)
                yield {"type": "tool_result", "data": _step_summary(step)}
                self._debug_log(f"  Tool result: {func_name} -> {trace.content(result)}")

                if self.provider.supports_tools is False:
                    call_result_parts.append(f"- {func_name}: {result}")
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    })

            # For models without tool support: the text protocol has no
            # role:tool message, so the results come back as a labeled user turn
            # and the assistant turn replays the call the model just made.
            #
            # That replay matters. The turn used to be a bare "(used tool: X)"
            # marker, and models COPIED it as their next reply — a dead end: a
            # marker carries no call, so the turn ended there and a second agent
            # could never be reached. Echoing the canonical JSON teaches the
            # protocol instead, and the worst case (an identical repeat) is
            # caught by the dedup above rather than losing the turn. The marker
            # stays as the first line: is_scaffolding_message() keys on it to
            # keep this plumbing out of the next turn's history.
            if self.provider.supports_tools is False and call_result_parts:
                names = ", ".join(tc["function"]["name"] for tc in tool_calls)
                messages[-1] = {
                    "role": "assistant",
                    "content": f"{prompts.USED_TOOL_PREFIX} {names})\n" + "\n".join(
                        json.dumps({"name": tc["function"]["name"],
                                    "arguments": _tool_call_args(tc)}, ensure_ascii=False)
                        for tc in tool_calls),
                }
                messages.append({
                    "role": "user",
                    "content": (prompts.TOOL_RESULTS_PREFIX + "\n"
                                + "\n\n".join(call_result_parts)
                                + prompts.TOOL_RESULTS_NUDGE),
                })
                # The answering temperature belongs to the answer. While the
                # budget still allows a tool call, the next turn is a DECISION,
                # so keep the tool-calling temperature for it.
                if total_tool_calls >= max_tool_calls or iterations + 1 >= self.agent.max_iterations:
                    use_response_temp = True
                self._debug_log("  Added 'decide: next tool or answer' nudge")

        # Final response: last assistant content produced THIS turn only (never
        # reach into injected prior-turn history — see turn_start).
        final_text = ""
        for m in reversed(messages[turn_start:]):
            if m.get("role") == "assistant" and m.get("content"):
                final_text = m["content"]
                break

        # If the model looped on tool calls and never wrote an answer, force one
        # last no-tool synthesis from the tool results, streaming it to the user
        # (avoids ending the turn with the bare "(used tool: ...)" marker).
        if self._is_degenerate_reply(final_text) and trace_steps:
            yield {"type": "clear_tokens"}
            forced = ""
            splitter = ReasoningSplitter()
            pending_sep = "\n\n" if full_reasoning else ""
            try:
                async for chunk in self.provider.chat_completion_stream(
                    messages=self._synthesis_messages(messages, trace_steps),
                    tools=None,
                    temperature=self._synthesis_temp(),
                ):
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    events, forced, added, pending_sep = self._reasoning_events(
                        splitter.feed(delta), forced, pending_sep)
                    full_reasoning += added
                    for ev in events:
                        yield ev
                events, forced, added, pending_sep = self._reasoning_events(
                    (*splitter.finish(), False), forced, pending_sep)
                full_reasoning += added
                for ev in events:
                    yield ev
            except Exception as e:
                log.warning("Forced answer stream failed: %s", e)
            if forced.strip():
                final_text = forced.strip()
                # Into `messages` too: the conversation handed back is built
                # from it, and without this the turn's only real answer never
                # reached the stored history — the next turn saw the question
                # and no reply.
                messages.append({"role": "assistant", "content": final_text})
                self._debug_log("  Forced final answer synthesized")

        # No usable reply produced this turn: surface the LLM error (rather than
        # ending on an empty reply or, worse, echoing a prior turn's answer).
        if self._is_degenerate_reply(final_text) and stream_error:
            yield {"type": "error", "data": stream_error}
            final_text = f"ERROR: LLM call failed: {stream_error}"
        elif not final_text and iterations >= self.agent.max_iterations:
            final_text = prompts.NO_FINAL_RESPONSE

        # Whole window back to the caller: system, EVERY history entry (the
        # foreign ones too — they were only kept out of the prompt), then what
        # this turn produced. Persisting `messages` alone would drop the
        # foreign turns from the stored conversation for good.
        clean_messages = []
        for m in messages[:1] + history + messages[history_end:]:
            clean_messages.append(ChatMessage(
                role=m.get("role", "assistant"),
                content=self._flatten_content(m.get("content")),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            ))

        response = ChatResponse(
            reply=final_text,
            reasoning=full_reasoning.strip(),
            conversation=clean_messages,
            iterations=iterations,
            tool_results=[_step_summary(s) for s in trace_steps],
            trace=self._build_trace(final_text, iterations, trace_steps),
            context=({**ctx_state, "peak_used": ctx_peak, "demoted": ctx_demoted}
                     if ctx_state else None),
        )
        yield {"type": "done", "data": response.model_dump()}
