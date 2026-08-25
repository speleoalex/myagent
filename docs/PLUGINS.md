# Plugins

A plugin extends a running myagent: it can mount API routes, publish services on
`app.state` and run background tasks. It is **not** shipped with the core — it is
installed separately, under the user's home, and discovered at startup.

That separation is the point. myagent standalone works offline and carries no
code for optional online services; adding one is an explicit act, and removing it
is deleting a directory.

The bundled `connectors` plugin (messaging bots) is the reference implementation:
`connectors/plugin/` in this repo, installed by `connectors/install.sh`.

## Layout

```text
~/myagent/plugins/<id>/
├── plugin.py            # entry point (required)
├── requirements.txt     # extra Python deps, installed into server/.venv by your installer
└── myagent_<id>/        # the plugin's own package
```

The directory name is the plugin id — there is no manifest file, because the only
metadata anyone needs is that name. Override the root with `MYAGENT_PLUGINS`.

A directory is **skipped** when its name starts with `.` or `_`, or ends with
`.disabled`. Renaming `connectors` to `connectors.disabled` is therefore the way
to park a plugin without deleting it.

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
connectors plugin exposes `resolve_recipients(name, channel)` and the tool calls
it when given a name instead of an id. Keep such a seam a plain method returning
`(results, error)` — an error the caller can read and correct beats an exception,
because the caller here is a language model.

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

There is no install command in the core. A plugin ships its own script; see
`connectors/install.sh`, which:

1. refuses to run as root (it installs under `$HOME` — the *service* user's,
   so for a service-account install it is run as `sudo -u myagent …`);
2. `rsync`s its code into `~/myagent/plugins/<id>/`;
3. installs its `requirements.txt` into the **running** install's `server/.venv`
   (found via `MYAGENT_INSTALL_DIR`, else the `WorkingDirectory` of the user or
   system unit), never
   with `--upgrade`, so core dependencies do not move underneath a working setup;
4. verifies the core still imports before restarting the service;
5. restarts myagent (systemd or launchd).

Uninstalling is `rm -rf ~/myagent/plugins/<id>` plus a restart. Say so in your
README, and say what state is left behind.
