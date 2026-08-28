from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator

import httpx

from app.engine import model_probe, prompts
from app.engine import trace
from app.models import ModelConfig

log = logging.getLogger(__name__)

# Payload fields a remote model may refuse outright with a 400 instead of
# ignoring them (OpenAI reasoning models — gpt-5, o1/o3/o4 — answer
# "'temperature' does not support 0.1, only the default (1)"). When that
# happens the field is dropped (or renamed) and the call retried.
DROPPABLE_PARAMS = (
    "temperature", "top_p", "frequency_penalty", "presence_penalty",
    "max_tokens", "max_completion_tokens",
    "top_k", "repeat_penalty", "repeat_last_n", "min_p", "num_ctx",
)

# What an endpoint refuses doesn't change between turns, and a provider
# instance only lives for one run, so remember the adaptations process-wide
# (keyed like the model_probe cache) to avoid paying a 400 round-trip per turn.
# param name -> replacement name, or None to drop it. The sentinel key "tools"
# records a rejected `tools` array: later runs then start in the text protocol
# (supports_tools=False) instead of re-paying the 400 AND losing their first
# iteration to a system prompt without the text-protocol instructions.
_PARAM_FIXES: dict[tuple[str, str], dict[str, str | None]] = {}

# Parameters an endpoint requires ALONGSIDE `tools` before it accepts them.
# OpenAI reasoning models refuse function tools on /v1/chat/completions unless
# reasoning_effort is 'none' — and the refusal names that fix, so taking it
# keeps native tool calling instead of falling through to the no-tools branch,
# which would silently downgrade a tool-capable model to the text protocol
# (observed with gpt-5.6-luna, 2026-08-11). Only applied to payloads that carry
# tools: the conflict is with the combination, and forcing reasoning off on
# plain calls would degrade them. Memoized process-wide like _PARAM_FIXES and
# for the same reason.
_TOOLS_PARAM_ADDS: dict[tuple[str, str], dict[str, str]] = {}

# Output caps this endpoint has told us about by REFUSING a request ("max_tokens:
# 100000 > 64000, which is the maximum ..."). Memoized like _PARAM_FIXES and for
# the same reason: without it every turn re-pays the 400 that discovers the
# ceiling. Separate dict because the values are numbers, not replacement names.
_MAX_TOKENS_CAPS: dict[tuple[str, str], int] = {}

# What we ask for when nothing declares an output cap (unreachable Models API, a
# gateway that doesn't state one). Deliberately conservative: too HIGH is a 400,
# and this is the value a provider falls back to when it could not ask.
FALLBACK_MAX_OUTPUT = 8192

# Anthropic names the model's real ceiling when it refuses: "max_tokens: 100000 >
# 64000, which is the maximum allowed number of output tokens for <model>".
_MAX_TOKENS_LIMIT_RE = re.compile(r"max_tokens:\s*(\d+)\s*>\s*(\d+)")

# Context windows learned by OVERFLOWING them. Memoized like _MAX_TOKENS_CAPS and
# for the same reason: the probe can be wrong (llama.cpp reports the per-slot
# n_ctx, but a payload also has to fit the answer beside it) and without this
# every turn re-pays the 400 that discovers the truth.
_CTX_CAPS: dict[tuple[str, str], int] = {}

# How badly `len // 4` under-counts THIS model's tokenizer, learned the same way
# and memoized beside it. A `pdftotext -layout` dump costs far more than 4 chars
# per token (measured 1.135 on a service-manual payload), and the estimate is
# what both the fit decision and the UI gauge are made of — an optimistic one
# means deciding "it fits" about a payload the server then refuses.
#
# MONOTONE (max of what we have seen) and clamped: under-counting is the failure
# mode, so a single anomalous refusal must not be able to shrink the budget to
# nothing, and a payload that was once 40% off must not permanently tax an
# all-ASCII one. 2.0 is the ceiling real content reaches (CJK, base64).
_TOKEN_RATIOS: dict[tuple[str, str], float] = {}
_RATIO_MAX = 2.0
# Where a model starts before anything has been learned about it. NOT 1.0: unlike
# _MAX_TOKENS_CAPS, which has no way to know a ceiling before being refused, the
# under-count here is a known property of the len//4 heuristic on anything that
# is not plain English prose. Measured on this corpus (Italian questions over
# `pdftotext -layout` service manuals): 13303 estimated cost 19413 real, and
# 11692 cost 17106 — a factor of 1.46 both times, with 1.17 on a lighter payload.
#
# The floor matters because of a feedback loop: the ratio is learned FROM an
# overflow, and the whole point of the in-turn demotion is that overflows stop
# happening. At 1.0 the guard would stay ~30% optimistic forever on a process
# that never overflows — deciding "it fits" against a number it had no reason to
# believe. 1.15 is deliberately below every value observed: too high wastes
# context, and being wrong in that direction is not self-correcting.
_RATIO_FLOOR = 1.15

# A context overflow NAMES both numbers, which is what makes it recoverable.
# llama.cpp: "request (17450 tokens) exceeds the available context size (16384
# tokens), try increasing it". OpenAI: "This model's maximum context length is
# 8192 tokens. However, your messages resulted in 8500 tokens".
_CTX_OVERFLOW_RES = (
    re.compile(r"request\s*\((\d+)\s*tokens?\)\s*exceeds?\s*the\s*available\s*"
               r"context\s*size\s*\((\d+)", re.I),
    re.compile(r"maximum\s*context\s*length\s*is\s*(\d+)\s*tokens?.{0,80}?"
               r"resulted\s*in\s*(\d+)\s*tokens?", re.I | re.S),
)


