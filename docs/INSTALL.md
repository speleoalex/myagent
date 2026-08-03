# Installing MyAgent

The short version is in the [README](../README.md#quickstart): clone, run
`./setup.sh`, start the server. This page covers everything around it —
requirements, optional dependencies, running it as a service, installing the UI
as an app, and hosting that UI somewhere else.

## Requirements

- **Python 3.10+** (3.12 recommended). `./setup.sh` refuses anything older
  before it installs a thing — on 3.9 the failure would otherwise arrive much
  later, as a pydantic traceback.
- **At least one LLM backend.** For a fully offline setup, a local one:
  - llama.cpp server (`http://localhost:8080`), or
  - Ollama (`http://localhost:11434`), or
  - any remote OpenAI-compatible API with a key — OpenAI, OpenRouter, Groq,
    Mistral, a vLLM box on the LAN … *(needs internet)*, or
  - the Anthropic API (Claude), spoken natively *(needs internet)*
- **`libzim`** — to search offline Wikipedia archives. Installed by `setup.sh`
  into its own virtualenv, so there is nothing to do.
- **Node.js + Chrome/Chromium** *(optional)* — only for the online web tools.

`setup.sh` offers to install the missing system packages, then reports what it
found, backend included:

```text
[3/4] Optional system dependencies...
  Missing: PDF text extraction, OCR
  Command: sudo apt-get install -y poppler-utils tesseract-ocr
  Install them now? [y/N] y
[4/4] LLM backend and optional features:
  [ok] LLM backend          (Ollama, 4 model(s))
  [ok] web browsing/search  (Node + Chrome/Chromium)
  [ok] offline library      (7 archive(s) in /home/you/myagent/library)
```

The prompt defaults to **no** and always prints the command, so you can install
it later by hand. With no terminal to answer on — a container build, a scripted
deploy — nothing is asked and nothing is installed. `--yes` accepts everything
(also `MYAGENT_ASSUME_YES=1`), `--no-optional` never asks.

Two things `setup.sh` deliberately leaves alone: **Chrome/Chromium**, because on
Ubuntu the apt package is a snap shim that fails inside containers and a
headless server often wants no browser at all, and the **library archives**,
which are gigabytes onto a disk only you can pick (see below).

## Optional features

The core install needs only Python. Extra tools light up when their system
dependencies are present:

| Feature | Tools | Needs |
|---|---|---|
| Web search & browsing | `web_search`, `browse_web`, `web_research` | Node.js + Chrome/Chromium (`PUPPETEER_EXECUTABLE_PATH` honored) |
| Document extraction (PDF, images) | `document_extract` | `poppler-utils`, `tesseract`, `pandoc` (each optional) |
| Offline Wikipedia archives | `local_search`, `local_read` | `libzim`, installed by `setup.sh` (`.zim` files only — Markdown/text notes need nothing). To repair it by hand: `server/.venv/bin/pip install libzim` |
| Speech to text (audio files, Telegram voice notes, voice satellites) | `document_extract` | `ffmpeg` + `faster-whisper` (installed with the connectors plugin) |

Missing dependencies never block startup: the tool simply fails when called,
and `./setup.sh` tells you which ones are dark.

## First run

On first start MyAgent seeds `~/myagent/` with the bundled agents, models and
tools. Nothing is overwritten on later runs, so upgrading is safe and your
edits survive.

Every bundled agent runs on the model chosen in **Settings**. If that model
isn't answering, MyAgent runs the turn on whichever local backend *is*
reachable and says so above the answer — so the first message works even
before you have matched the default to your setup. It never rewrites the
setting, and never falls back onto a model with an API key.

## Run as a service

- **Linux (systemd):** `sudo bash deploy.sh` — installs to
  `/opt/applications/myagent` (override with `MYAGENT_INSTALL_DIR`) and
  registers the `myagent` service.
  Logs: `journalctl -u myagent -f`.
- **macOS (launchd):** `bash deploy-macos.sh` — per-user LaunchAgent, no sudo.
  Logs: `~/Library/Logs/myagent.log`.

Both are safe to re-run: runtime state lives under `~/myagent/`, never inside
the install directory.

Windows is not supported natively — the tool `run` scripts are extensionless
and rely on shebangs, which Windows does not honor. Use WSL2.

The connectors plugin and the voice satellite have their own installers and are
deliberately not shipped by `deploy.sh`: see
[connectors/README.md](../connectors/README.md) and
[satellite/README.md](../satellite/README.md).

## Install it as an app

Open *Settings → Install app* and MyAgent installs like a native application:
its own window, an icon in the launcher or on the home screen, no address bar.
The interface is cached, so it opens even with the network down — the agents
themselves keep needing the server, which is the point of running it on your
own machine.

On iPhone and iPad, Safari has no install button: use *Share → Add to Home
Screen*.

### Installing from another device

Browsers only offer this over a **secure connection**. `http://localhost:8888`
counts as one, so the default setup works as is — but a LAN or VPN address in
plain http does not, and the browser withholds both the install prompt and the
offline cache without explaining why (Settings says so, instead of showing a
button that does nothing).

MyAgent can serve HTTPS itself, no reverse proxy involved:

```bash
MYAGENT_SSL_CERTFILE=/path/fullchain.pem MYAGENT_SSL_KEYFILE=/path/privkey.pem \
  server/.venv/bin/python server/main.py
```

(Omit `MYAGENT_SSL_KEYFILE` if the certificate file is a combined PEM.)

What matters is that the certificate is **trusted**, not merely present: with a
self-signed one you click through, the origin keeps a certificate error and
Chrome refuses to register the service worker — no install, no offline. Three
ways to get a trusted one for a private address:

- **`tailscale cert`** — a real certificate for your `*.ts.net` name, renewed
  automatically, trusted everywhere without touching any device.
- **Let's Encrypt via DNS-01** — point a domain you own at the private IP
  (`myagent.example.com` → `10.147.0.5`). The DNS challenge needs no inbound
  reachability, so it works for an address only your VPN can reach.
- **[mkcert](https://github.com/FiloSottile/mkcert)** — a local CA, quick on
  your own machines, fiddly to install on a phone.

If you are on Chrome and the traffic is already encrypted by a VPN
(WireGuard, Tailscale, ZeroTier), there is a cheaper route: add the origin to
`chrome://flags/#unsafely-treat-insecure-origin-as-secure`, once per browser.
Safari has no such flag, so an iPhone needs a real certificate.

## Hosting the UI elsewhere

By default MyAgent serves its own UI, and the UI talks to the origin it was
loaded from — nothing to configure. But the UI is plain static HTML (`ui/`),
so any web server can host it, at any path (asset references are relative):
copy the `ui/` folder to an Apache/nginx document root and tell it where the
API lives.

Two pieces make the split work:

1. **Point the UI at the server** — in *Settings → MyAgent server*, or by
   opening the UI once as
   `http://apache-host/myagent/?server=http://myagent-host:8888`
   (stored in the browser and stripped from the URL, like `?api_key=`; an empty
   `?server=` resets it to same-origin).
2. **Let the browser through** — cross-origin calls need the server's CORS
   consent: start MyAgent with `MYAGENT_CORS_ORIGINS=http://apache-host`
   (comma-separated list, `*` for any). Unset, the API stays same-origin only,
   which is the right default for the classic single-server setup.

Remember that the server must also be reachable from the browser's machine
(`MYAGENT_HOST=0.0.0.0` + an API key — see
[Security](../README.md#security)).

## Troubleshooting

- **"No model can answer yet"** on the home page — nothing is listening. Start
  Ollama (`ollama serve`) or a llama.cpp server, then reload.
- **Your backend is on another port or host** — set its address under
  **Models**, or the base URLs under **Settings**.
- **`Address already in use`** — something else has 8888:
  `MYAGENT_PORT=8899 server/.venv/bin/python server/main.py`.
- **A tool "works" in a terminal but not from an agent** — check its `run` is
  executable and its shebang points at an interpreter that has its
  dependencies; a service `PATH` is minimal. See
  [TOOLS.md](TOOLS.md).
- **Everything is stale after an upgrade** — the UI shell is cached by the
  service worker. *Settings → Install app* has a "clear the cache and reload"
  button.
