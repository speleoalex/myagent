# Architecture

MyAgent is an AI agent platform built around **atomic agents** (model +
system prompt + tools). Stack: FastAPI backend (`server/`), vanilla
JS/Bootstrap 5 frontend (`ui/`), plain-JSON storage. A separate, optional
messaging-bridge server lives in `connectors/`.

## Repository layout

```text
server/
├── main.py            # FastAPI entry point
├── requirements.txt
├── app/
│   ├── config.py      # all paths and env vars
│   ├── models.py      # Pydantic models (Agent, ModelConfig, Settings, ...)
│   ├── engine/        # executor, LLM provider, model probe, live runs
│   ├── routers/       # /api/* endpoints
│   ├── storage/       # JSON stores, sessions
│   └── tools/         # tool registry + internal tool handlers
├── config/            # bundled seed: default agents, models, settings.json
└── tools/             # bundled seed: default tool folders
ui/                    # static SPA (index.html, js/, css/, vendor/)
connectors/            # standalone Telegram bridge server (own README)
```

## Request flow

1. The frontend sends to `POST /api/chat/stream` (SSE) or `POST /api/chat`.
2. `server/app/routers/chat.py` → `AgentExecutor`
   (`server/app/engine/executor.py`).
3. `AgentExecutor` loops: LLM call → parse tool calls → execute tools →
   repeat (bounded by per-agent `max_iterations` and `max_tool_calls`).
   `run_stream()` is the single implementation (an async generator of SSE
   events); `run()` drains it for non-streaming callers (`POST /api/chat`,
   `call_agent`).
4. `LLMProvider` (`server/app/engine/llm_provider.py`) talks to any
   OpenAI-compatible `/v1/chat/completions` endpoint. Providers: `ollama`,
   `llamacpp`, `openai` (generic remote with Bearer `api_key`; non-standard
   sampling params are not forwarded to remote providers).
5. `ToolRegistry` (`server/app/tools/registry.py`) dispatches each tool call
   to an external subprocess or an internal Python handler.

SSE events emitted: `token`, `tool_start`, `tool_result`, `clear_tokens`,
`error`, `done` (plus `stopped` from the live-run manager).

## Executor details (`server/app/engine/executor.py`)

- **Tool-call deduplication** — identical consecutive tool calls are skipped
  to prevent loops.
- **Two temperatures** — `temperature` for tool-calling decisions,
  `response_temperature` for the final answer.
- **Fallback for models without function calling** — if the backend returns
  400 with tools attached, the request is retried without tools and the tool
  schema is injected into the system prompt; the model's textual tool calls
  are then parsed by `server/app/engine/toolcall_parser.py` (JSON and
  Python-style calls extracted from plain text).
- **Agent chaining** — the internal `call_agent` tool delegates a message to
  another agent (max depth 5). Delegation is gated per agent: `callable`
  controls whether an agent may be called at all, `callable_agents` lists who
  it may call (empty list = nobody, missing = everybody).
- **Live runs** — generation is decoupled from the HTTP client
  (`server/app/engine/live.py`): you can close the tab, re-attach to the
  stream later, or stop a run. In-flight runs live in memory only; finished
  turns are persisted by the session store.

## Context window (probed, not typed)

The context window is a property of what the backend is *serving*, so MyAgent
asks for it rather than requiring configuration
(`server/app/engine/model_probe.py`):

- **llama.cpp** — `/props` (`default_generation_settings.n_ctx`, the per-slot
  window). The probed value wins; an explicit override can only lower it.
- **Ollama** — `/api/show` (trained maximum) + `/api/ps` (window of the loaded
  instance). An explicit `context_window` is also *requested* (`num_ctx` in
  the payload). Without one, MyAgent assumes what Ollama serves by default
  (4096, or `/api/ps`; override the assumption with
  `MYAGENT_OLLAMA_DEFAULT_CTX`) instead of silently multiplying VRAM use.
- **openai (remote)** — a `/v1/models` entry's `context_length` /
  `max_model_len` when the gateway declares one (OpenRouter, vLLM, LiteLLM).
  Precedence: explicit → probed → 32768 safety net. The value only bounds
  message truncation; nothing is allocated on our behalf.