def _ctx_from_error(detail: str) -> tuple[int, int] | None:
    """(tokens the payload really cost, the window it has to fit) or None.

    The two orders are swapped between providers, so each pattern says which
    group is which by construction: llama.cpp states used first, OpenAI the
    limit first. A pair that doesn't actually overflow is discarded — acting on
    it would re-send an identical payload and burn an attempt.
    """
    for i, rx in enumerate(_CTX_OVERFLOW_RES):
        m = rx.search(detail)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        used, limit = (a, b) if i == 0 else (b, a)
        if 0 < limit < used:
            return used, limit
    return None


# Appended wherever this module has to cut a tool result to fit the context. A
# severed page reads as a finished one otherwise — the same reason every cap in
# the library tools declares itself.
_TRUNCATION_NOTE = ("\n\n[... this tool result was cut to fit the context window. "
                    "Call the tool again for the rest if you need it.]")

#: Below this, cutting a tool result buys less context than the note costs
#: honesty: it would mark a complete result as partial. See _trim_tool_results.
_MIN_TRIM_CHARS = 400


def _clamp_from_error(detail: str, current: int) -> int | None:
    """The ceiling named in a max_tokens refusal, or None if none is stated.

    Only a value strictly BELOW what we asked for counts: anything else would
    have the retry loop re-send an identical payload and burn an attempt.
    """
    m = _MAX_TOKENS_LIMIT_RE.search(detail)
    if not m:
        return None
    cap = int(m.group(2))
    return cap if 0 < cap < current else None


