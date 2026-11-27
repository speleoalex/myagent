"""Background compaction of chat sessions into per-agent long-term memory.

The session's ``conversation[]`` is the medium-short memory. When its cleaned
size exceeds the effective threshold (AgentExecutor.compaction_threshold:
``agent.memory_threshold`` capped by the history share of the context), the
oldest turns
are summarized by the agent's own model, archived as one Markdown chunk in the
agent's memory (:class:`MemoryStore` — summary only, the full transcript stays
in the sessions store), indexed in memory.md, and replaced by a compact
summary kept in ``session["memory"]`` — never inside ``conversation[]``, so
the scaffolding predicate and the rewind endpoint keep working unchanged:

    session["memory"] = {
        "archived_user_turns": N,      # real user turns spliced out so far
        "context": [{"node", "ts", "summary"}, ...]   # FIFO, newest last
    }

Crash-safe write order: select head → summarize (NO lock held — an LLM call
takes 10-60s on a local model and would starve the agent's memory tools) →
then, under ``memory.lock(agent_id)`` for milliseconds: re-check the content
hash (two concurrent compactions may race; the loser reuses the winner's
chunk), chunk file, index line → ONLY THEN re-read the session, verify the
selected head is still there, splice and persist. Nothing is ever removed from
the conversation before it is durable in memory; a crash in between is healed
by the content-hash dedup (the retry finds the chunk, repairs the index —
``add_to_index`` is idempotent — and only redoes the splice). Any failure —
model down, garbage summary, session moved on — aborts silently: behavior
degrades to exactly today's, retried next turn.

Runs fire-and-forget (``asyncio.create_task``) after a turn is persisted, so
the user never waits on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter

from app.engine import prompts
from app.engine.executor import AgentExecutor
from app.engine.llm_provider import OUTPUT_TRUNCATED, LLMProvider
from app.engine.reasoning import strip_reasoning
from app.models import Agent, ModelConfig
from app.storage.memory import MemoryStore, topic_slug
from app.storage.sessions import now_iso

log = logging.getLogger(__name__)

# Size gate for LLM-produced summaries (chars): the ceiling, and the floor the
# memory share may push it down to (summary_chars).
SUMMARY_MAX_CHARS = 1200
SUMMARY_MIN_CHARS = 300
# A summary missing more than this share of the details the user gave
# (numbers, dates, URLs, paths, names) gets one corrective call, then is
# accepted and marked degraded.
_MISSING_TOLERANCE = 1 / 3
# The newest real user turns are never archived: the model keeps them raw.
KEEP_RECENT_TURNS = 2
# FIFO cap of session["memory"]["context"] (older ground is covered by the
# memory.md index and memory_search).
CONTEXT_CAP = 6
# Temperature for summarization calls: factual, not creative.
_SUMMARY_TEMP = 0.2

_WORD_RE = re.compile(r"[a-zA-Zà-ÿÀ-ß0-9_]{5,}")


# ---------------------------------------------------------------- selection

def _is_real_user_turn(m: dict) -> bool:
    return m.get("role") == "user" and not AgentExecutor.is_scaffolding_message(
        "user", m.get("content"), m.get("tool_calls")
    )


def _clean(conversation: list[dict]) -> list[dict]:
    """Dialogue-only view of raw conversation entries (scaffolding dropped,
    content flattened to strings) — what gets summarized and measured."""
    out = []
    for m in conversation:
        content = AgentExecutor._flatten_content(m.get("content")) or ""
        if AgentExecutor.is_scaffolding_message(m.get("role", ""), content, m.get("tool_calls")):
            continue
        out.append({"role": m.get("role", ""), "content": content})
    return out


def _estimate_tokens(cleaned: list[dict], ratio: float) -> int:
    """Ratio-corrected, like the executor's history share: the two must
    measure the same conversation the same way."""
    return int(sum(LLMProvider._estimate_tokens(m.get("content") or "")
                   for m in cleaned) * ratio)


def should_compact(agent: Agent, conversation: list[dict], threshold: int,
                   ratio: float) -> bool:
    """Whether this session's cleaned conversation is over ``threshold``.
    Cheap and sync — callable from the request path."""
    if not agent.memory_enabled or not conversation:
        return False
    return _estimate_tokens(_clean(conversation), ratio) > threshold


def _select_head(conversation: list[dict], threshold: int, ratio: float):
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
        if _estimate_tokens(remaining, ratio) <= target:
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
    """Reject anything that must never enter the permanent memory: too short,
    runaway-long, JSON/tool-call shaped, or echoed tool plumbing."""
    if not text:
        return None
    # A thinking model summarizing: this call streams straight into a string
    # (no executor), so the chain-of-thought is still inline here.
    text = strip_reasoning(text)
    if len(text) < 20 or len(text) > max_chars * 2:
        return None
    if text[0] in "{[":
        return None
    # The marker comes from prompts.py: this gate decides what enters the
    # PERMANENT memory, so it must track any rewording of the scaffolding.
    if prompts.TOOL_RESULTS_PREFIX in text or '"arguments"' in text:
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


def summary_chars(memory_room: int, ratio: float) -> int:
    """How long one archived-turn summary may be. A quarter of the memory
    share (the summaries share it with memory.md), turned into characters at
    ~4 per raw token: on a 4k window six 1200-char summaries alone overflowed
    the whole share. 0 = share not measured, the ceiling."""
    if not memory_room:
        return SUMMARY_MAX_CHARS
    return max(SUMMARY_MIN_CHARS, min(SUMMARY_MAX_CHARS, int(memory_room / ratio)))


class _Truncated(Exception):
    """The summary hit the output cap: whatever came back is not whole."""


_RULES = ("Use at most {n} characters. Keep concrete facts, names, numbers, "
          "dates, decisions and preferences exactly as written. Write plain "
          "prose in the main language of the text, without preamble, headings "
          "or lists.")


async def _ask(provider: LLMProvider, instruction: str, text: str,
               max_chars: int) -> str | None:
    """One bare call (no tools, thinking off) → validated summary or None.
    Raises _Truncated: a cut summary is discarded, never stored."""
    out = ""
    async for chunk in provider.chat_completion_stream(
        messages=[{"role": "system",
                   "content": f"{instruction} {_RULES.format(n=max_chars)}"},
                  {"role": "user", "content": text}],
        tools=None,
        temperature=_SUMMARY_TEMP,
    ):
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        if delta.get("content"):
            out += delta["content"]
    if OUTPUT_TRUNCATED in out:
        raise _Truncated()
    return _validate_summary(out, max_chars)


def _pieces(cleaned: list[dict], budget: int, estimate) -> list[list[dict]]:
    """Consecutive slices of the dialogue, each within ``budget`` tokens. A
    single message over budget is cut into parts of its own, in order."""
    pieces: list[list[dict]] = [[]]
    used = 0
    for m in cleaned:
        text = m["content"]
        cost = estimate(text)
        if cost > budget:
            step = max(1, len(text) * budget // cost)
            parts = [{"role": m["role"], "content": text[i:i + step]}
                     for i in range(0, len(text), step)]
        else:
            parts = [m]
        for part in parts:
            cost = estimate(part["content"])
            if pieces[-1] and used + cost > budget:
                pieces.append([])
                used = 0
            pieces[-1].append(part)
            used += cost
    return [p for p in pieces if p]


# Details the user gave that a summary must not lose: URLs, mail addresses,
# paths, multi-digit numbers and dates, and capitalized words that do not
# open a sentence (names). Model-free, like OpenClaw's "exact identifiers".
_DETAIL_RE = re.compile(
    r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|~?/[\w.-]+(?:/[\w.-]+)+"
    r"|\d[\d.,:/-]*\d|[A-ZÀ-Ý][\w'-]{2,}")
_THOUSANDS = re.compile(r"(?<=\d)[.,](?=\d)")


def _details(cleaned: list[dict]) -> list[str]:
    found: dict[str, None] = {}
    for m in cleaned:
        if m.get("role") != "user":
            continue
        text = m["content"]
        for match in _DETAIL_RE.finditer(text):
            word = match.group().rstrip(".,;:)!?'\"")
            before = text[:match.start()].rstrip()
            if word[0].isupper() and (not before or before[-1] in ".!?:\n"):
                continue  # a capital that opens a sentence is not a name
            found.setdefault(word, None)
    return list(found)


def _missing_details(details: list[str], summary: str) -> list[str]:
    """The details not in the summary. Numbers are compared without thousand
    separators: a model that writes 1.200 for 1200 has not lost it."""
    flat = summary.lower()
    plain = _THOUSANDS.sub("", flat)
    return [d for d in details
            if d.lower() not in flat and _THOUSANDS.sub("", d.lower()) not in plain]


def _estimator(provider: LLMProvider):
    return lambda text: provider.estimate_payload_tokens([{"content": text}])


async def _rolling_summary(provider: LLMProvider, cleaned: list[dict],
                           budget: int, max_chars: int) -> str | None:
    """Piece by piece, in order: each call gets the summary so far and the
    next piece, so no call is ever bigger than the model can read."""
    estimate = _estimator(provider)
    room = max(budget - estimate("x" * max_chars), budget // 2)
    summary = None
    for piece in _pieces(cleaned, room, estimate):
        if summary is None:
            summary = await _ask(
                provider, "Summarize this excerpt of a past conversation "
                "between a user and an assistant.", _transcript(piece), max_chars)
        else:
            summary = await _ask(
                provider, "Update the summary of a past conversation between a "
                "user and an assistant with the part that follows it. Return "
                "the whole updated summary.",
                f"Summary so far:\n{summary}\n\nWhat follows:\n{_transcript(piece)}",
                max_chars)
        if summary is None:
            return None
    return summary


async def _restore_details(provider: LLMProvider, cleaned: list[dict], summary: str,
                           missing: list[str], budget: int, max_chars: int) -> str | None:
    """One corrective call: the summary, the missing details, and the user's
    lines that hold them (so the model does not have to guess what they
    refer to), cut to the budget."""
    lines = "\n".join(m["content"] for m in cleaned if m["role"] == "user"
                      and any(d in m["content"] for d in missing))
    lines = _pieces([{"role": "user", "content": lines}], budget,
                    _estimator(provider))[0][0]["content"]
    try:
        return await _ask(
            provider, "Rewrite this summary so it also keeps these exact details "
            "from the user's messages: " + ", ".join(missing)
            + ". Drop less important words to make room.",
            f"Summary:\n{summary}\n\nThe user's messages:\n{lines}", max_chars)
    except _Truncated:
        return None


async def summarize(model_config: ModelConfig, cleaned: list[dict],
                    max_chars: int) -> tuple[str | None, bool]:
    """(summary, degraded) of a dialogue slice, or (None, False) — model
    down, garbage, or cut twice: the compaction is retried next turn.

    Sized for the model that writes it: the transcript goes in pieces of at
    most half the room that model has (a 4k model fed the whole head saw a
    truncated input and summarized part of it, silently). A summary cut by the
    output cap is retried once on half-size pieces. Then a model-free check:
    if too many of the details the user gave are missing, one corrective call;
    whatever comes back is kept, flagged degraded when it still misses them."""
    provider = LLMProvider(model_config)
    provider.trace_label = "memory summary"
    # Same reason as the auto-router: a summary is housekeeping nobody is
    # waiting to read the working-out of, and it runs on whatever chat model
    # the agent already has — which may well be a thinking one.
    provider.reasoning = False
    try:
        budget = (await provider.context_state([], None))["fit"] // 2
        summary = None
        for _ in range(2):
            try:
                summary = await _rolling_summary(provider, cleaned, budget, max_chars)
                break
            except _Truncated:
                budget //= 2
        if not summary:
            return None, False
        details = _details(cleaned)
        too_many = lambda missing: len(missing) > len(details) * _MISSING_TOLERANCE
        missing = _missing_details(details, summary)
        if too_many(missing):
            fixed = await _restore_details(provider, cleaned, summary, missing,
                                           budget, max_chars)
            if fixed:
                summary = fixed
                missing = _missing_details(details, summary)
        return summary, too_many(missing)
    except Exception as e:
        log.warning("memory summarize failed: %s", e)
        return None, False
    finally:
        await provider.close()


def _transcript(cleaned: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in cleaned if m.get("content"))


# ----------------------------------------------------------------- pipeline

# Strong references to in-flight background tasks: asyncio.create_task keeps
# only a weak reference, so an unanchored fire-and-forget task can be garbage
# collected mid-run and silently vanish.
_BG_TASKS: set[asyncio.Task] = set()
# The same tasks, addressable — what makes cancel_background possible.
_BG_BY_KEY: dict[str, asyncio.Task] = {}


def schedule_background(coro, key: str | None = None) -> None:
    """Fire-and-forget an engine coroutine WITHOUT losing it to the GC.

    With a ``key`` the task stays addressable, so a later turn can cancel it
    (see cancel_background)."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    if key:
        _BG_BY_KEY[key] = task
        # Only clear the slot if it still holds THIS task: a re-schedule under
        # the same key must not be erased by its predecessor finishing.
        task.add_done_callback(
            lambda t, k=key: _BG_BY_KEY.pop(k, None) if _BG_BY_KEY.get(k) is t else None
        )