Probe answers are cached per (provider, base_url, model) with a 300s TTL and
invalidated when a model config is saved or deleted.
`GET /api/models/{id}/probe` (`?refresh=1` to bypass the cache) returns the
raw probe plus the resolved window; the UI shows it in the model form and as
a `… ctx` badge in the models list.

## Tool system

Tools are runtime data: they live in `~/myagent/tools/` (override:
`MYAGENT_TOOLS`), seeded on first run from the bundled `server/tools/`.
User- and AI-created tools survive redeploys because they are outside the
install dir.

Each tool is a folder with two files:

- `tool.json` — metadata (name, description, parameters as OpenAI JSON
  Schema, timeout, max_output, enabled)
- `run` — executable script in any language (`chmod +x`); receives the
  arguments as JSON on stdin and prints its result to stdout

Internal tools (`"internal": true`) are async Python handlers registered via
`registry.register_internal()`; currently only `call_agent`.

**Hot reload** — the registry re-scans the folder with mtime caching on every
access; adding or editing a tool requires no restart.

**Native catalogs** — the bundled `server/tools/` and `server/config/agents/`
are the "native" originals. After the wholesale first-run seed,
`GET /api/tools/native` / `POST /api/tools/native/{id}/import` (and the same
under `/api/agents`) list, import or reset individual native tools/agents
against the live runtime dirs, flagging local modifications.

See [TOOLS.md](TOOLS.md) for the full contract and a worked example.

## Data storage

Everything is plain JSON under `~/myagent/` (see `server/app/config.py`):

| Path | Env override | Contents |
|---|---|---|
| `~/myagent/config/agents/` | `MYAGENT_CONFIG` | one JSON per agent |
| `~/myagent/config/models/` | `MYAGENT_CONFIG` | LLM configs; a remote `api_key` is stored 0600, masked in the API, and PUT treats mask/empty as "keep" |
| `~/myagent/config/settings.json` | `MYAGENT_CONFIG` | default model, provider base URLs |
| `~/myagent/tools/` | `MYAGENT_TOOLS` | tool folders |
| `~/myagent/library/` | `MYAGENT_LIBRARY` | offline knowledge for `local_search` (ZIM archives, notes) — user-placed, never written by the app |
| `~/myagent/workspace/` | `MYAGENT_WORKSPACE` | working dir for agents' file operations; relative paths in file/shell tools resolve here |
| `~/myagent/sessions/` | `MYAGENT_SESSIONS` | `current.json`, `history/`, `channels/` (connector chats) |
| `~/myagent/connectors/` | `MYAGENT_CONNECTORS_DIR` | connector server state: bot bindings (0600), grants |
| `~/myagent/logs/` | `MYAGENT_DEBUG_FILE` | `debug.log` when `MYAGENT_DEBUG=1` |

On first run, if `~/myagent/config` (or `~/myagent/tools`) doesn't exist,
it is seeded by copying the bundled defaults; existing data is never
overwritten.

Sessions store the full recursive execution trace (tool calls and responses
of sub-agents included), so a chat can be archived, listed and resumed with
its complete history.

## Frontend

Static SPA in `ui/`, served by FastAPI at `/` (mounted after the `/api/*`
routers). Hash routing and the `App.api(method, path, body)` helper live in
`ui/js/app.js`; chat streaming uses `EventSource` on `/api/chat/stream`.
UI strings go through `i18n('key')` with dictionaries in `ui/js/i18n/en.js`
and `it.js` (both must be kept in sync). No build step; Bootstrap is vendored
under `ui/vendor/`.

## Key files

| Task | File |
|---|---|
| Agent execution loop | `server/app/engine/executor.py` |
| Text-based tool-call parsing | `server/app/engine/toolcall_parser.py` |
| LLM communication | `server/app/engine/llm_provider.py` |
| Context window / capability probe | `server/app/engine/model_probe.py` |
| Live (client-decoupled) runs | `server/app/engine/live.py` |
| Tool discovery/execution | `server/app/tools/registry.py` |
| Internal tool handlers | `server/app/tools/internal.py` |
| Data models (Pydantic) | `server/app/models.py` |
| Paths and env vars | `server/app/config.py` |
| API endpoints | `server/app/routers/` |
| Frontend JS | `ui/js/` |
