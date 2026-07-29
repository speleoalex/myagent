from __future__ import annotations

import re

from pydantic import BaseModel, field_validator, model_validator

# Entity ids become filenames (JsonStore) and URL path segments: restrict to a
# safe charset so a crafted id can't traverse outside the data directories.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# MCP server ids are stricter: they are embedded in the tool names sent to the
# LLM, so no dots (rejected by remote providers) and short enough to leave room
# for the remote tool name inside the 64-char function-name budget.
_VALID_MCP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


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
    # Native tool calling (OpenAI `tools` + `tool_calls` in the reply).
    # None = auto: the tools are sent and the endpoint's answer decides (a 400
    # falls back to the text protocol, where tool calls are parsed out of the
    # model's text — the JSON instructions are in the system prompt either way).
    # Set False for a server that accepts `tools` but never emits a tool_call.
    supports_tools: bool | None = None
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


class McpServer(BaseModel):
    """An external MCP (Model Context Protocol) server providing tools.

    The id becomes part of the tool name exposed to the LLM
    (``mcp_<id>_<tool>``), which must match ``^[a-zA-Z0-9_-]{1,64}$`` — remote
    OpenAI-compatible providers reject dots — hence the stricter id charset and
    the length cap (see app.mcp.naming for the name budget).
    """

    id: str
    name: str = ""
    description: str = ""
    transport: str = "stdio"  # "stdio" (local subprocess) | "http" (Streamable HTTP)
    enabled: bool = True
    # stdio transport
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}  # merged over os.environ; may hold secrets
    cwd: str = ""
    # http transport
    url: str = ""
    headers: dict[str, str] = {}  # may hold secrets
    bearer: str = ""              # convenience, folded into Authorization
    # limits
    connect_timeout: int = 20  # initialize + tools/list budget inside a chat turn
    timeout: int = 60          # per tools/call
    max_output: int = 10000    # same semantics as tool.json max_output
    max_tools: int = 32        # guard against flooding a small model's context
    tools_ttl: int = 300       # discovery cache TTL (seconds)
    # gating: which of the server's tools are exposed (empty allow = all)
    allow_tools: list[str] = []
    deny_tools: list[str] = []

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not _VALID_MCP_ID.match(v or ""):
            raise ValueError(
                "MCP server id must be 1-24 chars, lowercase letters, digits, "
                "hyphens or underscores, and start with a letter or digit"
            )
        return v

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        if v not in ("stdio", "http"):
            raise ValueError("transport must be 'stdio' or 'http'")
        return v

    @model_validator(mode="after")
    def check_transport_fields(self):
        if self.transport == "stdio":
            if not (self.command or "").strip():
                raise ValueError("stdio transport requires a command")
        elif not (self.url or "").startswith(("http://", "https://")):
            raise ValueError("http transport requires an http(s):// url")
        return self


class AutonomousConfig(BaseModel):
    """Optional tuning for a live (autonomous) agent. All fields have sane
    defaults, so ``live: true`` alone is a working configuration — this block
    only customizes it. There is deliberately NO ``enabled`` here:
    ``Agent.live`` is the single switch."""

    interval_s: int = 1800          # heartbeat period; 0 = wake on events only
    instructions: str = ""          # standing instructions injected at each wake
    max_wakes_per_hour: int = 12
    max_consecutive_errors: int = 5
    wake_timeout_s: int = 600
    notify_binding_id: str = ""     # default target for the notify_user tool
    notify_chat_id: str = ""
    # How many messages of the PREVIOUS wakes a wake may see. Default 0: none.
    #
    # Nothing like the interactive window (200 with memory on). A heartbeat is a
    # self-similar task, so its own history is dozens of near-identical copies of
    # the same wake prompt and the same reply — not continuity but a feedback
    # loop: the model reads its last output and reproduces it, which is how an
    # already-fixed misconfiguration keeps being reported as broken (observed,
    # for three wakes running, after the fix landed). Continuity that actually
    # matters belongs to deep memory, which is injected as a digest, and an agent
    # that needs more can be told to call memory_search in its instructions.
    #
    # The full conversation is still written to the session file either way: this
    # governs only what goes back INTO the prompt. Raise it if you have a reason.
    history_messages: int = 0


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
    # Per-agent deep memory (opt-in). False = hard exclusion: no compaction, no
    # prompt injection, and the memory_* tools refuse even if attached.
    memory_enabled: bool = False
    # Compaction threshold in estimated tokens of the CLEANED conversation:
    # above it, the oldest turns are archived to deep memory and summarized.
    memory_threshold: int = 4000
    # THE autonomy switch (default off). True = the AutonomyService wakes this
    # agent on a heartbeat and on queued events. Persisted with the agent, so a
    # started agent restarts on its own after a service/machine reboot;
    # live=false (or enabled=false) is the kill switch, effective within one
    # scheduler scan.
    live: bool = False
    # Optional autonomy knobs; None = all defaults (live alone is enough).
    autonomous: AutonomousConfig | None = None

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
    # Optional provenance of a channel-scoped turn — the connector type (e.g.
    # "telegram"). Stored on the session so the history UI can show where the
    # chat came from. Ignored for regular web chats.
    source: str | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str | None) -> str | None:
        # Strict: an empty string is a malformed channel key and must fail
        # loudly (422), not silently fall through to the web current chat.
        return None if v is None else _check_id(v)

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str | None) -> str | None:
        return None if not v else _check_id(v)


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
    # Where the notify_user tool delivers messages: the standalone connectors
    # server (POST /api/bindings/{id}/send) and its optional bearer key
    # (MYAGENT_CONNECTORS_API_KEY on the connectors side).
    connectors_base_url: str = "http://localhost:8899"
    connectors_api_key: str = ""
