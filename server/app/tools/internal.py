"""Internal tool handlers that require Python executor context."""

from __future__ import annotations

import re

MAX_AGENT_DEPTH = 5


def _resolve_attachments(executor, indices) -> list[dict]:
    """Map attachment_indices (from a call_agent tool-call) to the parent turn's
    actual attachments. The tool-call only carries small integer indices, so the
    model never has to reproduce base64 blobs. Accepts an int, a list, or a
    loose string (e.g. "[0, 1]" / "0,1") since text-based tool calls vary."""
    pool = getattr(executor, "_turn_attachments", None) or []
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

    depth = getattr(executor, "depth", 0)
    if depth >= MAX_AGENT_DEPTH:
        return f"ERROR: Maximum agent chaining depth ({MAX_AGENT_DEPTH}) reached"

    try:
        from app.engine.executor import AgentExecutor

        # Access gate: the caller may only delegate to agents it's allowed to
        # reach (target enabled + callable + in the caller's allowlist). This is
        # the real enforcement — the system-prompt directory is only advisory.
        caller = getattr(executor, "agent", None)
        target = executor.stores.agents.get(agent_id)
        if target is None:
            return f"ERROR: agent '{agent_id}' not found"
        if caller is not None and not AgentExecutor._agent_can_call(caller, target):
            return f"ERROR: agent '{agent_id}' is not callable from '{caller.id}'"

        sub_executor = await AgentExecutor.create_for_agent(
            agent_id,
            executor.tool_registry,
            executor.stores,
            depth=depth + 1,
        )
        forwarded = _resolve_attachments(executor, attachment_indices)
        response = await sub_executor.run(message, attachments=forwarded or None)

        # Hand the sub-agent's full trace to the parent so the whole flow is
        # persisted recursively. Queued in call order; the parent consumes it
        # when building the call_agent step for this call.
        if hasattr(executor, "_sub_traces"):
            executor._sub_traces.append(
                response.trace or {
                    "agent_id": agent_id,
                    "iterations": response.iterations,
                    "reply": response.reply,
                    "steps": [],
                }
            )

        # Forward sub-agent tool events to parent for SSE streaming
        if hasattr(executor, '_pending_sub_events') and response.tool_results:
            for tr in response.tool_results:
                executor._pending_sub_events.append({
                    "type": "tool_start",
                    "data": {
                        "tool": f"{agent_id}/{tr['tool']}",
                        "arguments": tr['arguments'],
                    },
                })
                executor._pending_sub_events.append({
                    "type": "tool_result",
                    "data": {
                        "tool": f"{agent_id}/{tr['tool']}",
                        "arguments": tr['arguments'],
                        "result_preview": tr.get('result_preview', ''),
                    },
                })

        return response.reply
    except Exception as e:
        return f"ERROR: Failed to call agent '{agent_id}': {e}"
