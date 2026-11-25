# Plugins

A plugin extends a running myagent: it can mount API routes, publish services on
`app.state` and run background tasks. It is **not** shipped with the core — it is
installed separately, under the user's home, and discovered at startup.

That separation is the point. myagent standalone works offline and carries no
code for optional online services; adding one is an explicit act, and removing it
is deleting a directory.

The `connectors` plugin (messaging channels) is the reference implementation:
`connectors/plugin/` in this repo, installed from **Settings → Server → Plugins**
or with `connectors/install.sh`.

## Layout

```text
~/myagent/plugins/<id>/
├── plugin.json          # manifest (required to be installable)
├── plugin.py            # entry point (required)
├── requirements.txt     # extra Python deps, installed into server/.venv
└── myagent_<id>/        # the plugin's own package
```

```json
{
  "id": "connectors",
  "name": {"en": "Channels", "it": "Canali"},
  "version": "1.0.0",
  "description": {"en": "…", "it": "…"},
  "requires_api": 1
}
```

`id` must equal the directory the plugin is installed in — the installer takes
the directory name from it. `name` and `description` are a string or a
per-language object. `requires_api` is the plugin API level the code was written
against (`PLUGIN_API` in `server/app/plugin_installer.py`, bumped when the
surface listed below changes incompatibly): a plugin asking for a newer one is
refused with "update MyAgent first" instead of being installed to fail at
import. Override the root with `MYAGENT_PLUGINS`.

A directory is **skipped** when its name starts with `.` or `_`, or ends with
`.disabled`. Renaming `connectors` to `connectors.disabled` is therefore the way
to park a plugin without deleting it. The installer relies on the same rule: it
stages a new version as `.<id>.new` and keeps the replaced one as `.<id>.prev`.

## Entry point

```python
def register(app) -> None:            # required, called at startup
async def startup(app) -> None:       # optional, called from the lifespan
async def shutdown(app) -> None:      # optional, called from the lifespan
```

`register(app)` receives the FastAPI application. That is the whole contract:
`app.state` is already how every router in this codebase reaches its services, so
there is no separate context object to keep in sync.

```python
def register(app) -> None:
    app.state.myplugin = build_services(app.state)
    app.include_router(my_router, prefix="/api/myplugin", tags=["myplugin"])
```

Rules that are not optional:

- **Mount routes under `/api/<plugin>/`.** Everything under `/api/` inherits the
  `MYAGENT_API_KEY` gate, and a plugin-specific prefix keeps two plugins from
  fighting over a generic name like `/api/contacts`.
- **Publish exactly one `app.state` key, named after the plugin.** That namespace
  is shared with the core and with every other plugin.
- **Start background tasks in `startup`, not in `register`.** Tasks need the
  running event loop, which does not exist yet when `register` is called.
- **Never use `app.router.on_startup.append(...)`.** This app passes a custom
  `lifespan=` to FastAPI, which replaces Starlette's default handler, so those
  callbacks never run — and never tell you they didn't.
- **Put `register`'s router mounts last.** If building the services raises, no
  endpoint should be left mounted with nothing behind it.

## What a plugin may import from the core

A plugin runs in myagent's process, so `from app...` works and duplication is
unnecessary. What the connectors plugin uses:

| Import | For |
|---|---|
| `app.ids.check_id` | the one definition of the safe entity-id charset |
| `app.storage.store.JsonStore` | one JSON file per record, atomic, 0600 |
| `app.storage.attachments.store_attachment` | put binary content where tools can open it |
| `app.storage.sessions.write_json` | atomic write of a single document |
| `app.routers.secrets.SECRET_MASK` | the write-only-secret sentinel |
| `app.engine.channel_turn.run_channel_turn` | one agent turn on a channel session |
| `app.engine.executor.AgentExecutor` | build an executor for an agent |
| `app.models.ChatRequest` | the validated turn request |

A plugin can also be *read* by a core tool through its `app.state` key, which is
how `notify_user` turns *"message Alessandro on Telegram"* into a chat id: the
connectors plugin exposes `resolve_recipients(name, channel, binding_id, agent_id)`
and the tool calls it when given a name instead of an id. Keep such a seam a plain method returning
`(results, error)` — an error the caller can read and correct beats an exception,
because the caller here is a language model. The same tool then calls the
connector's `notify(chat_id, text, subject, files)` — optional, looked up with
`getattr`, so a plugin that predates it still works through `send` — and reads
the names of the files the transport could not carry from its return value.

These are internal APIs, not a frozen surface: a plugin that imports them is
choosing to follow the core. Keep the list short and stated, so a core
refactoring knows what it can break.

## Where plugin state goes

**Under `~/myagent/<plugin>/`, never inside `~/myagent/plugins/`.**

`plugins/` holds code and is replaced wholesale on reinstall (`rsync --delete`).
State — the connectors plugin's bot tokens, for instance — must survive that, and
must survive uninstalling the plugin entirely. Follow the core's pattern:

```python
STATE_DIR = Path(
    os.environ.get("MYAGENT_MYPLUGIN_DIR") or (Path.home() / "myagent" / "myplugin")
).expanduser()
```

`.expanduser()` matters: without it `MYAGENT_MYPLUGIN_DIR=~/…` — the natural
thing to write in a systemd unit — creates a directory literally named `~` in the
process's working directory, and the data silently disappears on the next deploy.

## Isolation: a plugin shares the process

