# MyAgent Connectors

Standalone bridge between messaging services (Telegram today, others tomorrow)
and a running **MyAgent** instance. It runs as its **own server**, independent
of the myagent sources: it talks to myagent only over its HTTP API.

```text
┌──────────────────────────┐    HTTP    ┌───────────────────────┐
│  myagent-connectors      │ ─────────► │  myagent (:8888)      │
│  (this server, :8899)    │  /api/chat │  agent runtime        │
│  • Telegram long polling │ ◄───────── │  + channel sessions   │
│  • access control        │   reply    │                       │
│  • admin UI              │            └───────────────────────┘
└──────────────────────────┘
```

Each **binding** links one bot ↔ one agent, configured from the admin UI:
*"messages on THIS bot are answered by THIS agent, for THESE users only."*

## What it does

- **Long polling** (`getUpdates`) — no public URL, HTTPS, or webhook needed.
  Ideal for a bot driven from a local PC.
- **Per-chat conversations** — every Telegram chat maps to its own persistent
  session on myagent (`session_id = "<prefix>_<chat_id>"`), so context is kept
  per user without polluting the web UI's chat history. Sessions use the same
  format as web chats (tagged with `source: "telegram"`); on `/reset` the
  closed conversation is archived into the web UI's history, marked with a
  channel badge.
- **Access control** per binding: `allowlist` (Telegram user ids),
  `password` (`/start <password>` unlocks), or `open`.
- **Built-in commands**: `/start`, `/help`, `/reset` (clears the conversation).
- **Admin UI** at `/` — add/edit/delete bots, pick the agent, test the token,
  see live status. Bot tokens are stored `0600` and never shown in clear.
  Multilingual (English / Italian), following the myagent UI i18n pattern
  (`ui/js/i18n.js` + `ui/js/i18n/{en,it}.js`); language switcher in the header,
  choice persisted in `localStorage`.
- **Address book** — save people once (name + numeric id and/or @username,
  `/api/contacts`, stored in `contacts/` next to bindings); the binding form's
  authorized-users field shows them as one-click chips, so allowlists are
  built by name instead of pasting ids. The text field stays authoritative —
  chips only add/remove their contact's id in it.

## Requirements

- A running myagent server (default `http://localhost:8888`).
- Python 3.12+.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).

## Run (development)

```bash
cd connectors
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server/main.py
# Admin UI:  http://127.0.0.1:8899
```

Point it at a non-default myagent with an env var:

```bash
MYAGENT_API_URL=http://192.168.1.10:8888 python server/main.py
```

### Configuration (environment variables)

| Variable                        | Default                 | Meaning                               |
|---------------------------------|-------------------------|---------------------------------------|
| `MYAGENT_API_URL`               | `http://localhost:8888` | myagent base URL                      |
| `MYAGENT_API_TOKEN`             | *(empty)*               | Bearer token — set it to the myagent server's `MYAGENT_API_KEY` if that is configured |
| `MYAGENT_CONNECTORS_HOST`       | `127.0.0.1`             | bind host                             |
| `MYAGENT_CONNECTORS_PORT`       | `8899`                  | bind port                             |
| `MYAGENT_CONNECTORS_DIR`        | `~/myagent/connectors`  | bindings, grants and contacts storage |
| `MYAGENT_CONNECTORS_API_KEY`    | *(empty = open)*        | Bearer key for the `/send` endpoint   |
| `MYAGENT_TELEGRAM_POLL_TIMEOUT` | `30`                    | long-poll seconds                     |
| `MYAGENT_CHAT_TIMEOUT`          | `180`                   | max seconds per agent turn            |

## Add a Telegram bot

1. Create a bot with @BotFather and copy the token.
2. Open the admin UI → **+ New bot**.
3. Fill: ID, agent, paste the token → **Test** to validate.
4. Access control: `allowlist` and add your Telegram user id
   (message [@userinfobot](https://t.me/userinfobot) to find it), or use
   `password`.
5. Tick **Enabled** → **Save**. The bot starts polling immediately (hot,
   no restart). Write to your bot on Telegram.

> ⚠️ Agents can run tools (`shell_exec`, `file_write`, …). For a bot reachable
> by others, bind it to an agent scoped to safe tools, and keep access on
> `allowlist` / `password`.

## myagent dependency

This bridge relies on **channel-scoped sessions** added to myagent:

- `POST /api/chat` accepts an optional `session_id` (a named, persistent
  conversation, isolated from the web UI's `current.json`).
- `GET/DELETE /api/chat/sessions/{session_id}` inspect / reset a conversation.

Those live in the myagent repo (`server/app/storage/channel_sessions.py`,
`server/app/routers/chat.py`). No other myagent change is required.

## Outbound push (autonomous agents)

`POST /api/bindings/{binding_id}/send` with `{"chat_id": ..., "text": ...}`
sends an unsolicited message through a **running** binding (409 otherwise).
It is what myagent's `notify_user` tool calls when a live agent wants to reach
the user. The endpoint is best-effort (the connector logs and swallows
transport errors; Telegram messages are chunked at 4096 chars) and is the only
authenticated route: set `MYAGENT_CONNECTORS_API_KEY` here and the same value
as "Connectors send key" in myagent's Settings. Empty key = open, fine on
localhost.

## Adding another channel (Slack, Discord, …)

1. Implement a `BaseConnector` subclass in `app/channels/<name>.py`
   (`start`, `stop`, `send`; the shared inbound pipeline is in `base.py`).
2. Register it in `app/channels/registry.py` under its `type` string.

Nothing else changes — the manager, storage, API and UI are channel-agnostic.

## Production

Same conventions as myagent's `deploy.sh` / `deploy-macos.sh`.

### Linux (systemd)

Installs to `/opt/applications/myagent-connectors` and registers a system
service running as the invoking user:

```bash
sudo bash deploy_connectors.sh
# point at a non-default myagent (env is baked into the unit):
MYAGENT_API_URL=http://192.168.1.10:8888 sudo -E bash deploy_connectors.sh
```

```bash
systemctl status myagent-connectors
journalctl -u myagent-connectors -f
```

Configurable env (override when invoking): `MYAGENT_API_URL`,
`MYAGENT_API_TOKEN`, `MYAGENT_CONNECTORS_HOST` (default `127.0.0.1`),
`MYAGENT_CONNECTORS_PORT` (default `8899`).

### macOS (launchd, no sudo)

Runs in place and registers a per-user LaunchAgent:

```bash
bash deploy_connectors-macos.sh
tail -f ~/Library/Logs/myagent-connectors.log
```

### Rootless alternative (systemd user unit)

For an install without `sudo`/`/opt`, `deploy/myagent-connectors.service` is a
sample **user** unit (`systemctl --user`) you can adapt — see the comments in
that file.

> The admin UI binds to `127.0.0.1` by default (local only). Expose it beyond
> localhost only behind an authenticated reverse proxy — it manages bot tokens.
