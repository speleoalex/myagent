# Installing MyAgent

The short version is in the [README](../README.md#quickstart): clone, run
`./install.sh`, open the URL it prints. This page covers everything around it —
requirements, optional dependencies, the install modes (per-user, service
account, root, macOS, development), installing the UI as an app, and hosting
that UI somewhere else.

## Requirements

- **Python 3.10+** (3.12 recommended). `./install.sh` refuses anything older
  before it installs a thing — on 3.9 the failure would otherwise arrive much
  later, as a pydantic traceback.
- **At least one LLM backend.** For a fully offline setup, a local one:
  - llama.cpp server (`http://localhost:8080`), or
  - Ollama (`http://localhost:11434`), or
  - any remote OpenAI-compatible API with a key — OpenAI, OpenRouter, Groq,
    Mistral, a vLLM box on the LAN … *(needs internet)*, or
  - the Anthropic API (Claude), spoken natively *(needs internet)*
- **`libzim`** — to search offline Wikipedia archives. Installed by `install.sh`
  into its own virtualenv, so there is nothing to do.
- **Node.js + Chrome/Chromium** *(optional)* — only for the online web tools.

`install.sh` offers to install the missing system packages, then reports what
it found, backend included:

```text
  Optional system dependencies:
    Missing: PDF text extraction, OCR
    Command: sudo apt-get install -y poppler-utils tesseract-ocr
    Install them now? [y/N] y
[5/5] LLM backend and optional features:
  [ok] LLM backend          (Ollama, 4 model(s))
  [ok] web browsing/search  (Node + Chrome/Chromium)
  [ok] offline library      (7 archive(s) in /home/you/myagent/library)
```

The prompt defaults to **no** and always prints the command, so you can install
it later by hand. With no terminal to answer on — a container build, a scripted
deploy — nothing is asked and nothing is installed. `--yes` accepts everything
(also `MYAGENT_ASSUME_YES=1`), `--no-optional` never asks.

Two things `install.sh` deliberately leaves alone: **Chrome/Chromium**, because on
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
| Offline Wikipedia archives | `local_search`, `local_read` | `libzim`, installed by `install.sh` (`.zim` files only — Markdown/text notes need nothing). To repair it by hand: `server/.venv/bin/pip install libzim` |
| Speech to text (audio files, Telegram voice notes, voice satellites) | `document_extract` | `ffmpeg` + `faster-whisper` (installed with the connectors plugin) |

Missing dependencies never block startup: the tool simply fails when called,
and `./install.sh` tells you which ones are dark.

## First run

On first start MyAgent seeds `~/myagent/` with the bundled agents, models and
tools. Nothing is overwritten on later runs, so upgrading is safe and your
edits survive.

Every bundled agent runs on the model chosen in **Settings**. If that model
isn't answering, MyAgent runs the turn on whichever local backend *is*
reachable and says so above the answer — so the first message works even
before you have matched the default to your setup. It never rewrites the
setting, and never falls back onto a model with an API key.

## Install modes

`./install.sh` is the one installer: Python venv, tool dependencies, service
registration. *What* it installs follows who runs it — it never takes sudo on
its own:

| How you run it | Code | State (`MYAGENT_HOME`) | Service | Runs as |
|---|---|---|---|---|
| `./install.sh` (Linux, no sudo) | `~/myagent/bin` | `~/myagent` | systemd **user** unit | you |
| `sudo ./install.sh` → **[1]** service account *(default)* | `/home/myagent/myagent/bin` | `/home/myagent/myagent` | system unit, `User=myagent` | `myagent` |
| `sudo ./install.sh` → **[2]**, or `--as-root` | `/opt/myagent` | `/root/myagent` | system unit, `User=root` | root |
| `./install.sh` (macOS) | the checkout | `~/myagent` | LaunchAgent | you |
| `./install.sh --dev` | the checkout | `~/myagent` | none — `server/.venv/bin/python server/main.py` | you, by hand |

`MYAGENT_INSTALL_DIR` overrides the code location in the first three modes;
`--port N` (or `MYAGENT_PORT`) picks the port, otherwise the first free one
from 8888 up is used and kept on later runs. Every mode is safe to re-run:
runtime state lives under the service user's `~/myagent`, never inside the code.

**Per-user (no sudo).** The service runs as you, so it can do exactly what you
can — and nothing more: several users on one machine each get their own
instance, on their own port, and ordinary file permissions keep one user's
agents out of another's home. A user unit stops at logout; to keep it running
across logouts and reboots: `loginctl enable-linger $USER` (may need sudo —
`install.sh` says whether it is already on). Commands: `systemctl --user status
myagent`, `journalctl --user -u myagent -f`.

**Service account (sudo, default).** `install.sh` creates a system account
`myagent` (no login shell) and runs the service as it. This is the OS-level
boundary for a shared machine: the agents' shell and file tools cannot reach
any real user's files. The admin who ran the install is added to group
`myagent`, and the state tree is group-writable (with `UMask=0002` in the
unit), so `/home/myagent/myagent` can be read and edited without sudo after the
next login — secrets stay `0600`, as they should. Anything that must run *as
the service user* (plugins, `library/fetch.py`) goes through
`sudo -u myagent …`; the installer prints the exact commands.

**Root (sudo, `--as-root`).** For a box that is MyAgent's alone. The service
has every power on the machine, and so does every agent holding `shell_exec`
or a file tool: keep `MYAGENT_HOST=127.0.0.1`.

**Configuring the service.** The unit file is rewritten on every install (and
`./update.sh` reinstalls automatically), so never edit it directly — your
changes would be lost. Put environment overrides (`MYAGENT_HOST`,
`MYAGENT_API_KEY`, TLS, debug) in a drop-in, which installs leave untouched:

```bash
systemctl --user edit myagent        # per-user install
sudo systemctl edit myagent          # service account or root
# [Service]
# Environment=MYAGENT_HOST=0.0.0.0
systemctl --user restart myagent     # or: sudo systemctl restart myagent
```

On macOS the LaunchAgent is `~/Library/LaunchAgents/com.myagent.agent.plist`
(logs in `~/Library/Logs/myagent.log`); it too is rewritten on every install,
so set overrides by re-running `install.sh` with the environment you want.

Windows is not supported natively — the tool `run` scripts are extensionless
and rely on shebangs, which Windows does not honor. Use WSL2.

The connectors plugin and the voice satellite have their own installers and are
deliberately not shipped by `install.sh`: see
[connectors/README.md](../connectors/README.md) and
[satellite/README.md](../satellite/README.md).

## Updating

From the git checkout, `./update.sh` fetches GitHub and compares by git
ancestry, never by date: only when GitHub is strictly ahead does it
fast-forward the checkout and re-run `install.sh` against the installed
service it finds (your user unit, or the system-wide one — with sudo, in the
same mode it was installed). If the checkout has commits GitHub does not have, or
uncommitted edits to tracked files, **nothing is overwritten** — the script
explains why and exits with code 2. `--check` reports what would happen
without changing anything; `--no-deploy` updates the checkout only.

## Uninstalling

`./uninstall.sh` (from the git checkout) removes the **service and the code,
never your data**. Run it the way you installed: as yourself for a per-user or
macOS install, `sudo ./uninstall.sh` for the service-account or root install.
`--dry-run` prints the plan and changes nothing; `--yes` skips the confirmation.

It reads the installed unit (or LaunchAgent) to find what to remove — the unit
itself with its drop-ins, and the code directory the unit points at (venv,
`node_modules`, copied sources) — and refuses to delete any directory that holds
runtime data (`config/`, `sessions/`, …), whatever the unit says. What stays:
`~/myagent` of the service user (agents, models, API key, sessions, memory,
library, workspace, connector state, plugins), the `myagent` service account and
the LLM backend's models. The script prints the exact commands for the two
things it deliberately leaves to you (`rm -rf` of the data, `userdel -r
myagent`). Reinstalling later with `./install.sh` picks everything up as it was.

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
