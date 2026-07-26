from __future__ import annotations

import re

from pydantic import BaseModel, field_validator, model_validator

# Entity ids become filenames (JsonStore) and URL path segments: restrict to a
# safe charset so a crafted id can't traverse outside the data directories.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_id(v: str) -> str:
    if not _VALID_ID.match(v or "") or ".." in v:
        raise ValueError(
            "id may only contain letters, digits, dots, hyphens and underscores"
        )
    return v


class ModelConfig(BaseModel):
    id: str
    name: str
    provider: str = "ollama"  # "ollama" | "llamacpp" | "openai" (generic OpenAI-compatible API)
    model: str = ""
    base_url: str = "http://localhost:11434"
    # Bearer token for remote OpenAI-compatible providers (OpenAI, OpenRouter,
    # Groq, Mistral, ...). Never sent back to the frontend in clear (masked by
    # the models router); empty for local providers.
    api_key: str = ""
    api_format: str = "openai"
    # Modalities the model can read natively. They gate whether an attachment is
    # sent INLINE to the model: an image goes in as image_url only if the model
    # is vision-capable, audio as input_audio only if audio-capable. When false,
    # the attachment is still available as a workspace file (path in the turn's
    # attachment manifest) so the model can read it via a tool (document_extract).
    supports_vision: bool = True   # images inlined by default (back-compat)
    supports_audio: bool = False   # audio inlined only when explicitly enabled
    # Context window in tokens. None (or 0) means "auto": the real value is
    # probed from the model server — llama.cpp /props, Ollama /api/show +
    # /api/ps, remote /v1/models when it declares one — see
    # app.engine.model_probe. An explicit value overrides the probe and, on
    # Ollama, is also what we ask the server to allocate.
    context_window: int | None = None
    options: dict = {}

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("context_window")
    @classmethod
    def validate_context_window(cls, v: int | None) -> int | None:
        # 0 / negative from a cleared form field means "auto", not "no context".
        return v if v and v > 0 else None

    @model_validator(mode="before")
    @classmethod
    def migrate_num_ctx(cls, data):
        """Back-compat: the context window used to live in options.num_ctx.

        It is now a first-class field, so lift any legacy value out of the
        free-form options blob (where it would otherwise be forwarded twice).
        """
        if not isinstance(data, dict):
            return data
        opts = data.get("options")
        if not isinstance(opts, dict) or "num_ctx" not in opts:
            return data
        data = {**data, "options": {k: v for k, v in opts.items() if k != "num_ctx"}}
        if not data.get("context_window"):
            data["context_window"] = opts["num_ctx"]
        return data


class Agent(BaseModel):
    id: str
    name: str
    description: str = ""
    model_id: str = ""
    system_prompt: str = ""
    tools: list[str] = []
    max_iterations: int = 10
    max_tool_calls: int = 5  # hard cap on total tool executions per turn
    temperature: float | None = None
    response_temperature: float | None = None
    enabled: bool = True
    callable: bool = True             # can be called/selected by others (delegation + pickers)
    callable_agents: list[str] = ["*"]  # agents this agent may delegate to via call_agent; ["*"] = all

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return _check_id(v)


class ChatMessage(BaseModel):
    # content is a plain string for normal turns, or a list of OpenAI
    # multimodal content parts (text / image_url) when files are attached.
    role: str
    content: str | list | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class Attachment(BaseModel):
    name: str
    kind: str  # "image" | "audio" | "text" (anything else is treated as a binary file)
    data: str  # image/audio: data: URI (base64) — text: extracted file content
    mime: str | None = None


class ChatRequest(BaseModel):
    agent_id: str
    message: str
    conversation: list[ChatMessage] = []
    attachments: list[Attachment] = []
    # Optional channel-scoped session key. When set (used by external
    # connectors), the turn runs against a named, persistent session addressed
    # by this id (see NamedSessionStore) instead of the web UI's singleton
    # current chat. Constrained to the same safe charset as entity ids, since
    # it becomes a filename.
    session_id: str | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str | None) -> str | None:
        return None if v is None else _check_id(v)


class ChatResponse(BaseModel):
    reply: str
    conversation: list[ChatMessage] = []
    iterations: int = 0
    tool_results: list[dict] = []
    # Full recursive execution trace: {agent_id, model_id, iterations, reply,
    # steps:[{tool, arguments, result, result_preview, ts, sub_trace?}]}.
    # sub_trace is itself a trace (a called agent's own run), so the whole
    # multi-agent flow is captured recursively.
    trace: dict | None = None


class Settings(BaseModel):
    default_model_id: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    llamacpp_base_url: str = "http://localhost:8080"