def cancel_background(key: str) -> bool:
    """Cancel the background task registered under ``key``, if any is still
    running. Returns whether something was actually cancelled.

    Not awaited on purpose: the caller is a request path. The task raises
    CancelledError at its next await — inside the LLM stream — and
    ``summarize``'s ``finally`` closes the httpx client there, which is what
    releases the model slot.
    """
    task = _BG_BY_KEY.pop(key, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def _compaction_key(session_id: str) -> str:
    return f"compact:{session_id}"


def cancel_compaction(session_id: str) -> bool:
    """Call at the START of a turn for this session.

    A compaction racing a new turn was already doomed — phase C aborts on
    ``live.is_active`` — but only AFTER paying for the summarization, and on a
    single-slot local server that generation competes with the answer the user
    is waiting for. Cancelling is free: the pipeline is crash-safe by design
    (content-hash dedup + idempotent add_to_index), so the next turn's
    compaction picks the work up again, reusing any chunk already written.
    """
    return cancel_background(_compaction_key(session_id))


def schedule_compaction(executor, session_id: str, *, session_store=None,
                        named=None, live=None) -> None:
    """Fire-and-forget memory compaction AFTER a persisted turn: the user
    never waits on it (a summarization on a local model takes 10-60s). All
    safety is inside compact_session (head re-verification, idempotent retry
    via content hash). No-op for agents without memory."""
    if not executor.agent.memory_enabled or executor.stores.memory is None:
        return
    schedule_background(compact_session(
        executor.agent, executor.model_config, executor.stores.memory,
        session_id, threshold=executor.compaction_threshold(),
        ratio=executor.provider.token_ratio(),
        max_chars=summary_chars(executor.memory_room(), executor.provider.token_ratio()),
        session_store=session_store, named=named, live=live,
    ), key=_compaction_key(session_id))


async def compact_session(agent: Agent, model_config: ModelConfig,
                          memory: MemoryStore | None, session_id: str, *,
                          threshold: int, ratio: float = 1.0,
                          max_chars: int = SUMMARY_MAX_CHARS,
                          session_store=None, named=None, live=None) -> None:
    """Fire-and-forget entry point (wrap in asyncio.create_task).

    Exactly one of ``session_store`` (web session, guarded by ``live``) or
    ``named`` (channel session, guarded by its per-id lock) must be given.
    Never raises: any problem is logged and the compaction retried on a later
    turn. ``except Exception`` deliberately does NOT cover
    ``asyncio.CancelledError`` (a BaseException since 3.8): swallowing it here
    would make cancel_compaction cooperative in name only.
    """
    if memory is None or not agent.memory_enabled:
        return
    try:
        await _compact_one(agent, model_config, memory, session_id, threshold, ratio,
                           max_chars,
                           session_store=session_store, named=named, live=live)
    except Exception:
        log.exception("memory compaction failed for agent '%s'", agent.id)


def _load_session(session_id: str, session_store, named) -> dict | None:
    if named is not None:
        return named.get(session_id)
    return session_store.get(session_id)


async def _compact_one(agent: Agent, model_config: ModelConfig,
                       memory: MemoryStore, session_id: str, threshold: int,
                       ratio: float, max_chars: int, *, session_store, named,
                       live) -> None:
    session = _load_session(session_id, session_store, named)
    if not session:
        return
    conversation = session.get("conversation", [])
    if not should_compact(agent, conversation, threshold, ratio):
        return
    head, archived_turns = _select_head(conversation, threshold, ratio)
    if not head:
        return
    head_clean = _clean(head)
    if not head_clean:
        return
    chunk_hash = memory.hash_messages(head_clean)

    # Phase A — LLM work, NO lock held. Idempotent retry first: a crash after
    # the chunk write left it archived, reuse its summary without re-paying
    # the model.
    chunk_id = memory.find_chunk_by_hash(agent.id, session_id, chunk_hash)
    if chunk_id:
        existing = memory.read_chunk(agent.id, chunk_id) or {}
        summary = existing.get("body") or "(archived conversation)"
        degraded = False
    else:
        summary, degraded = await summarize(model_config, head_clean, max_chars)
        if not summary:
            return  # model down, garbage or cut output: abort, retry next turn

    # Phase B — file writes under the lock (milliseconds). Re-check the hash:
    # a concurrent compaction may have won the race while we were summarizing;
    # the double check replaces the old whole-pipeline serialization.
    async with memory.lock(agent.id):
        cid = memory.find_chunk_by_hash(agent.id, session_id, chunk_hash)
        if cid:
            chunk_id = cid
        else:
            chunk_id = memory.write_chunk(
                agent.id, body=summary,
                session_id=session_id,
                channel=session.get("channel", ""),
                source=session.get("source", ""),
                keywords=_keywords_from(summary),
                hash_=chunk_hash,
                degraded=degraded,
            )
        # Into the topic store, not the Recent log: the log could only append,
        # so a fact that changed three times stood in the prompt three times.
        # The subject here is derived (no second LLM call): a near-miss costs a
        # line that ages out, which is the cheap failure.
        memory.add_topic(agent.id, topic_slug(summary), summary)  # idempotent

    # Phase C — only now touch the session: re-read it fresh and make sure the
    # head we archived is still exactly its beginning (a concurrent turn, a
    # reset or a rewind means abort — the chunk stays, the retry is idempotent).
    if named is not None:
        async with named.lock(session_id):
            fresh = named.get(session_id)
            if not _splice(fresh, head, archived_turns, chunk_id, summary):
                return
            await asyncio.to_thread(named.save, session_id, fresh)
    else:
        if live is not None and live.is_active(session_id):
            return
        fresh = session_store.get(session_id)
        if not fresh or not _splice(fresh, head, archived_turns, chunk_id, summary):
            return
        await asyncio.to_thread(session_store.persist, fresh)
    log.info("memory: archived %d turn(s) of session '%s' as %s (agent '%s')",
             archived_turns, session_id, chunk_id, agent.id)


def _splice(session: dict, head: list[dict], archived_turns: int,
            chunk_id: str, summary: str) -> bool:
    conversation = session.get("conversation", [])
    if conversation[: len(head)] != head:
        return False
    session["conversation"] = conversation[len(head):]
    mem = session.setdefault("memory", {})
    mem["archived_user_turns"] = mem.get("archived_user_turns", 0) + archived_turns
    ctx = mem.setdefault("context", [])
    ctx.append({"node": chunk_id, "ts": now_iso(), "summary": summary})
    del ctx[: max(0, len(ctx) - CONTEXT_CAP)]
    return True
