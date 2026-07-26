# MyAgent

**An AI assistant that keeps working when the internet doesn't.**

MyAgent is a self-hosted agent platform designed to be useful **offline**: it
runs local models (via [llama.cpp](https://github.com/ggml-org/llama.cpp) or
[Ollama](https://ollama.com)), answers from a **local knowledge library** — a
full offline Wikipedia plus your own notes and documents — and talks to the
**IoT and home-automation devices on your LAN**. No cloud account, no
outbound traffic required. Point it at a remote API instead if you want to;
that's a choice, not a dependency.

Think: a boat, a mountain hut, a lab with no uplink, a blackout, or simply a
home where you'd rather not send everything to someone else's server.

## Features

- **Works offline** — local model + local knowledge + local devices. Nothing
  in the core path needs an internet connection
- **Offline knowledge library** — drop Wikipedia ZIM archives and your own
  Markdown/text documents into `~/myagent/library/`; the `local_search` tool
  searches them full-text (see [Offline knowledge](#offline-knowledge-the-library))
- **IoT & home automation** — agents call your devices' local HTTP APIs
  (Home Assistant, Shelly, Tasmota, ESPHome, Hue …) over the LAN
  (see [Local devices](#local-devices--home-automation))
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
- **Optional online tools** — web search and page reading are there when you
  *do* have connectivity, in a separate agent
- **i18n UI** — English and Italian out of the box

## Requirements

- **Python 3.10+** (3.12 recommended)
- At least one LLM backend — for a fully offline setup, a local one:
  - llama.cpp server (`http://localhost:8080`), or
  - Ollama (`http://localhost:11434`), or
  - any remote OpenAI-compatible API with an API key *(needs internet)*
- **`libzim`** *(optional)* — to search offline Wikipedia archives
- **Node.js + Chrome/Chromium** *(optional)* — only for the online web tools

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

## Offline knowledge (the library)

Everything in `~/myagent/library/` becomes searchable knowledge for your
agents. Two kinds of files live side by side in that folder:

| Put in `~/myagent/library/` | Searched how |
|---|---|
| **Wikipedia / ZIM archives** (`*.zim`) | full-text index built into the archive |
| **Your notes and documents** (`.md`, `.txt`, `.rst`) | keyword scorer, results returned per section with `file › heading` |

The **Librarian** agent is wired to it out of the box: ask it a question and
it searches the library and answers from what it finds — citing the article
or file — or tells you the library doesn't cover it. The **Master** agent
delegates general-knowledge questions to it before touching the web.

### Getting an offline Wikipedia

Download a `.zim` from [Kiwix](https://download.kiwix.org/zim/wikipedia/) and
drop it in the folder — no import step, no restart:

```bash
mkdir -p ~/myagent/library
cd ~/myagent/library
# ~12 GB for the full English "mini" edition; smaller editions exist
wget https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_mini.zim
server/.venv/bin/pip install libzim      # required for .zim files only
```

Several archives can coexist (e.g. English + Italian); an agent can restrict
a search to one edition with the tool's `lang` parameter.

### Your own documents

Copy Markdown or plain-text files (manuals, procedures, notes, wiki exports)
straight into `~/myagent/library/` — subfolders are scanned recursively.
PDFs, images and audio are **not** searched directly: convert them first with
the `document_extract` tool (ask an agent to extract the file and write the
Markdown into the library), then they become searchable like any other note.

Each agent can also have its **own** knowledge folder instead of the shared
one — the `local_search` tool takes an optional `path`.

## Local devices & home automation

Agents reach the devices on your LAN through the `http_request` tool, which
speaks the local HTTP APIs that home-automation gear already exposes — Home
Assistant, Shelly, Tasmota, ESPHome, Philips Hue, ESP32 sketches, a Raspberry
Pi you wrote yourself. This all happens inside your network: no vendor cloud,
no internet.

The bundled **Home Automation** agent is a template: open it in
**Agents → Home Automation** and list your devices in the *My devices*
section of its system prompt, one line each with the exact URL to call —

```text
- Living room light (Shelly): ON -> GET http://192.168.1.50/relay/0?turn=on
- Home Assistant: POST http://192.168.1.10:8123/api/services/light/turn_on
  header {"Authorization": "Bearer TOKEN"}  body {"entity_id": "light.kitchen"}
```

— then just say *"turn on the living room light"*. For protocols beyond HTTP
(MQTT, Zigbee, serial, GPIO) write a tool: a folder with a `tool.json` and a
`run` script that shells out to `mosquitto_pub`, a Python library, or
whatever your hardware speaks — see [docs/TOOLS.md](docs/TOOLS.md).

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
├── library/     # your offline knowledge: Wikipedia ZIM archives + notes/documents
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

First run seeds six agents:

| Agent | Does | Needs internet? |
|---|---|---|
| **Master** | orchestrator: routes your question to the right agent via `call_agent` | no |
| **Librarian** | answers from the offline library (Wikipedia ZIM + your documents) | no |
| **Home Automation** | drives IoT devices over their local HTTP APIs — customize with your devices | no |
| **System Administrator** | shell and file operations on the machine | no |
| **Conversation** | plain chat | no |
| **Web Researcher** | searches and reads web pages | yes |

…plus three model configs: `llama-cpp` (whatever your llama.cpp server is
serving on `:8080`), `gemma4` and `qwen3` (Ollama). The Ollama entries are
models with native tool-calling support, which is what makes agents usable
with small local models. Everything is editable and individually
re-importable from the UI (**Agents → Native agents**, **Tools → Native
tools**).

## Optional features

The core install needs only Python. Extra tools light up when their system
dependencies are present (`./setup.sh` reports what it finds):

| Feature                          | Tools                                | Needs                                             |
|----------------------------------|--------------------------------------|---------------------------------------------------|
| Web search & browsing            | `web_search`, `browse_web`, `web_research` | Node.js + Chrome/Chromium (`PUPPETEER_EXECUTABLE_PATH` honored) |
| Document extraction (PDF, images, audio) | `document_extract`           | `poppler-utils`, `tesseract`, `pandoc`, `ffmpeg` (each optional) |
| Offline Wikipedia archives       | `local_search`                       | `pip install libzim` (`.zim` files only — Markdown/text notes need nothing) |
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
