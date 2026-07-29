"""Internal tool handlers for the per-agent deep memory (pattern: call_agent).

The store is reached through ``executor.stores.memory`` and the agent through
``executor.agent`` — the registry passes ``executor=self`` on dispatch, so no
partials are needed at registration time.

Access rule (hard exclusion): an agent with ``memory_enabled == False`` gets a
refusal even if the tools are attached to it — the flag is the single switch
for the whole memory subsystem. Memory is strictly per-agent: each handler
only ever touches the calling agent's own tree.

Output is deliberately small and plain-text: these results are read back by
small local models.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

MAX_RESULTS = 5
READ_MAX_CHARS = 4000


def _gate(executor):
    """Common guard. Returns (memory, agent_id, error_string_or_None)."""
    if executor is None:
        return None, "", "ERROR: No executor context available for memory tools"
    agent = getattr(executor, "agent", None)
    memory = getattr(getattr(executor, "stores", None), "memory", None)
    if memory is None:
        return None, "", "ERROR: memory storage is not available"
    if agent is None or not getattr(agent, "memory_enabled", False):
        return None, "", "ERROR: memory is disabled for this agent"
    return memory, agent.id, None


def _fmt_line(r: dict) -> str:
    summary = " ".join((r.get("summary") or "").split())
    if len(summary) > 140:
        summary = summary[:140].rstrip() + "…"
    return f"{r.get('id')} | {(r.get('created_at') or '')[:10]} | {summary}"


async def memory_search_handler(query: str = "", deep: bool = False,
                                executor=None, **kwargs) -> str:
    """Keyword search over the agent's memory tree (summaries + keywords;
    deep=true also greps the archived raw chunks)."""
    memory, agent_id, err = _gate(executor)
    if err:
        return err
    if not (query or "").strip():
        return "ERROR: 'query' is required"
    async with memory.lock(agent_id):
        memory.load_tree(agent_id, adopt=True)
        results = memory.search(agent_id, query, max_results=MAX_RESULTS,
                                deep=bool(deep))
    if not results:
        tree = memory.load_tree(agent_id)
        nodes = tree.get("nodes", {})
        if not nodes:
            return "Memory is empty."
        # No keyword match ≠ empty memory. Vague queries ("what do you
        # remember?") never match keywords: hand the model the most recent
        # top-level entries so it has something true to report.
        recent = [n for n in nodes.values() if not n.get("parent")]
        recent.sort(key=lambda n: n.get("id", ""), reverse=True)
        lines = [f"No match for that query, but memory holds "
                 f"{len(recent)} item(s). Most recent:"]
        lines += [_fmt_line(n) for n in recent[:MAX_RESULTS]]
        lines.append("Use memory_read with an id to see the details.")
        return "\n".join(lines)
    lines = [_fmt_line(r) for r in results]
    lines.append("Use memory_read with an id to see the details.")
    return "\n".join(lines)


async def memory_read_handler(node_id: str = "", executor=None, **kwargs) -> str:
    """Drill down one node: a summary node lists its children; a chunk returns
    the archived transcript (truncated)."""
    memory, agent_id, err = _gate(executor)
    if err:
        return err
    node_id = (node_id or "").strip()
    async with memory.lock(agent_id):
        memory.load_tree(agent_id, adopt=True)
        data = memory.read_node(agent_id, node_id)
    if data is None:
        return f"ERROR: no memory node '{node_id}'"

    if data["type"] == "chunk":
        chunk = data["chunk"]
        if chunk.get("kind") == "note":
            return f"[note {chunk['id']} — {chunk.get('created_at', '')}]\n{chunk.get('text', '')}"
        lines = [f"[conversation chunk {chunk['id']} — {chunk.get('created_at', '')}"
                 f" — session {chunk.get('session_id', '') or '?'}]"]
        for m in chunk.get("messages", []):
            lines.append(f"{m.get('role', '?')}: {m.get('content', '')}")
        text = "\n".join(lines)
        if len(text) > READ_MAX_CHARS:
            text = (text[:READ_MAX_CHARS].rstrip()
                    + f"\n[truncated — {len(chunk.get('messages', []))} messages total]")
        return text

    node = data["node"]
    lines = [f"[{node['id']} — level {node.get('level')} — {node.get('created_at', '')}]",
             node.get("summary", "")]
    if data["children"]:
        lines.append("Children:")
        for c in data["children"]:
            lines.append("  " + _fmt_line(c))
        lines.append("Use memory_read with a child id to drill down.")
    return "\n".join(lines)


async def memory_note_handler(content: str = "", keywords=None,
                              executor=None, **kwargs) -> str:
    """Store a standalone fact in deep memory (survives across chats)."""
    memory, agent_id, err = _gate(executor)
    if err:
        return err
    if not (content or "").strip():
        return "ERROR: 'content' is required"
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    elif not isinstance(keywords, (list, tuple)):
        keywords = []
    async with memory.lock(agent_id):
        node_id = memory.add_note(agent_id, content, list(keywords))
    _schedule_reroot(executor, memory)
    return f"Saved as {node_id}."


def _schedule_reroot(executor, memory) -> None:
    """Refresh the root digest in the background after a note. Without this,
    a fresh note stays out of the injected '## Memory' section — what new
    sessions see without any tool call — until the next session compaction
    happens to run. Best-effort: on failure the digest is simply stale."""
    from app.engine.memory_compactor import fold_and_reroot  # lazy: import cycle

    agent, model_config = executor.agent, executor.model_config

    async def _job():
        try:
            async with memory.lock(agent.id):
                await fold_and_reroot(agent, model_config, memory)
        except Exception:
            log.exception("memory reroot after note failed for agent '%s'", agent.id)

    asyncio.create_task(_job())
