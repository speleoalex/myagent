# Configuration and runtime layout

Everything MyAgent needs is configured either from the UI (agents, models,
tools, connectors) or through environment variables. **None of the variables
are required** — the defaults are a working single-user install on localhost.

## Runtime layout

All runtime state lives under `~/myagent/`, decoupled from the code, so
upgrading or redeploying never touches your data:

```text
~/myagent/
├── config/      # agents, models (API keys, 0600), MCP servers, settings — small & precious: back this up
├── connectors/  # channel bindings (bot tokens and device keys, 0600), grants, address book
├── plugins/     # installed plugins — code, replaceable (see PLUGINS.md)
├── tools/       # your tools: the ones you (or the AI) create, plus your edits to the built-in ones
├── library/     # your offline knowledge: Wikipedia ZIM archives + notes/documents
├── workspace/   # working directory for agents' file operations (+ _attachments/ scratch, _resources/ files shown in chat)
├── sessions/    # chat state: current, history, connector channels
├── memory/      # per-agent long-term memory (only for agents that enable it)
├── autonomy/    # live agents' state and event queues (only for started agents)
├── cache/       # derived data, safe to delete (pdftext/: PDF text layers, for library search)
└── logs/        # debug.log (only with MYAGENT_DEBUG=1)
```

Two of those directories hold everything that is irreplaceable — a full backup
of what matters:

```bash
tar czf myagent-backup.tgz -C ~ myagent/config myagent/connectors
```

The library is deliberately left out: it is large, and it is re-downloadable
with [`library/fetch.py`](../library/README.md).

## Environment variables

### Network and access

| Variable | Default | Meaning |
|---|---|---|
| `MYAGENT_HOST` | `127.0.0.1` | bind address — see [Security](../README.md#security) before changing it |
| `MYAGENT_PORT` | `8888` | bind port |
| `MYAGENT_API_KEY` | *(unset)* | pin the API key (Bearer header or `?api_key=`); overrides — and makes read-only — the one managed in Settings → API key |
| `MYAGENT_CORS_ORIGINS` | *(unset = same-origin only)* | comma-separated browser origins allowed to call the API, for a [UI hosted elsewhere](INSTALL.md#hosting-the-ui-elsewhere) |
| `MYAGENT_SSL_CERTFILE` | *(unset = plain http)* | TLS certificate — serve HTTPS directly, no reverse proxy |
| `MYAGENT_SSL_KEYFILE` | *(unset)* | TLS private key; omit if the certificate file is a combined PEM |

### Where things are stored

| Variable | Default | Meaning |
|---|---|---|
| `MYAGENT_CONFIG` | `~/myagent/config` | agents, models, MCP servers, settings |
| `MYAGENT_TOOLS` | `~/myagent/tools` | tool folders |
| `MYAGENT_WORKSPACE` | `~/myagent/workspace` | agents' file-operation root |
| `MYAGENT_SESSIONS` | `~/myagent/sessions` | chat sessions |
| `MYAGENT_MEMORY` | `~/myagent/memory` | per-agent long-term memory |
| `MYAGENT_AUTONOMY` | `~/myagent/autonomy` | live agents' state and event queues |
| `MYAGENT_LIBRARY` | `~/myagent/library` | offline knowledge folder (library tools) |
| `MYAGENT_CACHE` | `~/myagent/cache` | derived data — currently the PDF text layers the library search reads; deleting it only costs one re-extraction |
| `MYAGENT_PLUGINS` | `~/myagent/plugins` | installed plugins |

Pointing `MYAGENT_LIBRARY` at an external disk is the common case — the library
is the one directory that grows to tens of gigabytes.

### Behaviour and diagnostics

| Variable | Default | Meaning |
|---|---|---|
| `MYAGENT_OLLAMA_DEFAULT_CTX` | `4096` | assumed context window of an Ollama model when not probed |
| `MYAGENT_CHANNEL_ROTATE_BYTES` | `2 MiB` | size at which a channel session is archived and restarted |
| `MYAGENT_MCP_SHUTDOWN_TIMEOUT` | `10` | seconds shutdown waits for MCP servers to close |
| `MYAGENT_DEBUG` | *(off)* | `1` = verbose executor trace — **logs full chat content** |
| `MYAGENT_DEBUG_FILE` | `~/myagent/logs/debug.log` | trace file location |

`MYAGENT_DEBUG=1` is the tool for "why did the agent do that": it records every
message sent to the model, the raw reply and the parsed tool calls. It is
cleared at each top-level turn, and it contains everything you said — enable it
while debugging an agent, not permanently.

The connectors plugin adds a few of its own (state directory, poll timeout,
Whisper model) — see [connectors/README.md](../connectors/README.md).

## Settings stored in the UI

Some things are configuration but belong to the install rather than the
deployment, so they live in `~/myagent/config/settings.json` and are edited
under **Settings**:

- **Default model** — every bundled agent uses it, so pointing the whole set at
  another model is one choice in one place.
- **Ollama / llama.cpp base URLs** — used to discover models and to probe
  reachability.
- **API key** — generated, rotated or removed without a restart. Overridden by
  `MYAGENT_API_KEY` when that is set.
- **MyAgent server** — a browser-side preference, for when the UI is served
  from somewhere other than the API.
