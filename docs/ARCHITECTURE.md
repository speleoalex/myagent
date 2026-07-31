# Architecture

MyAgent is an AI agent platform built around **atomic agents** (model +
system prompt + tools). Stack: FastAPI backend (`server/`), vanilla
JS/Bootstrap 5 frontend (`ui/`), plain-JSON storage. An optional messaging
plugin lives in `connectors/`, installed separately (see `docs/PLUGINS.md`).

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
connectors/            # optional Telegram plugin: source only, NOT deployed
                       # with the core (installed to ~/myagent/plugins/)
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
(`memory_search`/`memory_read`/`memory_note`), `notify_user`, `manage_tasks`
(the agent lists, schedules, edits and cancels its OWN tasks)
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
`GET /api/mcp/status`. The tool catalogue reaches the UI through the unified
`GET /api/tools` (each MCP entry marked `source: "mcp"`). Secrets (`bearer`, `env` and
`headers` values) are write-only: masked on GET, and a PUT that echoes the mask
keeps the stored value. Note that configuring a stdio server means running a
local command — the same capability `POST /api/tools` already grants by writing
an executable `run` script.

## Long-term memory (per-agent)

Opt-in, off by default: an agent with `memory_enabled: true` remembers across
chats. The store is plain Markdown, readable and hand-editable:

- **Store** (`server/app/storage/memory.py`) — one directory per agent under
  `~/myagent/memory/<agent_id>/`: a `memory.md` injected whole into the prompt
  (as the `## Memory` section) plus flat `chunks/<YYMMDDHHMMSS>.md` files, one
  per archived item. `memory.md` has three parts split by two load-bearing
  markers: everything before `## Notes` is user prose (a durable profile,
  edited by hand, preserved byte-for-byte); `## Notes` indexes explicit
  `memory_note` facts (cap 20); `## Recent` indexes automatic conversation
  summaries (cap 10, newest on top). Explicit "remember this" facts outrank
  automatic summaries: their own section, a higher cap, and first place in
  `memory_search` results. Evicted index lines disappear from `memory.md`
  only — the chunk files stay on disk and remain searchable. A chunk is
  frontmatter + body, where the body is ONLY the summary (or the full note
  text): the complete transcripts already live in the sessions store, the
  single ground truth — memory never duplicates them. Storage only, no LLM
  calls; per-agent `asyncio.Lock` held for file I/O only; atomic writes.
- **Compactor** (`server/app/engine/memory_compactor.py`) — fires in the
  background after a persisted turn. When the session's cleaned conversation
  exceeds `agent.memory_threshold` (estimated tokens, default 4000), the oldest
  turns are summarized with the agent's own model (no lock held — an LLM call
  takes 10-60s locally), written as a chunk, indexed in `memory.md`, and
  spliced out of `conversation[]`. The summary also goes into
  `session["memory"] = {archived_user_turns, context[]}` — never into
  `conversation[]` (that would break the scaffolding predicate and the rewind
  endpoint). A `memory_note` lands straight in the `## Notes` index with no
  LLM involved, so it reaches new sessions immediately. A `memory_search`
  with no keyword match still lists the most recent entries (vague queries
  like "what do you remember?" would otherwise read as an empty memory);
  `memory_read` on a conversation chunk names the session holding the full
  transcript.

