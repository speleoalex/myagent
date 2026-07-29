# Architecture

MyAgent is an AI agent platform built around **atomic agents** (model +
system prompt + tools). Stack: FastAPI backend (`server/`), vanilla
JS/Bootstrap 5 frontend (`ui/`), plain-JSON storage. A separate, optional
messaging-bridge server lives in `connectors/`.

It is designed to run **without internet**: a local model serves the
inference, the `local_search` tool answers from the offline library
(`~/myagent/library`), and `http_request` reaches IoT devices on the LAN.
The web tools are an optional online extra.

## Repository layout

```text
server/
├── main.py            # FastAPI entry point
├── requirements.txt
├── app/
│   ├── config.py      # all paths and env vars
│   ├── models.py      # Pydantic models (Agent, ModelConfig, Settings, ...)
│   ├── engine/        # executor, LLM provider, model probe, live runs
│   ├── mcp/           # MCP client, connection manager, naming, result mapping
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
  `response_temperature` for the final answer. The switch happens only when no
  further tool call is possible (tool budget spent, or last iteration): after a
  tool result the model is still *deciding*, and that decision belongs to the
  tool-calling temperature.
- **Native tool calling is the default** — every provider is asked (the `tools`
  array is sent and the endpoint's answer decides); a 400 downgrades to the
  text protocol below, and the rejection is remembered process-wide so later
  runs start in text mode directly. `ModelConfig.supports_tools` overrides
  the probe (`null` = auto, `false` = text protocol, `true` = skip the probe);
  the model form exposes it as *Tool calling: Auto / Native / Text*.
- **Text protocol (fallback)** — ONLY when the endpoint has no function calling
  is the tool schema injected into the system prompt (in native mode the
  `tools` payload is the documentation — injecting both made models emit JSON
  as prose), and the model's textual calls are parsed by
  `server/app/engine/toolcall_parser.py` (JSON and Python-style calls extracted
  from plain text). Results come back as a `TOOL RESULTS:` user
  turn, and the assistant turn replays the call the model just made — marker
  line plus its canonical JSON. That replay is load-bearing: with a bare
  `(used tool: X)` marker, models copied the marker as their next reply, which
  carries no call and silently ended a multi-step chain. A reply that clearly
  meant to be a call but does not parse (one stray quote is enough) is not
  accepted as the final answer either: the model is shown the problem and asked
  to resend it, at most twice per turn. The reverse also holds: an answer that
  merely *mentions* a tool is never mistaken for a call — in native mode the
  text parser only engages on an explicit JSON call shape, and a Python-style
  `tool("...")` whose parentheses yield no key=value arguments is treated as
  prose, not executed with empty arguments. For the same reason agent system
  prompts should not re-document their tools with hand-written JSON examples:
  the platform documents tools in whichever protocol is active.
- **Agent chaining** — the internal `call_agent` tool delegates a message to
  another agent (max depth 5). Delegation is gated per agent: `callable`
  controls whether an agent may be called at all, `callable_agents` lists who
  it may call (empty list = nobody, missing = everybody).
  An agent holding `call_agent` is told automatically who it can reach and what
  each one can do: the system prompt gets an **Available Agents** directory —
  one line per reachable agent, its description and its installed tool ids,
  deliberately minimal because it is injected on every turn — and
  `call_agent`'s `agent_id` parameter is narrowed to an `enum` of those ids.
  Both are derived from the same `_agent_can_call()` gate that enforces the
  call, so the advertised list and the permitted list cannot drift. Between
  agents only the essentials travel: the caller's `message` in, the sub-agent's
  `reply` out — a sub-agent never receives the parent's conversation history.
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

Tools are an **overlay of two layers**, with no install step:

- the bundled catalog `server/tools/` (`config.DEFAULT_TOOLS_DIR`) is the
  read-only *native* layer — always visible, so a freshly deployed app already
  has every native tool, and an upgrade updates them in place;
- `~/myagent/tools/` (override: `MYAGENT_TOOLS`) is the *user* layer: tools the
  user or the AI created, plus copy-on-write copies of edited native tools. It
  is outside the install dir, so it survives redeploys.

The user layer wins each id collision. Editing a native tool calls
`registry.ensure_override()`, which copies the folder (group layout and
vendored deps included) into the user layer before writing; the bundled
original is never modified. **Reset** (`POST /api/tools/{id}/reset`) deletes
that copy so the bundled version shows through again — there is no import or
re-download, and native tools cannot be deleted (`DELETE` only removes
user-created ones). Nothing is ever seeded: `config.ensure_tools_dir()` just
creates the empty user dir.

`GET /api/tools` annotates every folder tool with `origin` (`native` |
`custom`), `has_override` (a user copy shadows a bundled tool) and `modified`
(that copy actually differs — a byte-identical leftover copy is not a
modification). For native tools the *bundled* layout owns the `category`, so a
legacy flat copy of a grouped tool keeps its group and stays covered by a
`<group>/*` grant. `language` is detected from the `run` shebang (seeing
through a shell launcher to the script it execs) and is `Python` for internal
tools; like `category` it is derived, never read from tool.json.

The UI organizes the tools list by what can be done with each one — your own
tools (edit, delete), built-in ones (edit → local copy, reset) and system
(internal) tools, which are reference material in a collapsed section.

Each tool is a folder with two files:

- `tool.json` — metadata (name, description, parameters as OpenAI JSON
  Schema, timeout, max_output, enabled)
- `run` — executable script in any language (`chmod +x`); receives the
  arguments as JSON on stdin and prints its result to stdout

**Groups (categories)** — a subfolder of the tools dir *without* its own
`tool.json` is a group: its subfolders are scanned as ordinary tools, one
level deep (e.g. the bundled `file_management/` holds `file_read`,
`file_write`, `file_append`, `make_dir`). The group name becomes the tools'
`category`; ids stay global (the leaf folder name), so grouping a tool
changes nothing for the agents that reference it. An agent's `tools` list can
grant a whole group with the `<group>/*` wildcard (e.g. `file_management/*`)
— the folder analogue of `mcp:<server>/*` — expanded per turn by the
registry, so nothing downstream ever sees a wildcard. The UI shows a group
as one block: a master checkbox for the wildcard, or per-tool checkboxes.

Internal tools (`"internal": true`) are async Python handlers registered via
`registry.register_internal()`: `call_agent`, the memory tools
(`memory_search`/`memory_read`/`memory_note`), `notify_user`, `schedule_task`
and `autonomy_control` (an agent switches its OWN autonomous mode on/off from
chat — "start yourself" / "stop yourself" / "are you active?" — by toggling
the persisted `Agent.live` switch and cancelling any wake in flight).

**Hot reload** — the registry re-scans the folder with mtime caching on every
access; adding or editing a tool requires no restart.

**Native agent catalog** — agents follow a different (older) model: the bundled
`server/config/agents/` seeds `~/myagent/config/agents/` on first run, and
`GET /api/agents/native` / `POST /api/agents/native/{id}/import` list, import
or reset individual native agents, flagging local modifications. Tools need no
such endpoints — the overlay above makes every native tool present already.
The Agents page hides that difference: the catalog is merged into the single
card grid, where an agent you deleted stays in place as a dimmed card with an
Import button, and one you edited carries a *modified* badge and a reset
button — the same two states the Tools page draws.

See [TOOLS.md](TOOLS.md) for the full contract and a worked example.

## MCP servers (second tool source)

External [MCP](https://modelcontextprotocol.io) servers are a second source of
tools alongside the folders. One JSON file per server under
`~/myagent/config/mcp/`; the UI lives in the Tools area at `#/tools/mcp`.

- **Transports** — `stdio` (the server is launched as a child process, spoken to
  in newline-delimited JSON-RPC) and Streamable HTTP (`url`, optional bearer /
  extra headers). Protocol surface: `initialize`, `tools/list` (paginated),
  `tools/call`, `ping`, plus `notifications/cancelled` on timeout and
  `tools/list_changed` to invalidate the cache (which on the HTTP transport only
  arrives if a server interleaves it into a reply — there is no background listen
  stream, so the TTL and the Refresh button are the real mechanism). No OAuth, no
  legacy HTTP+SSE transport, no resources/prompts/sampling.
- **No new dependencies** — the client is `server/app/mcp/client.py` (asyncio
  subprocesses + the httpx already in use). It is the only module that knows the
  wire format, behind five methods (`connect`, `list_tools`, `call_tool`, `ping`,
  `aclose`), so it can be swapped for the official SDK without touching anything
  else.
- **Tool names** — a remote tool becomes `mcp_<server_id>_<name>`, sanitized to
  `^[a-zA-Z0-9_-]{1,64}$` (remote OpenAI-compatible gateways reject dots) with a
  short digest appended when the name had to be rewritten or truncated. The
  *tool* half is never recovered by parsing — that mapping lives in the tool
  metadata, since a server id may itself contain underscores; only the server
  segment is read back off the fixed prefix, to route a connect. An agent can also
  hold `mcp:<server_id>/*`, meaning "every tool this server exposes", so tools
  added server-side are picked up automatically.
- **Lazy and isolated** — `ToolRegistry.ensure_mcp()` (called once per turn by
  the executor) connects only the servers the current agent references. The wait
  is capped by `connect_timeout` while the connect continues in the background
  (`asyncio.shield`), so a cold `npx -y` costs one degraded turn instead of
  blocking it. A server that is down contributes no tools and never breaks a
  turn; failures reach the model in-band as `ERROR: ...`, like any tool error.
  A failed refresh keeps the previously discovered list.
- **Definitions** — discovery results are cached in memory with a TTL and
  mirrored to `~/myagent/config/mcp/cache/`, so the agent tool picker keeps
  showing a server's tools (marked unavailable) while it is offline instead of
  silently dropping them from every agent that uses them. Input schemas are
  flattened to a plain object schema (`$ref`/`anyOf`/union types resolved) because
  llama.cpp grammars and strict gateways reject the full JSON Schema surface, and
  descriptions are truncated: they are third-party text that lands in the prompt.
- **Results** — content blocks are flattened to text; images, audio and blob
  resources are written into `~/myagent/workspace/_attachments/` and referenced by
  a note, never inlined as base64 (that would land in both the context window and
  the session file).
- **Teardown** — stdio servers are child processes, so `server/main.py` has a
  FastAPI `lifespan` whose shutdown closes every connection (bounded by
  `MYAGENT_MCP_SHUTDOWN_TIMEOUT`, default 10s).

Endpoints: `GET/POST /api/mcp`, `GET/PUT/DELETE /api/mcp/{id}`,
`POST /api/mcp/{id}/refresh`, `POST /api/mcp/test` (probes an unsaved draft and
returns `{"ok": false, "error": ...}` with HTTP 200 on failure),
`POST /api/mcp/import` (a Claude Desktop / VS Code `mcpServers` blob),
`GET /api/mcp/tools`, `GET /api/mcp/status`. Secrets (`bearer`, `env` and
`headers` values) are write-only: masked on GET, and a PUT that echoes the mask
keeps the stored value. Note that configuring a stdio server means running a
local command — the same capability `POST /api/tools` already grants by writing
an executable `run` script.

## Deep memory (per-agent)

Opt-in long-term memory, off by default: an agent with `memory_enabled: true`
remembers across chats. Two moving parts:

- **Store** (`server/app/storage/memory.py`) — one tree per agent under
  `~/myagent/memory/<agent_id>/`: `tree.json` holds a root digest plus every
  summary node (`s-NNNNNN`); `chunks/c-NNNNNN.json` are the archived raw
  conversation slices (and notes), read only on drill-down. Storage only, no
  LLM calls; per-agent `asyncio.Lock`; atomic writes.
- **Compactor** (`server/app/engine/memory_compactor.py`) — fires in the
  background after a persisted turn. When the session's cleaned conversation
  exceeds `agent.memory_threshold` (estimated tokens, default 4000), the oldest
  turns are archived as a chunk, summarized with the agent's own model, and
  spliced out of `conversation[]`. The summary goes into
  `session["memory"] = {archived_user_turns, context[]}` — never into
  `conversation[]` (that would break the scaffolding predicate and the rewind
  endpoint) — and is injected at prompt build as a `## Memory` section, along
  with the tree's root digest. Every 8 parentless nodes fold into a
  higher-level summary (cascading), and the root digest is refreshed
  best-effort — both after a compaction and (fire-and-forget) after a
  `memory_note`, so a fresh note reaches new sessions without waiting for the
  next compaction. A `memory_search` with no keyword match still lists the
  most recent top-level entries (vague queries like "what do you remember?"
  would otherwise read as an empty memory).

Crash safety: chunk file → tree.json → session splice, in that order, with a
content-hash dedup making the retry idempotent — nothing is ever removed from
a conversation before it is durable in deep memory. A failed or garbage
summary aborts the whole pass (behavior degrades to exactly today's).

Access: memory is strictly per-agent (`call_agent` transfers none), and
`memory_enabled: false` (the default) is a hard exclusion — the three internal
tools `memory_search` / `memory_read` / `memory_note` refuse even when
attached. With the flag on but no tools attached the agent gets "passive"
memory (it remembers but can't explore or annotate).

## Autonomous (live) agents

Any agent can be made autonomous with the ``live: true`` flag — THE single
switch, off by default, persisted with the agent, so a started agent restarts
by itself after a service or machine reboot. The optional ``autonomous`` block
only customizes the defaults (30-min heartbeat, standing instructions, rate
limits, notify target); ``live`` alone is a working configuration.

- **Scheduler** (`server/app/engine/autonomy.py`, started in the FastAPI
  lifespan) — one supervisor loop (5s resolution) re-reads the agent store on
  every scan (toggling ``live`` from the UI takes effect within seconds, no
  restart; ``live: false`` is the kill switch) and spawns one wake task per due
  agent. Scheduling is fixed-delay-from-completion + jitter (no overlapping
  ticks, no backlog); per-agent state (last/next wake, error streak, pause,
  wake history) persists in ``~/myagent/autonomy/<id>/state.json`` — no wake
  storm on restart.
- **Events** (`server/app/storage/events.py`) — a persistent queue, one JSON
  file per event in ``autonomy/<id>/events/{pending,archive}/``. Producers: the
  agent itself (``schedule_task`` tool: reminders and recurring tasks) and
  ``POST /api/agents/{id}/events`` (webhooks, scripts). Delivery is
  once-on-success with a recorded outcome: after a clean wake each event is
  archived with ``reacted: true`` and a ``reaction`` (``null`` = "seen, chose
  not to act" — legitimate); on failure it stays pending and is re-delivered.
  ``archive/`` is the audit trail.
- **Wake turn** — a normal executor turn in the dedicated named session
  ``autonomous_<agent_id>`` (source ``autonomous``, visible in the UI session
  list), driven through the LiveRunManager and serialized on the same lock as
  connector chats. The wake prompt lists due events and standing instructions;
  a reply of exactly ``NOOP`` with zero tool calls skips persistence entirely
  (heartbeats must not grow the session), while any tool call is always
  recorded. Agents with ``memory_enabled`` get their root digest injected and
  the session compacts like any other; agents without memory have the saved
  conversation capped.
- **Reaching the user** — the ``notify_user`` tool POSTs to the connectors
  server (`POST /api/bindings/{id}/send`, bearer-gated by
  ``MYAGENT_CONNECTORS_API_KEY``, best-effort) using the agent's configured
  default binding/chat; ``Settings.connectors_base_url`` / ``connectors_api_key``
  point at it. The reply text of a wake is only logged.
- **Safety rails** — per-hour rate limit, auto-pause after N consecutive
  errors (cleared by re-saving the agent or ``POST /api/autonomy/{id}/resume``),
  ``wake_timeout_s`` wall, plus the existing per-turn ``max_iterations`` /
  ``max_tool_calls``.

Endpoints: `GET /api/autonomy/status`, `POST /api/autonomy/{id}/wake` (manual
trigger, also the main testing lever), `POST /api/autonomy/{id}/stop`,
`POST /api/autonomy/{id}/resume`; `POST/GET /api/agents/{id}/events`.
`DELETE /api/agents/{id}` removes the agent's autonomy state (deep memory is
deliberately kept).

`notify_user` both sends and appends the sent text to the target chat's own
conversation, so an unsolicited message is part of the history the agent replays
next turn (otherwise "repeat that" repeats the turn before it). The session key is
returned by the connectors `/send` ack — it derives from the binding's
`session_prefix` and is not derivable by myagent. A wake receives no chat history
at all by default (`AutonomousConfig.history_messages: 0`); continuity comes from
deep memory instead.

`GET /api/connectors/bindings` is a read-only proxy to the connectors server's
own binding list, so the agent form can offer a real picker for
`autonomous.notify_binding_id` instead of a free-text id. It exists because that
server listens on loopback and sets no CORS headers, so the browser cannot query
it directly; only picker-relevant fields are forwarded (never the bot token), and
an unreachable connectors server returns `{bindings: [], available: false}` rather
than an error — the form then falls back to a text input.

## Data storage

Everything is plain JSON under `~/myagent/` (see `server/app/config.py`):

| Path | Env override | Contents |
|---|---|---|
| `~/myagent/config/agents/` | `MYAGENT_CONFIG` | one JSON per agent |
| `~/myagent/config/models/` | `MYAGENT_CONFIG` | LLM configs; a remote `api_key` is stored 0600, masked in the API, and PUT treats mask/empty as "keep" |
| `~/myagent/config/settings.json` | `MYAGENT_CONFIG` | default model, provider base URLs |
| `~/myagent/config/mcp/` | `MYAGENT_CONFIG` | one JSON per MCP server (0600: `env`/`headers` may hold secrets) + `cache/` with the discovered tool catalogue |
| `~/myagent/tools/` | `MYAGENT_TOOLS` | tool folders |
| `~/myagent/library/` | `MYAGENT_LIBRARY` | offline knowledge for `local_search` (ZIM archives, notes) — user-placed, never written by the app |
| `~/myagent/workspace/` | `MYAGENT_WORKSPACE` | working dir for agents' file operations; relative paths in file/shell tools resolve here |
| `~/myagent/sessions/` | `MYAGENT_SESSIONS` | `current.json`, `history/`, `channels/` (connector chats) |
| `~/myagent/memory/` | `MYAGENT_MEMORY` | per-agent deep memory: `<agent_id>/tree.json` + `chunks/` |
| `~/myagent/autonomy/` | `MYAGENT_AUTONOMY` | live agents' runtime state: `<agent_id>/state.json` + `events/{pending,archive}/` |
| `~/myagent/connectors/` | `MYAGENT_CONNECTORS_DIR` | connector server state: bot bindings (0600), grants, contacts (address book) |
| `~/myagent/logs/` | `MYAGENT_DEBUG_FILE` | `debug.log` when `MYAGENT_DEBUG=1` |

On first run, if `~/myagent/config` (or `~/myagent/tools`) doesn't exist,
it is seeded by copying the bundled defaults; existing data is never
overwritten. On later runs, bundled tools missing from `~/myagent/tools` are
seeded individually (so upgrades deliver newly shipped tools), while existing
tool folders are never touched.

Sessions store the full recursive execution trace (tool calls and responses
of sub-agents included), so a chat can be archived, listed and resumed with
its complete history.

Connector chats (`sessions/channels/<key>.json`, one per external chat, keyed
by e.g. `telegram_bot_12345`) use the **same session format** as web chats —
the shared factory lives in `server/app/storage/sessions.py` — plus two
provenance fields: `channel` (the external chat key) and `source` (the
connector type, sent by the connector in `ChatRequest.source`). They stay out
of the history listing while active; a `/reset` archives the conversation into
the regular `history/` (fresh id, provenance preserved), where the web UI
shows it with a source badge. Because a channel file is never closed by "new
chat", it is size-rotated: past `MYAGENT_CHANNEL_ROTATE_BYTES` (default 2 MiB)
the log is archived into `history/` and the file restarts with the same
compact LLM conversation, so the bot keeps its context.

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
| Deep-memory store (tree + chunks) | `server/app/storage/memory.py` |
| Memory compaction pipeline | `server/app/engine/memory_compactor.py` |
| Autonomy scheduler + wake turns | `server/app/engine/autonomy.py` |
| Per-agent event queues | `server/app/storage/events.py` |
| Tool discovery/execution | `server/app/tools/registry.py` |
| Internal tool handlers | `server/app/tools/internal.py`, `server/app/tools/memory_tools.py` |
| MCP wire protocol (stdio + HTTP) | `server/app/mcp/client.py` |
| MCP connections, discovery, policy | `server/app/mcp/manager.py` |
| Data models (Pydantic) | `server/app/models.py` |
| Paths and env vars | `server/app/config.py` |
| API endpoints | `server/app/routers/` |
| Frontend JS | `ui/js/` |
