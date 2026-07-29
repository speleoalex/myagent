"""Background compaction of chat sessions into per-agent deep memory.

The session's ``conversation[]`` is the medium-short memory. When its cleaned
size exceeds ``agent.memory_threshold`` (estimated tokens), the oldest turns
are archived as a chunk in the agent's deep memory (:class:`MemoryStore`),
summarized by the agent's own model, and replaced by a compact summary kept in
``session["memory"]`` — never inside ``conversation[]``, so the scaffolding
predicate and the rewind endpoint keep working unchanged:

    session["memory"] = {
        "archived_user_turns": N,      # real user turns spliced out so far
        "context": [{"node", "ts", "summary"}, ...]   # FIFO, newest last
    }

Crash-safe write order (all under ``memory.lock(agent_id)``): select head →
summarize (no writes) → chunk file → tree.json → ONLY THEN re-read the session,
verify the selected head is still there, splice and persist. Nothing is ever
removed from the conversation before it is durable in deep memory; a crash in
between is healed by the content-hash dedup (the retry finds the chunk and only
performs the splice). Any failure — model down, garbage summary, session moved
on — aborts silently: behavior degrades to exactly today's, retried next turn.

Runs fire-and-forget (``asyncio.create_task``) after a turn is persisted, so
the user never waits on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter

from app.engine.executor import AgentExecutor
from app.engine.llm_provider import LLMProvider
from app.models import Agent, ModelConfig
from app.storage.memory import MemoryStore
from app.storage.sessions import now_iso

log = logging.getLogger(__name__)

# Parentless nodes at a level fold into one level-N+1 summary once they reach
# this count (cascading upward).
GROUP_SIZE = 8
# Size gates for LLM-produced summaries (chars).
SUMMARY_MAX_CHARS = 1200
ROOT_MAX_CHARS = 900
# The newest real user turns are never archived: the model keeps them raw.
KEEP_RECENT_TURNS = 2
# FIFO cap of session["memory"]["context"] (older ground is covered by the
# root digest and memory_search).
CONTEXT_CAP = 6
# Temperature for summarization calls: factual, not creative.
_SUMMARY_TEMP = 0.2

_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_WORD_RE = re.compile(r"[a-zA-Zà-ÿÀ-ß0-9_]{5,}")


# ---------------------------------------------------------------- selection

def _is_real_user_turn(m: dict) -> bool:
    return m.get("role") == "user" and not AgentExecutor.is_scaffolding_message(
        "user", m.get("content"), m.get("tool_calls")
    )


def _clean(conversation: list[dict]) -> list[dict]:
    """Dialogue-only view of raw conversation entries (scaffolding dropped,
    content flattened to strings) — what gets archived and measured."""
    out = []
    for m in conversation:
        content = AgentExecutor._flatten_content(m.get("content")) or ""
        if AgentExecutor.is_scaffolding_message(m.get("role", ""), content, m.get("tool_calls")):
            continue
        out.append({"role": m.get("role", ""), "content": content})
    return out


def _estimate_tokens(cleaned: list[dict]) -> int:
    return sum(LLMProvider._estimate_tokens(m.get("content") or "") for m in cleaned)


def should_compact(agent: Agent, conversation: list[dict]) -> bool:
    """Whether this session's cleaned conversation is over the agent's
    threshold. Cheap and sync — callable from the request path."""
    if not agent.memory_enabled or not conversation:
        return False
    return _estimate_tokens(_clean(conversation)) > agent.memory_threshold


def _select_head(conversation: list[dict], threshold: int):
    """The oldest slice of the raw conversation to archive.

    Cuts on real-user-turn boundaries only: the head ends right before the
    first kept turn, chosen so the remaining cleaned conversation fits within
    threshold/2 — while always keeping at least the last KEEP_RECENT_TURNS
    real turns raw. Returns (head_raw, archived_turns) or (None, 0).
    """
    turn_starts = [i for i, m in enumerate(conversation) if _is_real_user_turn(m)]
    if len(turn_starts) <= KEEP_RECENT_TURNS:
        return None, 0
    target = max(threshold // 2, 1)
    max_cut = len(turn_starts) - KEEP_RECENT_TURNS
    for k in range(1, max_cut + 1):
        remaining = _clean(conversation[turn_starts[k]:])
        if _estimate_tokens(remaining) <= target:
            return conversation[:turn_starts[k]], k
    # Even the deepest allowed cut doesn't reach the target: take it anyway
    # (the rest is protected by KEEP_RECENT_TURNS and _truncate_messages).
    return conversation[:turn_starts[max_cut]], max_cut


def _keywords_from(text: str, limit: int = 8) -> list[str]:
    """Cheap keyword extraction (no extra LLM round-trip): the most frequent
    long-ish words of the summary."""
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    return [w for w, _ in Counter(words).most_common(limit)]


# ------------------------------------------------------------- summarization

def _validate_summary(text: str | None, max_chars: int) -> str | None:
    """Reject anything that must never enter the permanent tree: too short,
    runaway-long, JSON/tool-call shaped, or echoed tool plumbing."""
    if not text:
        return None
    text = _THINK_RE.sub("", text).strip()
    if len(text) < 20 or len(text) > max_chars * 2:
        return None
    if text[0] in "{[":
        return None
    if "TOOL RESULTS:" in text or '"arguments"' in text:
        return None
    if len(text) > max_chars:
        cut = text[:max_chars]
        for sep in (". ", "\n", "; "):
            idx = cut.rfind(sep)
            if idx > max_chars // 2:
                cut = cut[: idx + 1]
                break
        text = cut.rstrip()
    return text


async def summarize(model_config: ModelConfig, text: str, max_chars: int,
                    instruction: str) -> str | None:
    """One bare LLM call (no executor, no tools) → validated summary or None."""
    provider = LLMProvider(model_config)
    try:
        out = ""
        async for chunk in provider.chat_completion_stream(
            messages=[
                {"role": "system", "content": (
                    f"{instruction} Use at most {max_chars} characters. Keep "
                    "concrete facts, names, numbers, dates, decisions and "
                    "preferences. Write plain prose in the main language of the "
                    "text, without preamble, headings or lists."
                )},
                {"role": "user", "content": text},
            ],
            tools=None,
            temperature=_SUMMARY_TEMP,
        ):
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                out += delta["content"]
        return _validate_summary(out, max_chars)
    except Exception as e:
        log.warning("memory summarize failed: %s", e)
        return None
    finally:
        await provider.close()


def _transcript(cleaned: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in cleaned if m.get("content"))


# ----------------------------------------------------------------- pipeline

async def compact_session(agent: Agent, model_config: ModelConfig,
                          memory: MemoryStore | None, session_id: str, *,
                          session_store=None, named=None, live=None) -> None:
    """Fire-and-forget entry point (wrap in asyncio.create_task).

    Exactly one of ``session_store`` (web session, guarded by ``live``) or
    ``named`` (channel session, guarded by its per-id lock) must be given.
    Never raises: any problem is logged and the compaction retried on a later
    turn.
    """
    if memory is None or not agent.memory_enabled:
        return
    try:
        async with memory.lock(agent.id):
            await _compact_locked(agent, model_config, memory, session_id,
                                  session_store=session_store, named=named, live=live)
    except Exception:
        log.exception("memory compaction failed for agent '%s'", agent.id)


def _load_session(session_id: str, session_store, named) -> dict | None:
    if named is not None:
        return named.get(session_id)
    return session_store.get(session_id)


async def _compact_locked(agent: Agent, model_config: ModelConfig,
                          memory: MemoryStore, session_id: str, *,
                          session_store, named, live) -> None:
    session = _load_session(session_id, session_store, named)
    if not session:
        return
    conversation = session.get("conversation", [])
    if not should_compact(agent, conversation):
        return
    head, archived_turns = _select_head(conversation, agent.memory_threshold)
    if not head:
        return
    head_clean = _clean(head)
    if not head_clean:
        return
    chunk_hash = memory.hash_messages(head_clean)

    # Idempotent retry: a crash after the tree write but before the splice
    # left the chunk archived — reuse its node and only redo the splice.
    memory.load_tree(agent.id, adopt=True)
    node_id = memory.find_chunk_by_hash(agent.id, session_id, chunk_hash)
    if node_id:
        node = memory.load_tree(agent.id)["nodes"].get(node_id, {})
        summary = node.get("summary", "")
    else:
        summary = await summarize(
            model_config, _transcript(head_clean), SUMMARY_MAX_CHARS,
            "Summarize this excerpt of a past conversation between a user and "
            "an assistant.",
        )
        if not summary:
            return  # model down or garbage output: abort, retry next turn
        node_id, _ = memory.archive_chunk(
            agent.id, head_clean,
            summary=summary,
            keywords=_keywords_from(summary),
            session_id=session_id,
            channel=session.get("channel", ""),
            source=session.get("source", ""),
        )

    # Only now touch the session: re-read it fresh and make sure the head we
    # archived is still exactly its beginning (a concurrent turn, a reset or a
    # rewind means abort — the chunk stays, the retry is idempotent).
    if named is not None:
        async with named.lock(session_id):
            fresh = named.get(session_id)
            if not _splice(fresh, head, archived_turns, node_id, summary):
                return
            await asyncio.to_thread(named.save, session_id, fresh)
    else:
        if live is not None and live.is_active(session_id):
            return
        fresh = session_store.get(session_id)
        if not fresh or not _splice(fresh, head, archived_turns, node_id, summary):
            return
        await asyncio.to_thread(session_store.persist, fresh)
    log.info("memory: archived %d turn(s) of session '%s' as %s (agent '%s')",
             archived_turns, session_id, node_id, agent.id)

    # Housekeeping (best-effort, after the splice so it never delays it):
    # cascade folds and refresh the root digest.
    await fold_and_reroot(agent, model_config, memory)


def _splice(session: dict, head: list[dict], archived_turns: int,
            node_id: str, summary: str) -> bool:
    conversation = session.get("conversation", [])
    if conversation[: len(head)] != head:
        return False
    session["conversation"] = conversation[len(head):]
    mem = session.setdefault("memory", {})
    mem["archived_user_turns"] = mem.get("archived_user_turns", 0) + archived_turns
    ctx = mem.setdefault("context", [])
    ctx.append({"node": node_id, "ts": now_iso(), "summary": summary})
    del ctx[: max(0, len(ctx) - CONTEXT_CAP)]
    return True


async def fold_and_reroot(agent: Agent, model_config: ModelConfig,
                          memory: MemoryStore) -> None:
    """Cascading compaction: whenever a level has GROUP_SIZE parentless nodes,
    fold the oldest GROUP_SIZE into one level-N+1 node (which can cascade
    further up). Then regenerate the root digest from the remaining orphans.
    Every step is optional: on failure the tree simply stays one fold behind.

    Public: memory_note's handler also fires this (in the background) so a
    fresh note reaches the root digest — what new sessions actually see —
    without waiting for the next session compaction. Caller must NOT hold
    ``memory.lock``.
    """
    level = 1
    while level < 10:
        orphans = memory.orphans_at_level(agent.id, level)
        if len(orphans) < GROUP_SIZE:
            level += 1
            continue
        group = orphans[:GROUP_SIZE]
        text = "\n\n".join(n.get("summary", "") for n in group)
        folded = await summarize(
            model_config, text, SUMMARY_MAX_CHARS,
            "Merge these summaries of consecutive past conversations into one.",
        )
        if not folded:
            return  # model unavailable: retry the fold on a later compaction
        memory.apply_fold(agent.id, [n["id"] for n in group], folded, level + 1)
        # Same level may still hold another full group; re-check before moving up.

    tree = memory.load_tree(agent.id)
    orphans = [n for n in tree.get("nodes", {}).values() if not n.get("parent")]
    if not orphans:
        return
    orphans.sort(key=lambda n: n.get("id", ""))
    text = "\n\n".join(n.get("summary", "") for n in orphans)
    root = await summarize(
        model_config, text, ROOT_MAX_CHARS,
        "Write the long-term memory digest of everything below (summaries of "
        "all past conversations of an assistant).",
    )
    if root:
        memory.set_root(agent.id, root)  # failure keeps the previous root