This is the real cost of in-process plugins, and the rules exist because the
failure modes were measured, not imagined.

- **Every background task catches its own exceptions.** A task that raises does
  not stop the server, but it does stop that task with no explanation unless it
  records why.
- **Every retry loop has a counter and an auto-pause.** An unbounded backoff loop
  fills the *agent's* journal forever, and a status that flips back to "running"
  after each failure hides an outage that has lasted for hours.
- **Nothing blocking on the event loop.** It serves the UI and every SSE stream.
  Base64-encoding a 15 MB upload, or any CPU work, goes in `asyncio.to_thread`.
- **Bound your concurrency.** Inbound work shares the process — and the local
  model — with the web UI. An unbounded burst makes the UI unusable.
- **Heavy native dependencies belong in a subprocess.** A segfault inside a
  native library takes down every chat in the process. The connectors plugin
  transcribes voice notes by invoking the `document_extract` tool, which the
  registry runs as a subprocess with a timeout and a guaranteed kill, rather than
  loading a Whisper runtime in-process.
- **Namespace your loggers** (`logging.getLogger("myplugin.…")`). The journal is
  shared now: `journalctl -u myagent -f | grep myplugin` is how anyone finds your
  lines.
- **Never let a secret reach a user-facing string or a log line.** If your
  transport builds URLs containing credentials, remember that HTTP client errors
  quote the failing URL — and that `httpx` logs every request line at INFO.

## Failures are contained by the loader

Discovery, import, `register`, `startup` and `shutdown` are each guarded: a
failure is logged as a warning and the server starts anyway. Losing an optional
service is bad; an agent runtime that refuses to boot is worse.

A plugin that failed to load is **reported, not hidden**: `GET /api/plugins`
returns it with `loaded: false` and the error, so the UI can say "installed but
broken" instead of "not installed".

```console
$ curl -s localhost:8888/api/plugins
{"plugins":[{"id":"connectors","loaded":true,"error":""}]}
```

The frontend uses that endpoint to decide whether to show a plugin's menu entry:
mark it `class="d-none" data-plugin="<id>"` in `ui/index.html` and
`App.pluginsReady()` reveals it. A plugin's routes simply do not exist when it
isn't loaded, so they answer 404 — the core does not shim them, since knowing
which prefixes to shim would mean naming a specific plugin.

## Installing

One code path installs a plugin, `server/app/plugin_installer.py`, reached three
ways:

| From | How |
|---|---|
| Settings → Server → Plugins | `POST /api/plugins/install` with `{"id"}` (a catalog entry) or `{"repo","ref","subdir"}` (any GitHub repository) |
| a git checkout | `bash connectors/install.sh` (also run by `update.sh`) |
| a terminal | `cd server && .venv/bin/python -m app.plugin_installer install --repo owner/name [--ref main] [--subdir path]` (or `--path dir`) |

A source is a **GitHub repository + ref + subdirectory**, downloaded as a
tarball over HTTPS: no git and no checkout are needed, so it works for a
service-account install too. The subdirectory is what lets one repository hold
several packages. What the installer does, in order:

1. extracts only regular files and directories under the subdirectory (links
   and `..` members are dropped), and records the commit the archive was cut
   from in `<plugin>/.installed.json`;
2. validates the manifest: safe `id`, the id the caller asked for, a `plugin.py`,
   a `requires_api` this core provides;
3. installs every `requirements.txt` into the server's virtualenv, never with
   `--upgrade`, so core dependencies do not move underneath a working setup — a
   failure here is a warning and that part of the plugin degrades;
4. verifies the core still imports, and only then swaps the new tree in, keeping
   the previous one for `POST /api/plugins/{id}/rollback`.

Plugins are loaded at startup, so a change takes effect at the next restart. The
install does not restart anything: `GET /api/plugins` reports `restart_needed`
and the page offers **Restart now** (`POST /api/system/restart`), which stops
gracefully and re-executes the server in place — same pid, same unit — so it
works in every install mode. It refuses while answers are being generated
unless forced.

Uninstalling (`DELETE /api/plugins/{id}`, or `rm -rf ~/myagent/plugins/<id>`
plus a restart) removes the code only. Say in your README what state is left
behind.

### The catalog

`server/plugins-index.json` lists the installable packages. The server reads it
from GitHub (`MYAGENT_PLUGIN_INDEX` overrides the URL) and falls back to the
copy shipped with the install when offline.

```json
{"packages": [{
  "id": "connectors", "kind": "plugin",
  "name": {"en": "Channels", "it": "Canali"}, "description": {"en": "…"},
  "version": "1.0.0",
  "repo": "speleoalex/myagent", "ref": "main", "subdir": "connectors/plugin"
}]}
```

`version` must match the manifest at that location: it is what the UI compares
with the installed one to offer an update (`tests/test_plugin_installer.py`
checks every entry pointing into this repository). `kind` is `plugin`, or a kind
a plugin defines for its own extensions — the connectors plugin uses `channel`
(see `connectors/README.md`). Publishing a plugin kept in another repository is
adding an entry here; nothing else in the core changes.

### Trust

A plugin is code running inside the server, with every API key, bot token and
conversation in reach. The installer protects the install step — HTTPS only,
validated ids, nothing written outside the target — and cannot make an untrusted
plugin safe. So: installing requires the API key whenever the server listens
beyond loopback, the UI asks for confirmation naming the repository for anything
outside the catalog, the installed commit is recorded, and no agent tool exposes
installation.
