from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from app.engine import model_probe, prompts
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
        # Payload fields this endpoint has already rejected (see _PARAM_FIXES).
        self._param_fixes = _PARAM_FIXES.setdefault(
            (model_config.base_url, model_config.model), {}
        )
        # An endpoint that already rejected `tools` won't accept them next run:
        # start in the text protocol so the executor injects its instructions
        # from the first iteration. Only in auto mode — an explicit config
        # value always wins.
        if self.supports_tools is None and "tools" in self._param_fixes:
            self.supports_tools = False

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
        Merges adjacent assistant(tool_calls) + tool(result) into one message."""
        sanitized = []
        i = 0
        while i < len(messages):
            m = messages[i]
            role = m.get("role")

            if role == "assistant" and m.get("tool_calls"):
                # Merge with following tool result messages
                parts = []
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
                    parts.append(
                        f"{prompts.SANITIZED_TOOL_PREFIX} '{name}' with {args}]\n"
                        f"Result: {result}"
                    )
                sanitized.append({
                    "role": "assistant",
                    "content": "\n\n".join(parts),
                })
                # Skip tool result messages that follow
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    i += 1
                continue

            elif role == "tool":
                # Orphaned tool result (no preceding assistant) — merge as assistant
                content = m.get("content", "")
                sanitized.append({
                    "role": "assistant",
                    "content": f"[Tool result]: {content}",
                })
            else:
                clean = {"role": role, "content": m.get("content", "")}
                if m.get("name"):
                    clean["name"] = m["name"]
                sanitized.append(clean)
            i += 1
        return sanitized

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

    def _truncate_messages(self, messages: list[dict], max_tokens: int) -> list[dict]:
        """Best-effort context guard: if the estimated total exceeds max_tokens,
        trim the LAST user message (the usual overflow source: pasted files).
        Earlier history is already capped by the executor's sliding window."""
        total = sum(self._estimate_tokens(m.get("content") or "") for m in messages)
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

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        stream: bool,
        max_ctx: int,
    ) -> dict:
        remote = self.config.provider == "openai"

        # Auto-truncate messages to fit the context window (probed, not guessed).
        messages = self._truncate_messages(messages, max_ctx)

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

        # Re-apply what this endpoint already told us it doesn't accept. The
        # "tools" sentinel is NOT a payload fix: it acts through supports_tools
        # above (and an explicit supports_tools=True must be able to override it).
        for key, replacement in self._param_fixes.items():
            if key != "tools" and key in payload:
                value = payload.pop(key)
                if replacement:
                    payload[replacement] = value
        return payload

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
    ) -> dict:
        # Truncate BEFORE translating: the estimator understands the
        # OpenAI-style shapes (strings, image_url parts).
        messages = self._truncate_messages(messages, max_ctx)

        system = "\n\n".join(
            m["content"] for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)
        )
        payload = {
            "model": self.config.model,
            "messages": self._anthropic_messages(messages),
            "stream": True,
            # Required by the Messages API (it is a hard output cap).
            "max_tokens": int(self.config.options.get("max_tokens") or 8192),
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
                max_ctx=await self._context_budget())
        else:
            payload = self._build_payload(messages, tools, temperature, stream=True,
                                          max_ctx=await self._context_budget())

        # A 400 from an OpenAI-compatible endpoint usually means "this model
        # doesn't accept that field" rather than a real failure: adapt the
        # payload (drop the rejected param, then fall back to no tools) and
        # retry until nothing is left to adapt.
        for _ in range(len(DROPPABLE_PARAMS) + 2):
            async with self._client.stream("POST", self._endpoint, json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    detail = self._error_message(body)
                    if resp.status_code == 400 and self._adapt_payload(payload, detail):
                        log.warning("Model '%s' rejected the request (%s) — retrying adapted",
                                    self.config.model, detail or "no detail")
                        continue
                    # Nothing left to adapt (or 401/404/500...): surface the
                    # provider's own message when there is one — "credit
                    # balance is too low" beats a bare "400 Bad Request" in
                    # the chat UI (the executor shows str(exception)).
                    self._log_error_body(body, streaming=True)
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
                                yield translated
                        else:
                            yield chunk
                return

        # Unreachable: every adaptation strictly shrinks the payload.
        raise RuntimeError(f"Model '{self.config.model}' kept rejecting the request")

    def _adapt_payload(self, payload: dict, detail: str) -> bool:
        """Make a 400-rejected payload acceptable, if we can tell how.

        Returns True when the payload was changed and the call is worth
        retrying. Named fields go first (a rejected `temperature` is not a
        reason to give up tool calling), then the no-tools fallback.
        """
        low = detail.lower()
        anthropic = self.config.provider == "anthropic"

        for key in DROPPABLE_PARAMS:
            if key not in payload or key not in low:
                continue
            if anthropic and key == "max_tokens":
                # Required by the Messages API: dropping it would only trade
                # this 400 for another. Surface the original error instead.
                continue
            # Some models want the same value under a different name.
            replacement = ("max_completion_tokens"
                           if key == "max_tokens" and "max_completion_tokens" in low
                           else None)
            value = payload.pop(key)
            if replacement:
                payload[replacement] = value
            self._param_fixes[key] = replacement
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

    async def close(self):
        await self._client.aclose()
