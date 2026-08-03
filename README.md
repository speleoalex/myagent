# MyAgent

***English** · [Italiano](README.it.md)*

**An AI assistant that keeps working when the internet doesn't.**

MyAgent runs entirely on your own machine: a local model, a local knowledge
library, and the devices on your own network. No cloud account, no telemetry,
no outbound traffic required. Point it at a remote API if you prefer — that's a
choice, not a dependency.

Because nothing is fetched at answer time, you can give it knowledge that
outlives your connection: **a full offline Wikipedia** alongside medicine,
first aid, repair manuals, agriculture and appropriate technology — tens of
gigabytes on a disk you own, which the agents search and quote from.

![The Librarian agent answering from the offline library](docs/images/chat-librarian.png)

*A local model answering from the offline library: the Librarian searches, opens
the best hit and answers from it. Nothing in that path touches the network.*

## What it's for

- **Off-grid and remote** — a boat, a mountain hut, a field station, a valley
  the network never reached
- **When infrastructure fails** — a blackout or an outage that outlasts the
  day: the medical, repair and agriculture references are still there, and
  still answer questions
- **Air-gapped work** — a lab, a workshop or a client site where connecting is
  not allowed
- **Total privacy** — a home, a studio or a practice where your questions and
  your documents must not leave the building
- **Home automation without a vendor cloud** — your lights and sensors answer
  to a model running in the same house
- **A personal library that answers back** — years of your own notes, manuals
  and converted PDFs, searchable and quotable
- **Unattended work** — scheduled agents that check, summarise and notify you
  on their own

## Features

- **Total privacy** — nothing leaves the machine: no account, no telemetry, no
  third party, not even a phone-home for updates. Your conversations,
  documents and device commands stay on hardware you control, and the only
  outbound traffic is what you explicitly turn on
- **Works offline** — local model + local knowledge + local devices; nothing in
  the core path needs a connection
- **Offline knowledge library** — Wikipedia ZIM archives and your own
  Markdown/text documents in `~/myagent/library/`, searched full-text
  ([details](library/README.md))
- **IoT & home automation** — agents call your devices' local HTTP APIs
  (Home Assistant, Shelly, Tasmota, ESPHome, Hue …) over the LAN
  ([details](docs/AGENTS.md#local-devices--home-automation))
- **Atomic agents** — an agent is just `model + system prompt + tools`,
  editable from the UI and stored as a JSON file
- **Tools are folders** — a `tool.json` plus an executable `run` in any
  language, hot-reloaded, no restart. The AI can write its own
  ([details](docs/TOOLS.md))
- **Autonomous agents** — scheduled tasks, unattended runs, agents that
  schedule their own future work and notify you ([details](docs/AUTONOMY.md))
- **Long-term memory** *(opt-in)* — old turns are archived and replaced by
  compact summaries, so an agent remembers without blowing a small model's
  context
- **Agent delegation** — agents call other agents, with per-agent permissions
- **MCP servers** — Model Context Protocol servers over stdio or HTTP join the
  tool list; paste a Claude Desktop config to import ([details](docs/MCP.md))
- **Any backend** — llama.cpp, Ollama, any OpenAI-compatible API and the
  Anthropic API, spoken natively; the context window is *probed*, not guessed
- **Built for small local models** — tool calls parsed from plain text for
  models without native function calling, loop protection, malformed-call
  retries ([why](docs/DESIGN.md))
- **Live chat** — token streaming, background generation you can leave and
  re-attach to, stop button, history, regenerate, prompt editing
- **Thinking models** — chain-of-thought is separated as it streams and shown
  collapsed: it never reaches the reply, the next prompt, or a speaker
- **Telegram and voice** — bridge an agent to a Telegram bot
  ([connectors](connectors/README.md)) or to a voice satellite you talk to out
  loud, with speech recognised on your own server
  ([satellite](satellite/README.md))
- **Installable** — the UI installs as an app and is cached, so it opens with
  the network down
- **i18n UI** — English and Italian out of the box

## Quickstart

You need **Python 3.10+** and **one LLM backend**. If you have neither,
[Ollama](https://ollama.com) is the shortest path:

```bash
ollama pull qwen3          # any tool-capable model will do

git clone https://github.com/speleoalex/myagent.git
cd myagent
./setup.sh
server/.venv/bin/python server/main.py
```

Open **<http://127.0.0.1:8888>**, pick an agent and chat. `setup.sh` reports
which backend it found, and MyAgent answers on whichever local model is
reachable — so the first message works before you have configured anything.

Then fill the library, which is what makes it useful offline:

```bash
server/.venv/bin/pip install libzim     # needed for .zim archives
library/fetch.py --list                 # the catalog, with current sizes
library/fetch.py --preset base          # ~1.4 GB starting set
```

Full requirements, optional dependencies, running as a service and
troubleshooting: **[docs/INSTALL.md](docs/INSTALL.md)**.

## Security

> **MyAgent includes tools that execute shell commands as the server user, and
> there is no sandbox.** By default the API has no authentication and binds to
> `127.0.0.1` — keep it that way unless the network is trusted. Before exposing
> it, set an API key in *Settings → API key* (or pin one with
> `MYAGENT_API_KEY`); over plain http, keep the traffic inside a VPN or let
> MyAgent serve HTTPS itself. Treat access to the API as equivalent to a shell
> on the machine.

Threat model, where the secrets live, and how to expose it safely:
**[docs/SECURITY.md](docs/SECURITY.md)**.

## Documentation

### Getting it running

- [Installing](docs/INSTALL.md) — requirements, optional dependencies, service,
  installing the UI as an app, hosting it elsewhere, troubleshooting
- [Configuration](docs/CONFIGURATION.md) — environment variables, the
  `~/myagent/` layout, what to back up
- [Security](docs/SECURITY.md) — threat model, API key, transport, secrets

### Using it

- [The library](library/README.md) — what offline knowledge is worth having,
  how to download it, how agents search it
- [Agents and devices](docs/AGENTS.md) — the bundled agents, the agent form,
  wiring up your home automation
- [Autonomous agents](docs/AUTONOMY.md) — scheduled tasks, live agents, guard
  rails
- [MCP servers](docs/MCP.md) — adding them, granting them, their limits
- [Connectors](connectors/README.md) — Telegram bots, channels, address book
- [Voice satellite](satellite/README.md) — the speaker/microphone client

### Understanding and extending it

- [Design rationale](docs/DESIGN.md) — why it's built this way, and what is
  deliberately absent
- [Writing tools](docs/TOOLS.md) — anatomy of a tool, the `run` contract, a
  worked example
- [Writing plugins](docs/PLUGINS.md) — the plugin contract, isolation rules
- [Architecture](docs/ARCHITECTURE.md) — request flow, executor, providers,
  context-window probing, storage

## Contributing

Issues and pull requests are welcome. Keep the stack boring: Python standard
library + FastAPI on the backend, vanilla JS + Bootstrap on the frontend, no
build step.

## Credits

Created by **Alessandro Vernassa**
([@speleoalex](https://github.com/speleoalex)).

## License

[MIT](LICENSE). The UI bundles [Bootstrap](https://getbootstrap.com) and
Bootstrap Icons (MIT).
