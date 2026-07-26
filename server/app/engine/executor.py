from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from app import config
from app.models import Agent, ChatMessage, ChatResponse, ModelConfig
from app.engine.llm_provider import LLMProvider
from app.engine.toolcall_parser import parse_tool_calls_from_text
from app.tools.registry import ToolRegistry
from app.storage.store import JsonStore

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Instruction for the forced final-answer pass: some local models keep
# re-emitting the same tool call after a result instead of writing an answer,
# which would otherwise leave the turn with the bare "(used tool: ...)" marker.
_FORCE_ANSWER_PROMPT = (
    "Now write the final answer to the user's request using ONLY the information "
    "from the tool results above. Reply in the user's language as normal prose. "
    "Do NOT output JSON and do NOT call any tool."
)


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


@dataclass
class Stores:
    agents: JsonStore
    models: JsonStore


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
        self._pending_sub_events: list[dict] = []  # sub-agent tool events for SSE forwarding
        # Full traces of sub-agents invoked via call_agent this turn, in call
        # order. Consumed (popped) when building the parent's trace steps so the
        # whole flow is captured recursively.
        self._sub_traces: list[dict] = []
        # Attachments of the CURRENT user turn, kept so call_agent can forward
        # them to a sub-agent by index (see call_agent_handler). The tool-call
        # JSON only carries small indices — never the base64 blobs themselves.
        self._turn_attachments: list[dict] = []

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _build_trace(self, reply: str, iterations: int, steps: list[dict]) -> dict:
        """Assemble this agent's full execution trace."""
        return {
            "agent_id": self.agent.id,
            "model_id": self.agent.model_id,
            "iterations": iterations,
            "reply": reply,
            "steps": steps,
        }

    @staticmethod
    def _is_degenerate_reply(text: str) -> bool:
        """A final reply that is empty or just the '(used tool: ...)' marker
        means the model looped on tool calls instead of answering."""
        t = (text or "").strip()
        return (not t) or t.startswith("(used tool:")

    def _synthesis_messages(self, messages: list[dict]) -> list[dict]:
        """Messages for a forced, no-tool answer: drop empty/dangling turns and
        append the instruction to answer from the tool results above."""
        clean = [m for m in messages if (m.get("content") or m.get("tool_calls"))]
        return clean + [{"role": "user", "content": _FORCE_ANSWER_PROMPT}]

    def _synthesis_temp(self):
        return (self.agent.response_temperature
                if self.agent.response_temperature is not None else self.agent.temperature)

    def _make_step(self, tool: str, arguments, result: str) -> dict:
        """Build one rich trace step (full result + nested sub-agent trace for
        call_agent). Consumes the next queued sub-trace when tool == call_agent."""
        step = {
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "result_preview": (result or "")[:200],
            "ts": _now_iso(),
        }
        if tool == "call_agent" and self._sub_traces:
            step["sub_trace"] = self._sub_traces.pop(0)
        return step

    def _debug_log(self, msg: str):
        """Append to the debug trace file (only when MYAGENT_DEBUG is on)."""
        if not config.DEBUG:
            return
        try:
            with open(config.DEBUG_LOG_FILE, "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def _debug_reset(self, user_message: str):
        """Truncate the debug file at the start of a top-level turn."""
        if not config.DEBUG or self.depth != 0:
            return
        try:
            with open(config.DEBUG_LOG_FILE, "w") as f:
                f.write(f"=== New chat with agent '{self.agent.id}' ===\n")
                f.write(f"User: {user_message}\n\n")
        except Exception:
            pass

    @classmethod
    async def create_for_agent(
        cls,
        agent_id: str,
        tool_registry: ToolRegistry,
        stores: Stores,
        depth: int = 0,
    ) -> AgentExecutor:
        agent_data = stores.agents.get(agent_id)
        if agent_data is None:
            raise ValueError(f"Agent not found: {agent_id}")
        agent = Agent(**agent_data)

        # Resolve the model. The sentinel "default" (or an unset model_id) means
        # "use the default model configured in Settings". Read config.settings
        # live — it's reassigned when settings are updated.
        model_id = agent.model_id
        if model_id in ("", "default"):
            model_id = config.settings.default_model_id
            if not model_id:
                raise ValueError(
                    "Agent uses the default model, but no default model is "
                    "configured in Settings"
                )

        model_data = stores.models.get(model_id)
        if model_data is None:
            raise ValueError(f"Model not found: {model_id}")
        model_config = ModelConfig(**model_data)

        return cls(agent, model_config, tool_registry, stores, depth)

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
            "\n\n## Available Tools\n"
            "When you need to use a tool, respond ONLY with a JSON object (no other text):\n"
            "```json\n"
            '{"name": "TOOL_NAME", "arguments": {"PARAM_NAME": "value"}}\n'
            "```\n\n"
            "Use the tool's EXACT name and EXACT parameter names shown below "
            "(do NOT use the literal words TOOL_NAME or PARAM_NAME).\n"
            "Available tools:"
        ]
        for td in tool_defs:
            schema = td.get("parameters", {})
            params = schema.get("properties", {})
            required = set(schema.get("required", []))
            parts = []
            for pname, pinfo in params.items():
                opt = "" if pname in required else ", optional"
                parts.append(f'{pname} ({pinfo.get("type", "string")}{opt}): {pinfo.get("description", "")}')
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

    def _build_agents_directory(self) -> str:
        """Build compact directory of available agents for call_agent tool."""
        agents = self.stores.agents.list_all()
        lines = [
            "\n\n## Available Agents",
            "You can delegate tasks to these agents using the call_agent tool:",
        ]
        for a in agents:
            if not self._agent_can_call(self.agent, a):
                continue
            desc = a.get("description") or a.get("name", "")
            lines.append(f"- {a['id']}: {desc}")
        return "\n".join(lines) if len(lines) > 2 else ""

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
            stem, ext = os.path.splitext(name)
            digest = hashlib.md5(raw).hexdigest()[:8]
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)[:40] or (kind or "file")
            fname = f"{safe}-{digest}{ext}"
            dest = workdir / fname
            if not dest.exists():
                try:
                    dest.write_bytes(raw)
                except OSError as e:
                    log.warning("cannot write attachment '%s': %s", name, e)
                    out.append(att)
                    continue
            att["path"] = f"_attachments/{fname}"  # relative to the tool workspace cwd
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
    def _clean_conversation(cls, conversation: list[ChatMessage], max_messages: int = 10) -> list[dict]:
        """Remove internal tool-call artifacts from conversation history.
        Keeps only clean user/assistant exchanges so the LLM doesn't
        mimic the '[Called tool ...]' format in follow-up turns.
        Also limits to last max_messages to avoid context overflow."""
        cleaned = []
        for msg in conversation:
            content = cls._flatten_content(msg.content) or ""
            # Skip tool-scaffolding user turns (results + continue/answer nudge)
            if msg.role == "user" and (
                "Do NOT call any more tools" in content
                or content.startswith("TOOL RESULTS:")
                or "reply with ONLY the next tool-call JSON" in content
            ):
                continue
            # Skip assistant tool-call markers ("[Called tool ..." / "(used tool: ..")
            if msg.role == "assistant" and (
                content.startswith("[Called tool") or content.startswith("(used tool:")
            ):
                continue
            # Skip tool role messages
            if msg.role == "tool":
                continue
            # Skip messages with tool_calls
            if msg.tool_calls:
                continue
            data = msg.model_dump(exclude_none=True)
            data["content"] = content  # store flattened (string) content
            cleaned.append(data)
        # Sliding window: keep only the last N messages
        if len(cleaned) > max_messages:
            cleaned = cleaned[-max_messages:]
        return cleaned

    def _system_prompt_with_tools(self, tool_defs: list[dict]) -> str:
        """Return system prompt, always appending tool instructions.
        Local models may accept the tools param but still not use structured
        tool_calls — text instructions ensure they know the JSON format."""
        prompt = self.agent.system_prompt
        # Inject agent directory if agent can call other agents
        if "call_agent" in self.agent.tools:
            prompt += self._build_agents_directory()
        # Always inject tool instructions so models know the JSON format
        if tool_defs:
            prompt += self._build_tools_prompt(tool_defs)
        return prompt

    def _build_attachments_manifest(self, attachments: list[dict]) -> str:
        """List this turn's attachments with index, kind, name and workspace
        PATH, plus how to reach each: read inline (if it was sent inline), read
        the file via a tool (document_extract on its path), or forward to a
        sub-agent by index (call_agent's attachment_indices)."""
        vision = getattr(self.model_config, "supports_vision", True)
        audio_ok = getattr(self.model_config, "supports_audio", False)
        has_extract = "document_extract" in self.agent.tools
        has_call = "call_agent" in self.agent.tools

        hints = ["Items marked 'inline' are already included in the message — "
                 "read them directly."]
        if has_extract:
            hints.append("For an item marked 'file only' (audio, PDF, a binary "
                         "document, or an image you can't see), call `document_extract` "
                         "with its `path` to get the text or transcription.")
        if has_call:
            hints.append("You may forward one or more items to a sub-agent via "
                         "call_agent's `attachment_indices` (0-based), e.g. attachment_indices=[0].")

        lines = ["\n\n## Attachments (this turn)",
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

    def _prepare_turn(self, tool_defs: list[dict], attachments: list[dict] | None):
        """Return (system_content, tool_defs, openai_tools) for this turn.

        When the user attaches files, append a manifest so the model knows what
        it received, which items are inline vs available only as a workspace file
        (with its `path`, to hand to document_extract), and — for router agents —
        how to forward them to a sub-agent by index."""
        openai_tools = ToolRegistry.to_openai_format(tool_defs) if tool_defs else None
        system_content = self._system_prompt_with_tools(tool_defs)
        has_attachment = bool(attachments) and any(
            (a or {}).get("data") or (a or {}).get("path") for a in attachments
        )
        if has_attachment:
            system_content += self._build_attachments_manifest(attachments)
        return system_content, tool_defs, openai_tools

    def _parse_text_tool_calls(self, content: str) -> list[dict] | None:
        """Fallback parsing of tool calls from plain text (delegates to
        toolcall_parser with this executor's context)."""
        definitions = {d["id"]: d for d in self.tool_registry.get_all_definitions()}
        agent_ids_provider = None
        if "call_agent" in self.agent.tools:
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
    ) -> ChatResponse:
        """Non-streaming entry point: drain run_stream() and return the final
        ChatResponse (single implementation of the loop lives there)."""
        response: ChatResponse | None = None
        error: str | None = None
        async for event in self.run_stream(user_message, conversation, attachments):
            et = event.get("type")
            if et == "done":
                response = ChatResponse(**event.get("data", {}))
            elif et == "error":
                error = str(event.get("data") or "unknown error")

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
    ):
        """The agent loop, as an async generator of SSE-compatible event dicts.

        Event types: token, clear_tokens, tool_start, tool_result, error, done.
        """
        try:
            async for event in self._run_stream_inner(user_message, conversation, attachments):
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
    ):
        self._debug_reset(user_message)
        # Write attachments to workspace files (stamping a `path` on each) so the
        # model can reach any file via a tool, independently of what it can read
        # inline. Done once here; forwarded sub-agent attachments carry the path.
        attachments = self._materialize_attachments(attachments)
        # Expose this turn's attachments so call_agent can forward them by index.
        self._turn_attachments = attachments or []

        messages: list[dict] = []

        # Build OpenAI-format tool definitions (dropped if answering an attachment directly)
        tool_defs = self.tool_registry.get_definitions_for_agent(self.agent.tools)
        system_content, tool_defs, openai_tools = self._prepare_turn(tool_defs, attachments)

        # System prompt (tool descriptions injected if model doesn't support native tools)
        messages.append({"role": "system", "content": system_content})

        # Prior conversation (cleaned of tool-call artifacts)
        if conversation:
            messages.extend(self._clean_conversation(conversation))

        # New user message (with attachments folded in / images as content parts)
        messages.append({"role": "user", "content": self._build_user_content(user_message, attachments)})
        # Boundary between injected history and messages produced THIS turn. The
        # final-reply search must not reach back into a prior turn's answer, or
        # an LLM failure would silently echo the previous reply.
        turn_start = len(messages)

        iterations = 0
        total_tool_calls = 0
        max_tool_calls = self.agent.max_tool_calls  # hard limit per run
        all_tool_results: list[dict] = []
        trace_steps: list[dict] = []  # rich recursive trace (full results + sub-agents)
        executed_calls: dict[tuple, str] = {}  # (name, args) -> result
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
            if self.provider.supports_tools is False and tool_defs:
                new_prompt = self._system_prompt_with_tools(tool_defs)
                if messages[0]["content"] != new_prompt:
                    messages[0]["content"] = new_prompt

            # Debug log: dump messages sent to LLM
            self._debug_log(f"=== Agent '{self.agent.id}' iteration {iterations} ===")
            self._debug_log(f"Messages sent ({len(messages)}):")
            for i, m in enumerate(messages):
                role = m.get("role", "?")
                content = (m.get("content") or "")[:200]
                tc = m.get("tool_calls")
                self._debug_log(f"  [{i}] {role}: {content}" + (f" [tool_calls: {len(tc)}]" if tc else ""))

            # Stream LLM response token by token
            try:
                full_content = ""
                tool_calls_accum: dict[int, dict] = {}

                async for chunk in self.provider.chat_completion_stream(
                    messages=messages,
                    tools=openai_tools,
                    temperature=current_temp,
                ):
                    delta = chunk.get("choices", [{}])[0].get("delta", {})

                    if delta.get("content"):
                        full_content += delta["content"]
                        yield {"type": "token", "data": delta["content"]}

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

            except Exception as e:
                err_msg = str(e) or type(e).__name__
                log.error("LLM stream failed: %s", err_msg)
                stream_error = err_msg
                # Break out of the loop; the post-loop code decides whether to
                # salvage a partial answer (tool results this turn) or surface
                # the error. Either way the error is reported if no reply exists.
                break

            tool_calls = [tool_calls_accum[i] for i in sorted(tool_calls_accum)] if tool_calls_accum else None

            # Debug log LLM response
            self._debug_log(f"LLM response content: {(full_content or '')[:300]}")
            if tool_calls:
                self._debug_log(f"  Structured tool_calls: {json.dumps(tool_calls, ensure_ascii=False)[:500]}")

            # Fallback: parse tool calls from text content
            if not tool_calls and tool_defs and full_content:
                parsed = self._parse_text_tool_calls(full_content)
                if parsed:
                    log.info("Stream fallback: parsed %d tool call(s) from text", len(parsed))
                    self._debug_log(f"  Parsed from text: {json.dumps(parsed, ensure_ascii=False)[:500]}")
                    tool_calls = parsed
                    full_content = ""
                    # Tell frontend to clear the streamed JSON text
                    yield {"type": "clear_tokens"}

            # Deduplicate within same response + skip cross-iteration repeats
            if tool_calls:
                seen = set()
                unique = []
                for tc in tool_calls:
                    key = _tool_call_key(tc)
                    if key in seen or key in executed_calls:
                        self._debug_log(f"  DEDUP skipped: {key[0]}({key[1][:80]})")
                        continue
                    seen.add(key)
                    unique.append(tc)
                if len(unique) < len(tool_calls):
                    log.info("Filtered tool calls: %d -> %d", len(tool_calls), len(unique))
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
                    executed_calls[_tool_call_key(tc)] = result
                    summary = {"tool": func_name, "arguments": {}, "result_preview": result[:200]}
                    all_tool_results.append(summary)
                    trace_steps.append(self._make_step(func_name, {}, result))
                    yield {"type": "tool_start", "data": {"tool": func_name, "arguments": {}}}
                    yield {"type": "tool_result", "data": summary}
                    if self.provider.supports_tools is False:
                        call_result_parts.append(f"- {func_name}: {result}")
                    else:
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                    continue

                yield {"type": "tool_start", "data": {"tool": func_name, "arguments": func_args}}

                result = await self.tool_registry.execute(
                    func_name, func_args, executor=self
                )
                total_tool_calls += 1

                executed_calls[_tool_call_key(tc)] = result
                summary = {"tool": func_name, "arguments": func_args, "result_preview": result[:200]}
                all_tool_results.append(summary)
                # Rich trace step (full result + nested sub-agent trace). Built
                # here so the call_agent sub-trace queued by call_agent_handler
                # is consumed in the same order it was produced.
                trace_steps.append(self._make_step(func_name, func_args, result))
                yield {"type": "tool_result", "data": summary}
                self._debug_log(f"  Tool result: {func_name} -> {result[:200]}")

                # Forward sub-agent tool events (populated by call_agent_handler)
                while self._pending_sub_events:
                    yield self._pending_sub_events.pop(0)

                if self.provider.supports_tools is False:
                    call_result_parts.append(f"- {func_name}: {result}")
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result,
                    })

            # For models without tool support: deliver results as a labeled user
            # turn; keep the assistant turn a bare marker so the model doesn't
            # mimic a '[Called tool ...]' format instead of writing an answer.
            if self.provider.supports_tools is False and call_result_parts:
                names = ", ".join(tc["function"]["name"] for tc in tool_calls)
                messages[-1] = {"role": "assistant", "content": f"(used tool: {names})"}
                messages.append({
                    "role": "user",
                    "content": (
                        "TOOL RESULTS:\n" + "\n\n".join(call_result_parts) +
                        "\n\nIf you still need another tool, reply with ONLY the next tool-call JSON. "
                        "Otherwise write the final answer for the user, in their language, "
                        "summarizing ALL the results above. Do NOT repeat these lines or the tool names."
                    ),
                })
                use_response_temp = True
                self._debug_log("  Added 'now answer' nudge")

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
        if self._is_degenerate_reply(final_text) and all_tool_results:
            yield {"type": "clear_tokens"}
            forced = ""
            try:
                async for chunk in self.provider.chat_completion_stream(
                    messages=self._synthesis_messages(messages),
                    tools=None,
                    temperature=self._synthesis_temp(),
                ):
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        forced += delta["content"]
                        yield {"type": "token", "data": delta["content"]}
            except Exception as e:
                log.warning("Forced answer stream failed: %s", e)
            if forced.strip():
                final_text = forced.strip()
                self._debug_log("  Forced final answer synthesized")

        # No usable reply produced this turn: surface the LLM error (rather than
        # ending on an empty reply or, worse, echoing a prior turn's answer).
        if self._is_degenerate_reply(final_text) and stream_error:
            yield {"type": "error", "data": stream_error}
            final_text = f"ERROR: LLM call failed: {stream_error}"
        elif not final_text and iterations >= self.agent.max_iterations:
            final_text = "[Agent reached maximum iterations without a final response]"

        clean_messages = []
        for m in messages:
            clean_messages.append(ChatMessage(
                role=m.get("role", "assistant"),
                content=self._flatten_content(m.get("content")),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
            ))

        response = ChatResponse(
            reply=final_text,
            conversation=clean_messages,
            iterations=iterations,
            tool_results=all_tool_results,
            trace=self._build_trace(final_text, iterations, trace_steps),
        )
        yield {"type": "done", "data": response.model_dump()}
