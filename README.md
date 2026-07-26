# MyAgent

**Self-hosted AI agent platform.** Define atomic agents — a model, a system
prompt and a set of tools — and run them against any OpenAI-compatible LLM
backend: [llama.cpp](https://github.com/ggml-org/llama.cpp),
[Ollama](https://ollama.com), or a remote API (OpenAI, OpenRouter, Groq, …).
Plain-JSON storage, a vanilla-JS web UI, no database, no framework.

## Features

- **Atomic agents** — each agent is just `model + system prompt + tools`,
  editable from the UI and stored as a JSON file
- **Agent delegation** — agents can call other agents (`call_agent`), with
  per-agent permission control
- **Folder-based tools** — a tool is a folder with a `tool.json` and an
  executable `run` script in any language; hot-reloaded, no restart needed.
  The AI can write its own tools
- **Works with tool-less models** — tool calls are also parsed from plain
  model text, so small local models without native function calling still work
- **Live chat** — token streaming, background generation you can leave and
  re-attach to, stop button, session history
- **Telegram connector** — bridge any agent to a Telegram bot
  (see [connectors/](connectors/README.md))
- **Offline knowledge** — search local Markdown notes and Wikipedia ZIM
  archives with the `local_search` tool
- **i18n UI** — English and Italian out of the box

## Requirements

- **Python 3.10+** (3.12 recommended)
- At least one LLM backend:
  - llama.cpp server (`http://localhost:8080`), or
  - Ollama (`http://localhost:11434`), or
  - any remote OpenAI-compatible API with an API key
- **Node.js** *(optional)* — only for the web tools (`browse_web`,
  `web_search`, `web_research`)

## Quickstart

```bash
git clone https://github.com/speleoalex/myagent.git
cd myagent
./setup.sh
server/.venv/bin/python server/main.py
```

Open **<http://127.0.0.1:8888>**, pick an agent and chat. On first run MyAgent
seeds `~/myagent/` with the bundled agents, models and tools; point the
default model at your backend under **Models** if it isn't llama.cpp on
`localhost:8080`.

## Security

> **MyAgent includes tools that execute shell commands as the server user.**
> By default the API has no authentication and binds to `127.0.0.1` — keep it
> that way unless the network is trusted. Before exposing it, set
> `MYAGENT_API_KEY`: every `/api` request then requires the key, either as an
> `Authorization: Bearer <key>` header or as an `?api_key=<key>` query
> parameter. The web UI asks for the key on first use (or open it once as
> `http://host:8888/?api_key=<key>` — the key is stored in the browser and
> stripped from the URL). For anything internet-facing, still prefer an
> authenticating reverse proxy on top.

## Runtime layout

MyAgent keeps all runtime state under `~/myagent/`, decoupled from the code:

```text
~/myagent/
├── config/      # agents, models (API keys, 0600), settings — small & precious: back this up
├── connectors/  # Telegram bindings (bot tokens, 0600) and grants
├── tools/       # tool folders (hot-reloaded; user/AI-created tools live here)
├── library/     # optional offline knowledge (ZIM archives, notes) for local_search
├── workspace/   # working directory for agents' file operations
├── sessions/    # chat state: current, history, connector channels
└── logs/        # debug.log (only with MYAGENT_DEBUG=1)
```

A full backup of everything that matters:
`tar czf myagent-backup.tgz -C ~ myagent/config myagent/connectors`

## Configuration

Everything is configured via environment variables (none are required):

| Variable             | Default                | Meaning                                    |
|----------------------|------------------------|--------------------------------------------|
| `MYAGENT_HOST`       | `127.0.0.1`            | bind address (see [Security](#security))   |
| `MYAGENT_PORT`       | `8888`                 | bind port                                  |
| `MYAGENT_API_KEY`    | *(unset = no auth)*    | require this key on every `/api` request (Bearer header or `?api_key=`) |
| `MYAGENT_CONFIG`     | `~/myagent/config`     | agents, models, settings                   |
| `MYAGENT_TOOLS`      | `~/myagent/tools`      | tool folders                               |
| `MYAGENT_WORKSPACE`  | `~/myagent/workspace`  | agents' file-operation root                |
| `MYAGENT_SESSIONS`   | `~/myagent/sessions`   | chat sessions                              |
| `MYAGENT_LIBRARY`    | `~/myagent/library`    | `local_search` knowledge folder            |
| `MYAGENT_DEBUG`      | *(off)*                | `1` = verbose executor trace (full chat content) |
| `MYAGENT_DEBUG_FILE` | `~/myagent/logs/debug.log` | trace file location                    |
| `MYAGENT_OLLAMA_DEFAULT_CTX` | `4096`         | assumed context window of an Ollama model when not probed |

The Telegram connector server has its own variables — see
[connectors/README.md](connectors/README.md).

## Bundled agents and models

First run seeds four agents — **Master** (orchestrator that delegates via
`call_agent`), **System Administrator** (shell + file tools), **Web
Researcher** (search + browse) and **Conversation** — and three local model
configs: `llama-cpp` (whatever your llama.cpp server is serving on `:8080`),
`gemma4` and `qwen3` (Ollama). The Ollama entries are models with native
tool-calling support, which is what makes agents usable with small local
models. All of them are editable and individually re-importable from the UI
(**Agents → Native agents**, **Tools → Native tools**).

## Optional features

The core install needs only Python. Extra tools light up when their system
dependencies are present (`./setup.sh` reports what it finds):

| Feature                          | Tools                                | Needs                                             |
|----------------------------------|--------------------------------------|---------------------------------------------------|
| Web search & browsing            | `web_search`, `browse_web`, `web_research` | Node.js + Chrome/Chromium (`PUPPETEER_EXECUTABLE_PATH` honored) |
| Document extraction (PDF, images, audio) | `document_extract`           | `poppler-utils`, `tesseract`, `pandoc`, `ffmpeg` (each optional) |
| Offline Wikipedia / notes search | `local_search`                       | `pip install libzim` (ZIM archives only; plain notes work without) |
| Voice notes on Telegram          | connectors server                    | `ffmpeg` (uses faster-whisper)                    |

## Run as a service

- **Linux (systemd):** `sudo bash deploy.sh` — installs to
  `/opt/applications/myagent` (override with `MYAGENT_INSTALL_DIR`) and
  registers the `myagent` service.
- **macOS (launchd):** `bash deploy-macos.sh` — per-user LaunchAgent, no sudo;
  logs in `~/Library/Logs/myagent.log`.

Both are safe to re-run: runtime state lives under `~/myagent/`, never inside
the install directory. Windows is not supported natively (the tool scripts
rely on shebangs); use WSL2.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — request flow, executor, providers,
  context-window probing, storage
- [Writing tools](docs/TOOLS.md) — anatomy of a tool, the `run` contract,
  a worked example
- [Telegram connectors](connectors/README.md) — standalone messaging-bridge
  server

## Contributing

Issues and pull requests are welcome. Keep the stack boring: Python standard
library + FastAPI on the backend, vanilla JS + Bootstrap on the frontend, no
build step.

## License

[MIT](LICENSE). The UI bundles [Bootstrap](https://getbootstrap.com) and
Bootstrap Icons (MIT).