def _accumulate(chunk: dict, text: str, reasoning: str, tools: dict) -> tuple[str, str]:
    """Rebuild the reply as it streams, for the API log only.

    Deliberately separate from the executor's own accumulation: this must never
    change what the caller sees, so it only reads the chunks going past.
    """
    try:
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
    except Exception:
        return text, reasoning
    if delta.get("content"):
        text += delta["content"]
    if delta.get("reasoning_content"):
        reasoning += delta["reasoning_content"]
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        cur = tools.setdefault(idx, {"id": tc.get("id"), "type": "function",
                                     "function": {"name": "", "arguments": ""}})
        fn = tc.get("function") or {}
        if fn.get("name"):
            cur["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            cur["function"]["arguments"] += fn["arguments"]
    return text, reasoning


class LLMProvider:
    """Unified LLM interface via OpenAI-compatible chat completions API.
    Works identically for Ollama and llama.cpp.
    """

    def __init__(self, model_config: ModelConfig):
        self.config = model_config
        self._endpoint = self._resolve_endpoint()
        # Vision inference on local models can be slow (image encode + prefill),
        # so use a generous read timeout. Configurable via model options.
        read_timeout = float(model_config.options.get("request_timeout", 600))
        # Remote OpenAI-compatible providers authenticate with a Bearer token;
        # the Anthropic Messages API wants x-api-key + a pinned API version.
        headers = {}
        if model_config.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if model_config.api_key:
                headers["x-api-key"] = model_config.api_key
        elif model_config.api_key:
            headers["Authorization"] = f"Bearer {model_config.api_key}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(read_timeout, connect=10.0),
            headers=headers,
        )
        # Native tool calling: None = ask the endpoint (tools are sent, and a 400
        # downgrades this process to the text protocol — see _adapt_payload).
        # An explicit ModelConfig.supports_tools skips the probe: False is the
        # escape hatch for a server that accepts `tools` but never emits a
        # tool_call, True for one that only reveals it mid-conversation.
        # llama.cpp used to be pinned to False here; it does support tool calls
        # (streaming included) with a tool-capable chat template, and the text
        # protocol costs the model a whole extra convention to follow.
        self.supports_tools: bool | None = model_config.supports_tools
        # Token budget for one request: resolved lazily on the first call (the
        # probe is async and cached process-wide, so this costs nothing after
        # the first turn). See model_probe.
        self._ctx_budget: int | None = None
        # Same for the output cap (Anthropic enforces one per request).
        self._max_out: int | None = None
        # Payload fields this endpoint has already rejected (see _PARAM_FIXES).
        self._param_fixes = _PARAM_FIXES.setdefault(
            (model_config.base_url, model_config.model), {}
        )
        # Parameters this endpoint wants alongside `tools` (see _TOOLS_PARAM_ADDS).
        self._tools_param_adds = _TOOLS_PARAM_ADDS.setdefault(
            (model_config.base_url, model_config.model), {}
        )
        self._caps_key = (model_config.base_url, model_config.model)
        # An endpoint that already rejected `tools` won't accept them next run:
        # start in the text protocol so the executor injects its instructions
        # from the first iteration. Only in auto mode — an explicit config
        # value always wins.
        if self.supports_tools is None and "tools" in self._param_fixes:
            self.supports_tools = False

    # Who is making the call, for the raw API log. Set by the caller — the
    # executor stamps agent+iteration, the auto-router "auto-route", the memory
    # compactor "memory". Default is honest rather than empty: an unlabelled
    # call in the log is a call nobody can attribute.
    trace_label: str = "llm"

    def _resolve_endpoint(self) -> str:
        """Accept both base-URL conventions: with or without a trailing /v1
        (e.g. "https://api.openai.com/v1" and "http://localhost:11434")."""
        base = self.config.base_url.rstrip("/")
        if self.config.provider == "anthropic":
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            return f"{base}/messages"
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Convert tool-related messages to plain text for models that
        don't support role:tool in conversation history.

        The rebuilt list must have the shape the TEXT protocol already uses in
        the executor: the CALL as an assistant message, the RESULT as the user
        message that follows. Folding both into one assistant message produced a
        run of consecutive assistant messages, and llama.cpp reads a trailing
        assistant message as a prefill to continue — it refuses two of them
        outright ("Cannot have 2 or more assistant messages at the end of the
        list", observed 2026-08-27) and, with only one, would have had the model
        continue its own tool-call transcript instead of answering.
        """
        sanitized: list[dict] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            role = m.get("role")

            if role == "assistant" and m.get("tool_calls"):
                calls, results = [], []
                for tc in m["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "unknown")
                    args = func.get("arguments", "{}")
                    # Find matching tool result
                    result = ""
                    tc_id = tc.get("id", "")
                    j = i + 1
                    while j < len(messages) and messages[j].get("role") == "tool":
                        if messages[j].get("tool_call_id") == tc_id or not tc_id:
                            result = messages[j].get("content", "")
                            break
                        j += 1
                    # The marker is a prompts.py constant: the history matcher
                    # (is_scaffolding_message) keys on it, and a rewording here
                    # that missed the matcher would leak plumbing into history.
                    calls.append(f"{prompts.SANITIZED_TOOL_PREFIX} '{name}' with {args}]")
                    results.append(f"{name}: {result}" if result else f"{name}: (no output)")
                if m.get("content"):
                    calls.insert(0, str(m["content"]))
                sanitized.append({"role": "assistant", "content": "\n".join(calls)})
                sanitized.append({
                    "role": "user",
                    "content": prompts.TOOL_RESULTS_PREFIX + "\n" + "\n\n".join(results),
                })
                # Skip tool result messages that follow
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    i += 1
                continue

            elif role == "tool":
                # Orphaned tool result (no preceding assistant call) — it is
                # still a RESULT, so it takes the user side of the protocol.
                sanitized.append({
                    "role": "user",
                    "content": prompts.TOOL_RESULTS_PREFIX + "\n" + str(m.get("content") or ""),
                })
            else:
                clean = {"role": role, "content": m.get("content", "")}
                if m.get("name"):
                    clean["name"] = m["name"]
                sanitized.append(clean)
            i += 1
        return LLMProvider._merge_consecutive_assistants(sanitized)

    @staticmethod
    def _merge_consecutive_assistants(messages: list[dict]) -> list[dict]:
        """Collapse adjacent assistant messages into one.

        A backstop, not the mechanism: the caller above already alternates. But
        the input list can arrive with two assistant turns in a row (a forced
        synthesis appended after a stopped turn), and llama.cpp refuses that
        outright at the end of the list — so this is cheaper than a 400 whose
        message names nothing the caller did.
        """
        merged: list[dict] = []
        for m in messages:
            prev = merged[-1] if merged else None
            if (prev and prev.get("role") == "assistant" and m.get("role") == "assistant"
                    and isinstance(prev.get("content"), str)
                    and isinstance(m.get("content"), str)):
                merged[-1] = {**prev,
                              "content": (prev["content"] + "\n\n" + m["content"]).strip()}
                continue
            merged.append(m)
        return merged

    @staticmethod
    def _estimate_tokens(content) -> int:
        """Rough token estimate: ~4 chars per token for English/Italian.
        Handles multimodal content lists (text parts + fixed cost per image)."""
        if isinstance(content, list):
            total = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += len(part.get("text", "")) // 4 + 1
                elif part.get("type") == "image_url":
                    total += 512  # rough placeholder cost for an image
            return total
        if not isinstance(content, str):
            return 0
        return len(content) // 4 + 1

    @classmethod
    def is_tool_scaffolding(cls, m: dict) -> bool:
        """True for a message that carries a TOOL RESULT rather than dialogue.

        Two shapes, because a result reaches the payload by two routes: role:tool
        (native protocol) and the user message that carries it in the text
        protocol — the executor's and _sanitize_messages' alike. Both are
        plumbing, and both are where a long turn's overflow actually lives.

        Deliberately NOT the assistant side. The only assistant scaffolding in a
        payload is the CALL transcript, which is short and must stay intact: a
        severed `[Called tool 'x' with {…` is malformed JSON in the history, and
        the models this protocol exists for imitate what they read.
        """
        role = m.get("role")
        if role == "tool":
            return True
        content = m.get("content")
        return (role == "user" and isinstance(content, str)
                and content.startswith(prompts.TOOL_RESULTS_PREFIX))

    def _trim_tool_results(self, messages: list[dict], budget: int,
                           total: int) -> tuple[list[dict], int]:
        """Shrink tool results, OLDEST first, until the estimate fits `budget`.

        Deliberately breaks the "tool results are never truncated" rule of the
        executor's sliding window, and only here: at this point the alternative
        is not a fuller context, it is a failed turn. Oldest first because the
        model is about to reason from the most RECENT result, and the last one
        is trimmed only if nothing else was enough. Every cut is DECLARED, or
        the model reads a severed page as a finished one.
        """
        result = list(messages)
        idx = [i for i, m in enumerate(result) if self.is_tool_scaffolding(m)]
        if not idx:
            return result, total
        # The newest result goes last in line, not out of it.
        for i in idx[:-1] + [idx[-1]]:
            if total <= budget:
                break
            content = result[i].get("content")
            if not isinstance(content, str):
                continue
            was = self._estimate_tokens(content)
            keep = max(0, min(was, budget - (total - was)))
            head = content[:keep * 4] if keep else ""
            # A cut that saves almost nothing still DECLARES itself, which reads
            # as "there was more" on a result that is in fact complete. Skip it
            # and let the next (older) message carry the reduction.
            if len(content) - len(head) < _MIN_TRIM_CHARS:
                continue
            trimmed = head + _TRUNCATION_NOTE
            result[i] = {**result[i], "content": trimmed}
            total += self._estimate_tokens(trimmed) - was
            log.warning("Trimmed a tool result from %d to %d chars to fit the context",
                        len(content), len(trimmed))
        return result, total

    def _truncate_messages(self, messages: list[dict], max_tokens: int,
                           reserve: int = 0) -> list[dict]:
        """Best-effort context guard: if the estimated total exceeds the budget,
        trim the tool results (oldest first), then the LAST user message (the
        other usual overflow source: pasted files). Earlier history is already
        capped by the executor's sliding window.

        `reserve` is what the payload costs BESIDE the messages — the tool
        definitions and the room the answer needs. Without it the guard measures
        the wrong thing: it passed a payload that the server then refused for
        104 tokens (observed 2026-08-27, a librarian reading five PDF pages).
        """
        max_tokens = max(512, max_tokens - reserve)
        total = sum(self._estimate_tokens(m.get("content") or "") for m in messages)
        if total <= max_tokens:
            return messages
        messages, total = self._trim_tool_results(messages, max_tokens, total)
        if total <= max_tokens:
            return messages

        # Reserve tokens for system prompt + response
        system_tokens = self._estimate_tokens(messages[0].get("content", "")) if messages else 0
        reserve = system_tokens + 512  # 512 tokens for response
        available = max_tokens - reserve
        if available < 200:
            available = 200

        result = list(messages)
        # Find and truncate the longest user message
        for i in range(len(result) - 1, -1, -1):
            if result[i].get("role") == "user":
                content = result[i].get("content", "")
                if not isinstance(content, str):
                    break  # multimodal content (images) — do not slice
                content_tokens = self._estimate_tokens(content)
                if content_tokens > available:
                    max_chars = available * 4
                    truncated = content[:max_chars]
                    # Cut at last sentence/paragraph boundary
                    for sep in ["\n\n", "\n", ". ", ", "]:
                        idx = truncated.rfind(sep)
                        if idx > max_chars // 2:
                            truncated = truncated[:idx + len(sep)]
                            break
                    result[i] = {**result[i], "content": truncated + "\n\n[... testo troncato per limiti del modello]"}
                    log.warning("Truncated user message from %d to %d chars (est. %d -> %d tokens)",
                                len(content), len(truncated), content_tokens, self._estimate_tokens(truncated))
                break
        return result

    async def _context_budget(self) -> int:
        """Token budget for one request, resolved once per provider instance.

        Ask the model server instead of trusting a hand-typed number: llama.cpp
        reports its per-slot n_ctx, Ollama the loaded/trained window. An
        explicit config value still wins (see model_probe.resolve).
        """
        if self._ctx_budget is None:
            self._ctx_budget = await model_probe.context_budget(
                self.config, client=self._client
            )
            log.debug("Context budget for '%s': %d tokens",
                      self.config.id, self._ctx_budget)
        return self._ctx_budget

    async def _max_output(self) -> int:
        """The per-request output cap, resolved once per provider instance.

        Same shape as _context_budget, one asymmetry: a cap discovered from a
        400 (_MAX_TOKENS_CAPS) beats everything, INCLUDING an explicit config
        value. The endpoint has stated its own ceiling; honouring a higher
        number typed in the form would just fail the turn again.
        """
        if self._max_out is None:
            budget = await model_probe.max_output_budget(
                self.config, client=self._client
            ) or FALLBACK_MAX_OUTPUT
            known = _MAX_TOKENS_CAPS.get(self._caps_key)
            self._max_out = min(budget, known) if known else budget
            log.debug("Output cap for '%s': %d tokens",
                      self.config.id, self._max_out)
        return self._max_out

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        stream: bool,
        max_ctx: int,
    ) -> dict:
        remote = self.config.provider == "openai"

        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": stream,
        }
        # Only include tools if model supports them
        if tools and self.supports_tools is not False:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        elif self.config.options.get("temperature") is not None:
            payload["temperature"] = self.config.options["temperature"]

        # Sampling passthrough. Remote OpenAI-compatible APIs reject unknown
        # parameters with 400, so only the standard ones are forwarded there;
        # Ollama/llama.cpp accept the extended set.
        standard = ("top_p", "frequency_penalty", "presence_penalty")
        extended = ("top_k", "repeat_penalty", "repeat_last_n", "min_p")
        allowed = standard if remote else standard + extended
        for key in allowed:
            if key in self.config.options:
                payload[key] = self.config.options[key]

        # Ollama allocates the KV cache for whatever num_ctx we ask for, so an
        # explicit context_window has to be requested to take effect. llama.cpp
        # fixes its window at launch (-c) and ignores the field; remote APIs
        # reject it — for them context_window is only OUR truncation budget.
        if self.config.provider == "ollama" and self.config.context_window:
            payload["num_ctx"] = self.config.context_window

        # Cap a single response so a small LOCAL model can't run away into an
        # infinite repetition loop (a hard backstop even without a repeat
        # penalty). Remote providers don't need this crutch and some reject a
        # low cap or the `max_tokens` name outright, so it's only applied when
        # explicitly set for them. Overridable per model via options; 0 disables.
        default_max = None if remote else 2048
        max_tokens = self.config.options.get("max_tokens", default_max)
        if max_tokens:
            payload["max_tokens"] = max_tokens

        # Re-apply what this endpoint demanded alongside `tools` — BEFORE the
        # fixes below, so a later "drop this param" verdict still wins.
        if "tools" in payload:
            for key, value in self._tools_param_adds.items():
                payload.setdefault(key, value)

        # Re-apply what this endpoint already told us it doesn't accept. The
        # "tools" sentinel is NOT a payload fix: it acts through supports_tools
        # above (and an explicit supports_tools=True must be able to override it).
        for key, replacement in self._param_fixes.items():
            if key != "tools" and key in payload:
                value = payload.pop(key)
                if replacement:
                    payload[replacement] = value

        # Fit the context window (probed, not guessed) — LAST, so the reserve
        # can be measured against the finished payload: the tool definitions are
        # part of the prompt, and the answer needs room beside it.
        learned = _CTX_CAPS.get(self._caps_key)
        if learned:
            max_ctx = min(max_ctx, learned)
        payload["messages"] = self._truncate_messages(
            payload["messages"], max_ctx, reserve=self._payload_overhead(payload))
        return payload

    def _payload_overhead(self, payload: dict) -> int:
        """What the payload costs beside the messages: tool schemas + answer room."""
        tools = payload.get("tools") or []
        overhead = self._estimate_tokens(json.dumps(tools)) if tools else 0
        overhead += (payload.get("max_tokens")
                     or payload.get("max_completion_tokens") or 0)
        return overhead + 64  # chat-template scaffolding per message

    # ------------------------------------------------------------------
    # How full is the context — ONE definition, three readers: the executor's
    # in-turn demotion, the last-resort trim below, and the UI gauge.
    # ------------------------------------------------------------------
    def token_ratio(self) -> float:
        """The correction for this model's tokenizer: learned if we have been
        refused once, else the conservative floor (see _RATIO_FLOOR)."""
        return _TOKEN_RATIOS.get(self._caps_key, _RATIO_FLOOR)

    def _learn_ratio(self, used: int, estimated: int) -> float:
        """Record what the endpoint says a payload we estimated at `estimated`
        actually cost, and return the ratio now in force.

        `estimated` MUST already include the tool schemas: `used` is the whole
        request, and dividing it by a messages-only estimate conflates the
        per-character under-count with the overhead — which is then subtracted a
        second time by the caller. On the measured turn that produced 1.39
        against a true 1.135, i.e. it threw away ~1900 tokens of usable window.
        """
        if estimated > 0 and used > 0:
            ratio = min(_RATIO_MAX, max(1.0, used / estimated))
            _TOKEN_RATIOS[self._caps_key] = max(
                _TOKEN_RATIOS.get(self._caps_key, _RATIO_FLOOR), ratio)
        return self.token_ratio()

    def estimate_payload_tokens(self, messages: list[dict],
                                tools: list[dict] | None = None) -> int:
        """Ratio-corrected estimate of what `messages` (+ `tools`) will cost."""
        est = sum(self._estimate_tokens(m.get("content") or "") for m in messages)
        if tools:
            est += self._estimate_tokens(json.dumps(tools))
        return int(est * self.token_ratio())

    async def context_state(self, messages: list[dict],
                            tools: list[dict] | None = None) -> dict:
        """``{window, used, reserve, fit, source, ratio}`` — how full we are.

        `window` applies the _CTX_CAPS clamp, which `_context_budget()` alone
        does NOT: that clamp lives in _build_payload, so a caller asking the
        probe directly would decide "we fit" against a window this provider is
        about to shrink — and the 400-then-trim dance would come straight back.

        `fit` is what the MESSAGES may cost, i.e. the window minus the tool
        schemas and the room the answer needs. Deciding against the raw window
        is the bug `reserve=` was added to fix.
        """
        window = await self._context_budget()
        learned = _CTX_CAPS.get(self._caps_key)
        source = "served"
        if learned and learned < window:
            window, source = learned, "learned"
        max_out = self.config.options.get("max_tokens")
        if max_out is None:
            max_out = 0 if self.config.provider == "openai" else 2048
        reserve = (self._estimate_tokens(json.dumps(tools)) if tools else 0)
        reserve += int(max_out or 0) + 64
        return {
            "window": window,
            "used": self.estimate_payload_tokens(messages, tools),
            "reserve": reserve,
            "fit": max(512, window - reserve),
            "source": source,
            "ratio": round(self.token_ratio(), 3),
        }

    # ------------------------------------------------------------------
    # Anthropic Messages API (provider "anthropic")
    #
    # The rest of the app speaks OpenAI chat-completions end to end: the
    # executor builds OpenAI-style messages/tools and consumes OpenAI-style
    # stream chunks. The Anthropic support is therefore a translation layer
    # confined to this class — requests are translated on the way out and
    # SSE events back into `{"choices":[{"delta":...}]}` chunks on the way
    # in, so executor, ReasoningSplitter and memory_compactor stay untouched.
    # ------------------------------------------------------------------

    @staticmethod
    def _anthropic_tool(tool: dict) -> dict:
        """OpenAI function definition -> Anthropic tool definition."""
        func = tool.get("function", tool)
        return {
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters")
            or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _anthropic_user_blocks(content) -> list[dict]:
        """OpenAI user content (string or multimodal parts) -> content blocks."""
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content.strip() else []
        blocks: list[dict] = []
        if not isinstance(content, list):
            return blocks
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text", "")
                if text.strip():
                    blocks.append({"type": "text", "text": text})
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    # data:<media_type>;base64,<data>
                    header, _, data = url.partition(",")
                    media_type = header[5:].split(";", 1)[0] or "image/png"
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64",
                                   "media_type": media_type, "data": data},
                    })
                elif url:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
            else:
                # e.g. input_audio: the Messages API has no audio input.
                log.warning("Anthropic provider: dropping unsupported "
                            "content part type %r", ptype)
        return blocks

    def _anthropic_messages(self, messages: list[dict]) -> list[dict]:
        """OpenAI-style history -> Anthropic messages.

        system entries are hoisted by the caller; assistant tool_calls become
        tool_use blocks and role:"tool" results become tool_result blocks in
        a user message. Same-role neighbours are merged (tool results MUST
        land in one user turn for parallel calls, and it keeps the strict
        user-first alternation happy)."""
        out: list[dict] = []

        def append(role: str, blocks: list[dict]) -> None:
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            if role == "tool":
                append("user", [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": str(m.get("content") or ""),
                }])
            elif role == "assistant":
                blocks = []
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    # Never emit an empty text block: the API rejects it.
                    blocks.append({"type": "text", "text": content})
                for tc in m.get("tool_calls") or []:
                    func = tc.get("function", {})
                    try:
                        args = json.loads(func.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": args,
                    })
                append("assistant", blocks)
            else:
                append("user", self._anthropic_user_blocks(m.get("content")))
        return out

    def _build_anthropic_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        max_ctx: int,
        max_out: int,
    ) -> dict:
        # Truncate BEFORE translating: the estimator understands the
        # OpenAI-style shapes (strings, image_url parts). The reserve is the
        # same idea as in _build_payload — the tool schemas are part of the
        # prompt, and max_tokens is a hard output cap the input has to fit
        # beside.
        learned = _CTX_CAPS.get(self._caps_key)
        if learned:
            max_ctx = min(max_ctx, learned)
        reserve = max_out + 64
        if tools and self.supports_tools is not False:
            reserve += self._estimate_tokens(json.dumps(tools))
        messages = self._truncate_messages(messages, max_ctx, reserve=reserve)

        system = "\n\n".join(
            m["content"] for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
        payload = {
            "model": self.config.model,
            "messages": self._anthropic_messages(messages),
            "stream": True,
            # Required by the Messages API (it is a hard output cap), and asked
            # for rather than guessed — see model_probe.max_output_budget.
            "max_tokens": max_out,
        }
        if system:
            payload["system"] = system
        if tools and self.supports_tools is not False:
            payload["tools"] = [self._anthropic_tool(t) for t in tools]
            payload["tool_choice"] = {"type": "auto"}
        if temperature is not None:
            payload["temperature"] = temperature
        elif self.config.options.get("temperature") is not None:
            payload["temperature"] = self.config.options["temperature"]
        # frequency/presence penalty don't exist here; top_k does.
        for key in ("top_p", "top_k"):
            if key in self.config.options:
                payload[key] = self.config.options[key]

        # Re-apply what this endpoint already rejected (newer Claude models
        # 400 on explicit sampling params — see _adapt_payload).
        for key, replacement in self._param_fixes.items():
            if key != "tools" and key in payload:
                value = payload.pop(key)
                if replacement:
                    payload[replacement] = value
        return payload

    def _anthropic_chunks(self, event: dict, tool_idx: dict[int, int]) -> list[dict]:
        """One Anthropic SSE event -> zero or more OpenAI-style stream chunks.

        tool_idx maps the event's content-block index to the OpenAI tool_call
        index the executor accumulates on (input_json_delta carries only the
        block index, and text blocks in between must not shift tool indices).
        """
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = len(tool_idx)
                tool_idx[event.get("index", 0)] = idx
                return [{"choices": [{"delta": {"tool_calls": [{
                    "index": idx,
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {"name": block.get("name", ""), "arguments": ""},
                }]}}]}]
            return []
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return [{"choices": [{"delta": {"content": delta.get("text", "")}}]}]
            if dtype == "thinking_delta":
                # Lands on the reasoning channel via ReasoningSplitter.
                return [{"choices": [{"delta": {
                    "reasoning_content": delta.get("thinking", "")}}]}]
            if dtype == "input_json_delta":
                idx = tool_idx.get(event.get("index", 0))
                if idx is None:
                    return []
                return [{"choices": [{"delta": {"tool_calls": [{
                    "index": idx,
                    "function": {"arguments": delta.get("partial_json", "")},
                }]}}]}]
            return []
        if etype == "message_delta":
            stop = (event.get("delta") or {}).get("stop_reason")
            if stop == "refusal":
                # Safety classifiers decline with HTTP 200: without this the
                # user would just see an empty reply.
                details = event.get("delta", {}).get("stop_details") or {}
                note = details.get("explanation") or ""
                text = "[Request declined by the provider's safety filters"
                text += f": {note}]" if note else ".]"
                return [{"choices": [{"delta": {"content": text}}]}]
            if stop == "max_tokens":
                # The hard output cap cut the turn off mid-generation. Staying
                # silent here is the worst outcome: when the cut lands inside a
                # tool call's input_json_delta, the executor gets unparseable
                # arguments and the model retries blind, with no idea it hit a
                # ceiling (observed 2026-08-10, a file_write of a ~10 KB
                # script). Content and not an `error` event, like the refusal
                # above: whatever was generated is still worth keeping, and the
                # note lands in the turn the model reads back.
                cap = int(self.config.options.get("max_tokens") or 8192)
                return [{"choices": [{"delta": {"content": (
                    f"\n\n[Output truncated: this reply hit the max_tokens cap "
                    f"({cap}). Nothing above is necessarily complete. Raise "
                    f"max_tokens in the model options, or produce less per call "
                    f"— write a large file in several smaller pieces.]"
                )}}]}]
            return []
        if etype == "error":
            message = (event.get("error") or {}).get("message", "unknown error")
            raise RuntimeError(f"Anthropic stream error: {message}")
        # ping, message_start, content_block_stop, message_stop
        return []

    async def chat_completion_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[dict]:
        anthropic = self.config.provider == "anthropic"
        if anthropic:
            payload = self._build_anthropic_payload(
                messages, tools, temperature,
                max_ctx=await self._context_budget(),
                max_out=await self._max_output())
        else:
            payload = self._build_payload(messages, tools, temperature, stream=True,
                                          max_ctx=await self._context_budget())

        # A 400 from an OpenAI-compatible endpoint usually means "this model
        # doesn't accept that field" rather than a real failure: adapt the
        # payload (drop the rejected param — or add one the endpoint demands
        # next to `tools` — then fall back to no tools) and retry until
        # nothing is left to adapt. Bound = every droppable param + the
        # reasoning_effort add + the no-tools fallback, plus the final attempt.
        # EVERY model call is traced here, whoever made it: this is the one
        # choke point, which is why the auto-routing classifier and the memory
        # compactor show up in the log without knowing anything about it.
        attempt = 0
        for _ in range(len(DROPPABLE_PARAMS) + 3):
            attempt += 1
            label = (self.trace_label if attempt == 1
                     else f"{self.trace_label} retry {attempt}")
            trace.call(label, self._endpoint, payload)
            seen_text, seen_tools, seen_reasoning = "", {}, ""
            async with self._client.stream("POST", self._endpoint, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    detail = self._error_message(body)
                    if resp.status_code == 400 and self._adapt_payload(payload, detail):
                        log.warning("Model '%s' rejected the request (%s) — retrying adapted",
                                    self.config.model, detail or "no detail")
                        trace.reply(label, "", error=f"HTTP 400 ({detail or 'no detail'}) "
                                                     f"— payload adapted, retrying")
                        continue
                    # Nothing left to adapt (or 401/404/500...): surface the
                    # provider's own message when there is one — "credit
                    # balance is too low" beats a bare "400 Bad Request" in
                    # the chat UI (the executor shows str(exception)).
                    self._log_error_body(body, streaming=True)
                    trace.reply(label, "", error=f"HTTP {resp.status_code}: "
                                                 f"{detail or body[:400]!r}")
                    if detail:
                        raise RuntimeError(
                            f"LLM provider error (HTTP {resp.status_code}): {detail}")
                    resp.raise_for_status()

                if self.supports_tools is None and "tools" in payload:
                    self.supports_tools = True
                # Anthropic streams typed events (the type is inside the data
                # JSON; the SSE `event:` lines are redundant and ignored here).
                tool_idx: dict[int, int] = {}
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if anthropic:
                            for translated in self._anthropic_chunks(chunk, tool_idx):
                                seen_text, seen_reasoning = _accumulate(
                                    translated, seen_text, seen_reasoning, seen_tools)
                                yield translated
                        else:
                            seen_text, seen_reasoning = _accumulate(
                                chunk, seen_text, seen_reasoning, seen_tools)
                            yield chunk
                trace.reply(label, seen_text,
                            [seen_tools[k] for k in sorted(seen_tools)] or None,
                            seen_reasoning)
                return

        # Unreachable: each adaptation can fire at most once per payload.
        raise RuntimeError(f"Model '{self.config.model}' kept rejecting the request")

    def _adapt_payload(self, payload: dict, detail: str) -> bool:
        """Make a 400-rejected payload acceptable, if we can tell how.

        Returns True when the payload was changed and the call is worth
        retrying. Named fields go first (a rejected `temperature` is not a
        reason to give up tool calling), then params an endpoint demands next
        to `tools` (reasoning_effort), and the no-tools fallback last.
        """
        low = detail.lower()
        anthropic = self.config.provider == "anthropic"

        ctx = _ctx_from_error(detail)
        if ctx:
            # The refusal names the real window, so learn it and re-fit — the
            # same shape as the max_tokens cap above. This branch has to come
            # FIRST: a context overflow says nothing about tool support, but the
            # no-tools branch at the bottom catches every unexplained 400, so
            # without it one long payload downgraded a tool-capable model to the
            # text protocol process-wide, then failed anyway on the same
            # overflow — and reported llama.cpp's complaint about the rebuilt
            # message list instead of the real cause (observed 2026-08-27).
            used, limit = ctx
            _CTX_CAPS[self._caps_key] = limit
            self._ctx_budget = limit
            msgs = payload.get("messages") or []
            before = sum(self._estimate_tokens(m.get("content") or "") for m in msgs)
            # Learn how far off the estimate is, against the WHOLE payload — see
            # _learn_ratio: dividing `used` by a messages-only figure double-counts
            # the overhead and over-trims by ~14% of the window, for good.
            ratio = self._learn_ratio(
                used, before + self._estimate_tokens(json.dumps(payload.get("tools") or [])))
            budget = int((limit - self._payload_overhead(payload)) / ratio)
            payload["messages"] = self._truncate_messages(msgs, budget)
            after = sum(self._estimate_tokens(m.get("content") or "")
                        for m in payload["messages"])
            if after < before:
                log.warning("Model '%s' context is %d tokens (payload cost %d); "
                            "trimmed est. %d -> %d", self.config.id, limit, used,
                            before, after)
                return True
            # Nothing left to trim: surface the real error instead of trading it
            # for a misleading one.
            return False

        for key in DROPPABLE_PARAMS:
            if key not in payload or key not in low:
                continue
            if anthropic and key == "max_tokens":
                # Required by the Messages API: dropping it would only trade
                # this 400 for another. But the refusal NAMES the model's real
                # ceiling, so clamp to it and retry — a value that is too high
                # (typed into the form, or a probe that never ran) self-corrects
                # instead of failing the turn. Remembered process-wide so the
                # next turn starts at the ceiling.
                cap = _clamp_from_error(detail, payload["max_tokens"])
                if cap is None:
                    continue  # unparseable: surface the original error
                payload["max_tokens"] = cap
                _MAX_TOKENS_CAPS[self._caps_key] = cap
                self._max_out = cap
                log.warning("Model '%s' caps output at %d tokens; clamped",
                            self.config.id, cap)
                return True
            # Some models want the same value under a different name.
            replacement = ("max_completion_tokens"
                           if key == "max_tokens" and "max_completion_tokens" in low
                           else None)
            value = payload.pop(key)
            if replacement:
                payload[replacement] = value
            self._param_fixes[key] = replacement
            return True

        if ("tools" in payload and "reasoning_effort" in low
                and payload.get("reasoning_effort") != "none"):
            # "Function tools with reasoning_effort are not supported ... set
            # reasoning_effort to 'none'": the refusal names its own fix, and
            # taking it keeps native tool calling. The != "none" guard stops
            # this from re-sending an identical payload if the endpoint rejects
            # the combination anyway — the next 400 then falls through to the
            # no-tools branch below.
            payload["reasoning_effort"] = "none"
            self._tools_param_adds["reasoning_effort"] = "none"
            return True

        if "tools" in payload:
            # Retry without tools, with tool messages sanitized out of the
            # conversation (models that reject `tools` also reject role:tool).
            # Remembered process-wide so later runs start in text mode.
            self.supports_tools = False
            self._param_fixes["tools"] = None
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            if not anthropic:
                # Anthropic payloads carry already-translated content blocks;
                # the sanitizer only understands the OpenAI message shape
                # (and Anthropic never rejects native tools anyway).
                payload["messages"] = self._sanitize_messages(payload["messages"])
            return True

        return False

    @staticmethod
    def _error_message(body: bytes) -> str:
        """The provider's human-readable error message, if the body carries one."""
        try:
            err_body = json.loads(body)
            return err_body.get("error", {}).get("message", "") or str(err_body)
        except Exception:
            return body[:200].decode(errors="replace") if body else ""

    @classmethod
    def _log_error_body(cls, body: bytes, streaming: bool = False) -> None:
        """Log the provider's error message, if the body carries one."""
        err_msg = cls._error_message(body)
        if err_msg:
            log.error("LLM %serror detail: %s", "stream " if streaming else "", err_msg)

    def explain_error(self, e: Exception) -> str:
        """Turn a transport exception into a sentence that names the fix.

        The raw httpx text is "All connection attempts failed" — no provider, no
        URL, nothing to act on — and it travels untouched from the executor to
        the chat bubble, the Telegram reply and the autonomy log. This is the
        only class that knows which backend it was talking to and where, so it
        is the only place that can say so.

        The original message is always appended: httpcore raises ConnectError
        for TLS and DNS failures too, so "is it running?" must never be the
        whole answer. Anything that is not a transport error keeps today's text.
        """
        try:
            base = (self.config.base_url or "").rstrip("/")
            detail = str(e) or type(e).__name__
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                if self.config.provider == "ollama":
                    fix = (f"Ollama is not answering at {base} — start it with "
                           f"'ollama serve', or pick another model in Settings.")
                elif self.config.provider == "llamacpp":
                    fix = (f"The llama.cpp server is not answering at {base} — "
                           f"start it with 'llama-server -m <model.gguf> --port "
                           f"8080 --jinja', or pick another model in Settings.")
                else:
                    fix = (f"Cannot reach {base} — check the connection and this "
                           f"model's base URL under Models.")
                return f"{fix} ({detail})"
            if isinstance(e, httpx.ReadTimeout):
                # The opposite advice: the model is there, it is just slow.
                timeout = self.config.options.get("request_timeout", 600)
                return (f"'{self.config.model}' at {base} did not answer within "
                        f"{timeout}s. Raise request_timeout in the model's "
                        f"options, or use a smaller model. ({detail})")
            return detail
        except Exception:  # pragma: no cover — runs inside an except block
            return str(e) or type(e).__name__

    async def close(self):
        await self._client.aclose()
