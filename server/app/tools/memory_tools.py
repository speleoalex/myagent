"""Internal tool handlers for the per-agent long-term memory (pattern: call_agent).

The store is reached through ``executor.stores.memory`` and the agent through
``executor.agent`` — the registry passes ``executor=self`` on dispatch, so no
partials are needed at registration time.

Access rule (hard exclusion): an agent with ``memory_enabled == False`` gets a
refusal even if the tools are attached to it — the flag is the single switch
for the whole memory subsystem. Memory is strictly per-agent: each handler
only ever touches the calling agent's own files.

Output is deliberately small and plain-text: these results are read back by
small local models.
"""
from __future__ import annotations

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
    kind = "note | " if r.get("kind") == "note" else ""
    return f"{r.get('id')} | {(r.get('date') or '')[:10]} | {kind}{summary}"


async def memory_search_handler(query: str = "", deep: bool = False,
                                executor=None, **kwargs) -> str:
    """Keyword search over the memory chunks (notes rank first). ``deep`` is
    accepted and ignored — kept so cached prompts of older sessions don't
    break; every search already covers the full stored text."""
    memory, agent_id, err = _gate(executor)
    if err:
        return err
    if not (query or "").strip():
        return "ERROR: 'query' is required"
    results = memory.search(agent_id, query, max_results=MAX_RESULTS)
    if not results:
        recent = memory.list_recent(agent_id, MAX_RESULTS)
        if not recent:
            return "Memory is empty."
        # No keyword match ≠ empty memory. Vague queries ("what do you
        # remember?") never match keywords: hand the model the most recent
        # entries so it has something true to report.
        lines = [f"No match for that query. Most recent memories:"]
        lines += [_fmt_line(r) for r in recent]
        lines.append("Use memory_read with an id to see the details.")
        return "\n".join(lines)
    lines = [_fmt_line(r) for r in results]
    lines.append("Use memory_read with an id to see the details.")
    return "\n".join(lines)


async def memory_read_handler(node_id: str = "", executor=None, **kwargs) -> str:
    """Read one memory item by id: a note returns its full text, a
    conversation chunk its summary plus the session holding the transcript."""
    memory, agent_id, err = _gate(executor)
    if err:
        return err
    node_id = (node_id or "").strip()
    data = memory.read_chunk(agent_id, node_id)
    if data is None:
        return f"ERROR: no memory item '{node_id}'"
    meta, body = data["meta"], data["body"]
    if len(body) > READ_MAX_CHARS:
        body = body[:READ_MAX_CHARS].rstrip() + "\n[truncated]"
    date = meta.get("date", "")
    if meta.get("kind") == "note":
        return f"[note {node_id} — {date}]\n{body}"
    lines = [f"[conversation summary {node_id} — {date}"
             f" — session {meta.get('session') or '?'}]", body]
    if meta.get("session"):
        lines.append(f"Full transcript: session '{meta['session']}' in the "
                     "sessions store.")
    return "\n".join(lines)


async def memory_note_handler(content: str = "", keywords=None,
                              executor=None, **kwargs) -> str:
    """Store an explicit fact in long-term memory. It lands straight in the
    '## Notes' index of memory.md — visible to every future session without
    any tool call, and with higher staying power than automatic summaries."""
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
        chunk_id = memory.add_note(agent_id, content, list(keywords))
    return f"Saved as {chunk_id}."