Crash safety: chunk file → `memory.md` index → session splice, in that order,
with a content-hash dedup making the retry idempotent (`add_to_index` is a
no-op on an already-listed id) — nothing is ever removed from a conversation
before it is durable in memory, and a chunk missing from the index is still
found by `memory_search`, which scans the files, not the index. A failed or
garbage summary aborts the whole pass (behavior degrades to exactly today's).

Access: memory is strictly per-agent (`call_agent` transfers none), and
`memory_enabled: false` (the default) is a hard exclusion — the three internal
tools `memory_search` / `memory_read` / `memory_note` refuse even when
attached. With the flag on but no tools attached the agent gets "passive"
memory (it remembers but can't explore or annotate).

## Autonomous (live) agents

Any agent can be made autonomous with the ``live: true`` flag — THE single
switch, off by default, persisted with the agent, so a started agent restarts
by itself after a service or machine reboot. The optional ``autonomous`` block
only customizes the safety rails (rate limit, error tolerance, wake timeout,
notify target); ``live`` alone is a working configuration.

WHEN an agent acts, and to do what, is not a setting on the agent: it is its
**task list**. There is no separate heartbeat — a routine is a task with a cron
expression — so one list answers the whole question, and an agent with no task
stays idle whether it is live or not.

- **Tasks** (`server/app/models.py` ``Task``, `server/app/storage/tasks.py`) —
  one JSON file per task in ``~/myagent/config/tasks/<id>.json``: an
  ``agent_id``, a ``prompt`` (what to do, phrased as an order) and a schedule,
  either a 5-field ``cron`` expression or a one-off ``at`` timestamp (neither =
  due immediately, which is how an external poke queues work). Because a task
  is an addressable entity, it can be listed, edited, disabled and cancelled —
  from the UI, the API, or the agent's own ``manage_tasks`` tool.
  ``next_at`` is the ONLY field the scheduler reads; ``TaskStore`` is the only
  thing that computes it, on save and after each run.
- **Cron** (`server/app/engine/cron.py`) — a ~35-line stdlib parser: ``*``,
  ``n``, ``a-b``, ``a,b,c`` and ``/step`` on any of them; ``next_after``
  iterates by day (not by minute), so cost is bounded by skipped days. Two
  declared deviations from POSIX: day-of-month and day-of-week are ANDed, not
  ORed, and there are no names or ``@daily`` macros. No dependency was added.
- **Scheduler** (`server/app/engine/autonomy.py`, started in the FastAPI
  lifespan) — one supervisor loop (5s resolution) re-reads the agent store on
  every scan (toggling ``live`` from the UI takes effect within seconds, no
  restart; ``live: false`` is the kill switch) and spawns one wake per agent
  with work due. Tasks due at the same moment share ONE wake, so a slow
  local-model turn can never build a backlog. Rescheduling happens only after a
  successful wake (``TaskStore.advance``): run-once-on-success, with a failed
  run left due and retried. Per-agent runtime state (last wake, error streak,
  pause, wake history) persists in ``~/myagent/autonomy/<id>/state.json`` — no
  wake storm on restart.
- **Wake turn** — a normal executor turn in the dedicated named session
  ``autonomous_<agent_id>`` (source ``autonomous``, visible in the UI session
  list), driven through the LiveRunManager and serialized on the same lock as
  connector chats. The wake prompt lists the due tasks as orders, plus one line
  naming what comes next so the agent can answer "what's scheduled?" without a
  tool call. A reply of exactly ``NOOP`` with zero tool calls skips persistence
  entirely (a 20-minute routine must not grow the session), while any tool call
  is always recorded. Agents with ``memory_enabled`` get their memory.md
  injected and the session compacts like any other; agents without memory have
  the saved conversation capped.
- **Reaching the user** — the ``notify_user`` tool hands the text to a running
  connector, found through ``app.state.connectors`` (the connectors plugin).
  Recipients are addressed BY NAME from the address book (``to``), and the agent's
  configured binding/chat is the fallback for when no name is given — in that
  order, so a named recipient is never overridden by the default. Best-effort, and
  a clear error when the plugin is not installed. The reply text of a wake is only
  logged.
- **Scheduling for OTHER agents** (opt-in, ``Agent.schedule_others``, default
  off) — with the flag, the executor injects an optional ``agent_id`` into
  ``manage_tasks`` and ``autonomy_control``, pinned by an ``enum`` to the agents
  this one may act on (``_with_scheduling_targets`` /
  ``_agent_can_schedule``: enabled, ``callable``, not itself). Both tools,
  because a task on an agent whose ``live`` is off never runs, so scheduling
  without starting is half a capability. The parameter is injected rather than
  declared in ``tool.json``: without the grant the schema must not even mention
  it — a documented parameter reads as an invitation — and the handlers refuse
  it too, against the same list the enum offered. Omitted (or a placeholder like
  "self") always means the caller itself, which is the whole behaviour when the
  flag is off. Every message that names a target says so ("Task t-3 created for
  agent 'sysadmin'"): the reader is a model that relays the line to the user,
  and "task created" is otherwise heard as its own.
- **Safety rails** — per-hour rate limit (also the floor on how often a task
  can really run), auto-pause after N consecutive errors (cleared by re-saving
  the agent or ``POST /api/autonomy/{id}/resume``), ``wake_timeout_s`` wall,
  plus the existing per-turn ``max_iterations`` / ``max_tool_calls``. A failed
  wake records the outcome on its due tasks WITHOUT advancing ``next_at``: they
  stay due and are retried, and the Tasks page shows the error instead of the
  last success.

Endpoints: `GET /api/autonomy/status`, `POST /api/autonomy/{id}/wake` (manual
trigger, also the main testing lever), `POST /api/autonomy/{id}/stop`,
`POST /api/autonomy/{id}/resume`. Tasks are a resource of their own:
`GET|POST /api/tasks`, `GET|PUT|DELETE /api/tasks/{id}`,
`POST /api/tasks/{id}/run` (make it due now) and `GET /api/tasks/preview?cron=`
(the next occurrences, so the form never has to reimplement cron in JS).
`DELETE /api/agents/{id}` removes the agent's autonomy state and its tasks
(long-term memory is deliberately kept).

`notify_user` both sends and appends the sent text to the target chat's own
conversation, so an unsolicited message is part of the history the agent replays
next turn (otherwise "repeat that" repeats the turn before it). The session key is
asked of the connector (`session_id_for`): it derives from the binding's
`session_prefix`, so nothing else can compute it. A wake receives no chat history
at all by default (`AutonomousConfig.history_messages: 0`); continuity comes from
long-term memory instead.

Inbound provenance is the reverse path: the transport hands
`process_message` the sender's id / @username / display name, `CoreClient.chat`
resolves them against the address book (`Connectors.sender_display`, the mirror
of `resolve_recipients`) and the turn prefixes the MODEL-visible message with
`[Message from Alessandro via Telegram]` (`ChatRequest.sender`,
`run_channel_turn`). Per message, not per session — in a group the sender
changes at every turn — and only on the model's copy: the stored display
message stays exactly what the person typed.

`GET /api/connectors/bindings` is served by the plugin and lets the agent form
offer a real picker for `autonomous.notify_binding_id` instead of a free-text id;
`GET /api/connectors/contacts` fills the suggestions for `autonomous.notify_to`,
which stores a person's NAME — the same string `notify_user`'s `to` takes, resolved
by the same code, so a default target is checkable and survives that person's chat
id changing. A raw id still works (a group's is negative and no address book can
hold it). Secrets are masked, never forwarded. Without the plugin the routes do not exist, so the fetches fail and the
form falls back to a text input — which is the right control in that state, since
the id must still be typeable.

**The `satellite` channel** is a voice device (PC/Raspberry) running the
standalone client in the repo's `satellite/` folder — installed on the device
by its own `install.sh`, not inside myagent. The transport is inverted: the
device calls us. Speech (or text) arrives on
`POST /api/connectors/inbound/{binding_id}`, audio is transcribed server-side
by the same Whisper path Telegram voice notes use, and the agent's reply
travels back **in the same HTTP response** (a voice exchange must not race a
push); the device's `/say` endpoint stays reserved for unsolicited messages —
what `notify_user` delivers — and `/health` answers the UI's test button. One
shared key works in both directions: the binding's token. That inbound route
authenticates itself, so the plugin registers its prefix in
`app.state.self_authenticated_prefixes` and the global MYAGENT_API_KEY
middleware steps aside for it — a handoff, never an exemption: the route
enforces the per-binding key with a constant-time compare, and an unknown id
answers exactly like a bad key. A satellite has one conversation
(chat id ≡ binding id), and an address-book contact whose `satellite` handle
is the binding id lets agents notify the device by name — the notification
lands in the same session as the spoken turns.

**The address book reaches the model through the tool schema.** The plugin exposes
`Connectors.notify_targets()` (contact names, channel labels, the broadcast word);
the core reads it through a late-bound `ToolRegistry.notify_targets` callable and
the executor pins `notify_user`'s `to` and `channel` to those values per turn
(`_with_notify_targets`, deep-copying the cached definition exactly as
`_with_delegation_targets` does for `call_agent`). Names only, never handles: a
model shown an id pastes it into `chat_id` and skips the lookup that makes the
recipient verifiable. This is the cheapest of the three options — a separate
"list the contacts" tool would carry its own definition in every payload, more
tokens than the enum it replaces, plus an iteration; a prompt section costs more
for the same names and constrains nothing. Above 20 contacts the enum is dropped
rather than truncated, because the text protocol would cut it silently. `to: "all"`
broadcasts to every contact reachable on that channel, and names the ones it could
not reach in the result.

## Plugins

`~/myagent/plugins/<id>/plugin.py` exposing `register(app)` (plus optional
`startup`/`shutdown` lifespan hooks) — loaded by `app/plugins.py` between the core
routers and the static catch-all mount, because Starlette matches routes in
registration order. Every step is guarded: a broken plugin logs a warning and is
reported by `GET /api/plugins` with `loaded: false`, but never stops the server
from starting. Nothing in the core names a specific plugin; there is no bundled
plugin layer, since shipping one would put optional online code inside an install
meant to work offline. Full contract and isolation rules: `docs/PLUGINS.md`.

## Data storage

Everything is plain JSON under `~/myagent/` (see `server/app/config.py`):

| Path | Env override | Contents |
|---|---|---|
| `~/myagent/config/agents/` | `MYAGENT_CONFIG` | one JSON per agent |
| `~/myagent/config/models/` | `MYAGENT_CONFIG` | LLM configs; a remote `api_key` is stored 0600, masked in the API, and PUT treats mask/empty as "keep" |
| `~/myagent/config/settings.json` | `MYAGENT_CONFIG` | default model, provider base URLs |
| `~/myagent/config/mcp/` | `MYAGENT_CONFIG` | one JSON per MCP server (0600: `env`/`headers` may hold secrets) + `cache/` with the discovered tool catalogue |
| `~/myagent/config/tasks/` | `MYAGENT_CONFIG` | one JSON per scheduled task (agent + prompt + cron/`at`) — user intent, hence config and not runtime state |
| `~/myagent/tools/` | `MYAGENT_TOOLS` | tool folders |
| `~/myagent/library/` | `MYAGENT_LIBRARY` | offline knowledge for `local_search` (ZIM archives, notes) — user-placed, never written by the app |
| `~/myagent/workspace/` | `MYAGENT_WORKSPACE` | working dir for agents' file operations; relative paths in file/shell tools resolve here |
| `~/myagent/sessions/` | `MYAGENT_SESSIONS` | `current.json`, `history/`, `channels/` (connector chats) |
| `~/myagent/memory/` | `MYAGENT_MEMORY` | per-agent long-term memory: `<agent_id>/memory.md` + `chunks/*.md` |
| `~/myagent/autonomy/` | `MYAGENT_AUTONOMY` | live agents' runtime state: `<agent_id>/state.json` (the schedule itself lives in `config/tasks/`) |
| `~/myagent/connectors/` | `MYAGENT_CONNECTORS_DIR` | connectors *plugin* state: bot bindings (0600), grants, contacts, kill switch |
| `~/myagent/plugins/` | `MYAGENT_PLUGINS` | installed plugins (code, replaceable — state never lives here) |
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
`ui/js/app.js`; chat streaming uses `fetch` on `/api/chat/stream`.
UI strings go through `i18n('key')` with dictionaries in `ui/js/i18n/en.js`
and `it.js` (both must be kept in sync). No build step; Bootstrap is vendored
under `ui/vendor/`.

The UI does not have to be served by MyAgent: it is plain static HTML with
relative asset paths, so any web server can host it, and `App.serverBase`
(Settings → *MyAgent server*, or a one-shot `?server=` URL parameter; stored
in `localStorage`, empty = same origin) points it at the API. Every request —
including the two streaming `fetch`es in `chat.js` — is built through
`App.apiUrl(path)`; a literal `/api/...` URL would silently talk to the host
serving the static files. The server side of the split is
`MYAGENT_CORS_ORIGINS` (comma-separated origins; unset = no CORS layer,
same-origin only). The CORS middleware is added AFTER the API-key middleware,
which makes it the outer layer: a CORS preflight carries no `Authorization`
header, so the key check must not see it first.

### Installable (PWA)

`ui/manifest.webmanifest` + `ui/sw.js` + `ui/js/pwa.js` make the UI installable
as an app, with the shell available offline. Everything is relative
(`start_url` and `scope` are `"."`), so it also installs from a subpath. The
manifest needs its MIME type registered (`mimetypes.add_type` in
`server/main.py`) — Python's table does not know `.webmanifest` and browsers
reject a manifest served as `text/plain`.

Three properties of the service worker are load-bearing:

- **Nothing under `/api/` is ever cached.** The shell is static and versioned;
  API answers are live state, and a stale agent list or a replayed chat turn is
  worse than an error. Requests to a *remote* server (`App.serverBase`) are
  cross-origin, which the worker already skips.
- **`index.html` is network-first**, everything else cache-first. Every asset in
  it carries a `?v=N` buster, making the HTML the only unversioned file — served
  from cache first, an upgrade would never be seen. Offline, the cached copy
  answers.
- **The precache list is derived from `index.html`**, not written out in
  `sw.js`. Duplicating the `?v=N` stamps across two files means one of them
  rots. Each navigation re-derives the list from the HTML it just fetched,
  caches what is missing and deletes superseded `?v=` copies, so a version bump
  needs no worker change.

Install and worker both require a **secure context**: `localhost` qualifies,
a plain-http LAN address does not, and browsers offer neither without saying
why — Settings → *Install app* reports that case explicitly, alongside the
install button and a "clear cache and reload" escape hatch. For the LAN/VPN
case, `MYAGENT_SSL_CERTFILE` / `MYAGENT_SSL_KEYFILE` (`_tls_files()` in
`server/main.py`) hand a certificate to uvicorn, so HTTPS needs no reverse
proxy. The key is optional — `ssl.load_cert_chain` reads it out of a combined
PEM — but the paths are validated up front, because uvicorn surfaces a typo as
a bare `FileNotFoundError` from inside the `ssl` module. Note that the
certificate must be *trusted*: on an origin with a certificate error Chrome
still refuses to register a service worker, so clicking through a self-signed
warning buys nothing.

Icons in `ui/icons/` are the bootstrap-icons `bi-robot` glyph (U+F6B1, the same
one the navbar shows) rendered white on a blue gradient.

## Key files

| Task | File |
|---|---|
| Agent execution loop | `server/app/engine/executor.py` |
| Text-based tool-call parsing | `server/app/engine/toolcall_parser.py` |
| LLM communication | `server/app/engine/llm_provider.py` |
| Context window / capability probe | `server/app/engine/model_probe.py` |
| Live (client-decoupled) runs | `server/app/engine/live.py` |
| Long-term memory store (memory.md + chunks) | `server/app/storage/memory.py` |
| Memory compaction pipeline | `server/app/engine/memory_compactor.py` |
| Autonomy scheduler + wake turns | `server/app/engine/autonomy.py` |
| Scheduled-task store (`next_at` is computed here) | `server/app/storage/tasks.py` |
| Cron parser / next-occurrence solver | `server/app/engine/cron.py` |
| One agent turn on a channel session | `server/app/engine/channel_turn.py` |
| Plugin discovery + lifecycle | `server/app/plugins.py` |
| Connectors plugin (messaging bots) | `connectors/plugin/` (see `docs/PLUGINS.md`) |
| Tool discovery/execution | `server/app/tools/registry.py` |
| Internal tool handlers | `server/app/tools/internal.py`, `server/app/tools/memory_tools.py` |
| MCP wire protocol (stdio + HTTP) | `server/app/mcp/client.py` |
| MCP connections, discovery, policy | `server/app/mcp/manager.py` |
| Data models (Pydantic) | `server/app/models.py` |
| Paths and env vars | `server/app/config.py` |
| API endpoints | `server/app/routers/` |
| Frontend JS | `ui/js/` |
| Installable app (manifest, offline shell) | `ui/manifest.webmanifest`, `ui/sw.js`, `ui/js/pwa.js` |
