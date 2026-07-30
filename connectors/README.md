# MyAgent Connectors

Bridge between messaging services (Telegram today, others tomorrow) and your
agents. It is an **optional plugin**: not part of the core install, because the
core works offline and this is the part that needs the internet.

Installed, it runs **inside** myagent's process — one service, one port, one UI.

```text
┌────────────────────────────────────────────┐         ┌──────────────────┐
│  myagent (:8888)                           │  HTTPS  │ api.telegram.org │
│  ┌──────────────────────────────────────┐  │ ──────► │                  │
│  │ connectors plugin                    │  │ ◄────── │                  │
│  │  • one long-poll task per bot        │  │         └──────────────────┘
│  │  • access control + address book     │  │
│  └──────────────────────────────────────┘  │
│  agent runtime · channel sessions · UI     │
└────────────────────────────────────────────┘
```

Each **binding** links one bot ↔ one agent: *"messages on THIS bot are answered
by THIS agent, for THESE users only."* Configure them at
`http://127.0.0.1:8888/#/connectors`.

## What it does

- **Long polling** (`getUpdates`) — no public URL, HTTPS or webhook needed.
  Ideal for a bot driven from a local PC.
- **Per-chat conversations** — every Telegram chat maps to its own persistent
  session (`session_id = "<prefix>_<chat_id>"`), so context is kept per user
  without polluting the web UI's chat history. Sessions use the same format as
  web chats (tagged `source: "telegram"`); on `/reset` the closed conversation is
  archived into the web history with a channel badge.
- **Access control** per binding: `allowlist` (user ids and/or @usernames),
  `password` (`/start <password>` unlocks, grants persist), or `open`.
- **Built-in commands**: `/start`, `/help`, `/reset`.
- **Attachments** — photos, text files, PDFs and audio are forwarded to the
  agent; **voice notes are transcribed** to text first (see below).
- **Address book** — save people once (name + numeric id and/or @username); the
  binding form offers them as one-click chips, so allowlists are built by name
  instead of pasting ids. The text field stays authoritative, so you can also
  authorize someone who is not in the address book.
- **Outbound push** — the `notify_user` tool lets an agent (typically an
  autonomous one) start a conversation. It both delivers the message and appends
  it to that chat's own history, so the agent remembers having said it.

## Requirements

- An installed myagent (`./deploy.sh`, or `./setup.sh` for a dev checkout).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Optional, for voice notes: `ffmpeg` on the PATH.

## Install

```bash
bash connectors/install.sh          # from the git checkout
```

It copies the plugin to `~/myagent/plugins/connectors/`, installs its one extra
Python dependency into myagent's virtualenv, and restarts the service. If the old
standalone `myagent-connectors` service is still running it refuses to proceed —
two pollers on one bot token means Telegram delivers each message to only one of
them, at random, with no error on either side.

Then, at `http://127.0.0.1:8888/#/connectors`: **New bot** → paste the token →
**Test token** → pick the agent → list the authorized user ids → Save. The bot
starts polling immediately, no restart needed. To find your numeric id, message
[@userinfobot](https://t.me/userinfobot).

Logs: `journalctl -u myagent -f | grep connectors`

## Uninstall

```bash
rm -rf ~/myagent/plugins/connectors
sudo systemctl restart myagent
```

**Your bots and tokens stay** in `~/myagent/connectors/` — reinstalling brings
them back. To reclaim the transcription dependency too (~380 MB):

```bash
<install>/server/.venv/bin/pip uninstall -y faster-whisper ctranslate2 onnxruntime
```

## Voice notes: size and first run

Transcription runs through the bundled `document_extract` tool, which the tool
registry executes as a **subprocess** with a timeout. That matters: the Whisper
runtime is a heavy native library, and a crash inside it would otherwise take
down every chat in the agent's process.

- `install.sh` adds `faster-whisper` to myagent's virtualenv: **~380 MB**.
- The **first** voice note downloads a model into `~/.cache/huggingface`:
  **~464 MB** for the default `small`. Set `MYAGENT_WHISPER_MODEL=base` in the
  service environment to use a ~2.6 MB one instead.
- Without `ffmpeg` the bot answers that transcription is unavailable; everything
  else keeps working.

## State and configuration

State lives in `~/myagent/connectors/` (override with `MYAGENT_CONNECTORS_DIR`):
`bindings/` (bot definitions, **0600**, they hold the tokens), `grants/`
(password-mode authorized ids), `contacts/` (address book), `state.json` (the
kill switch). It is separate from the plugin's code on purpose, so reinstalling
or removing the plugin never touches your bots — back this directory up together
with `~/myagent/config`.

| Variable | Default | What |
|---|---|---|
| `MYAGENT_CONNECTORS_DIR` | `~/myagent/connectors` | state directory |
| `MYAGENT_TELEGRAM_POLL_TIMEOUT` | `30` | long-poll seconds |
| `MYAGENT_CHAT_TIMEOUT` | `180` | wall clock for one agent turn |
| `MYAGENT_CONNECTORS_CONCURRENCY` | `2` | inbound turns running at once, all bots |
| `MYAGENT_CONNECTORS_MAX_ERRORS` | `10` | consecutive failures before self-pausing |
| `MYAGENT_WHISPER_MODEL` | `small` | Whisper model for voice notes |

There is no host, port, API url or API token: the plugin does not talk to myagent
over the network any more. The whole API — bot tokens included — sits behind
myagent's own `MYAGENT_API_KEY` gate, which is stricter than the standalone
server was (there, only the outbound send endpoint was authenticated).

## When a bot misbehaves

- **Status per bot** is in the list at `#/connectors`: `running`, `starting`,
  `error` (with the reason), `paused`, or `disabled`.
- **A bot that keeps failing pauses itself** after `MYAGENT_CONNECTORS_MAX_ERRORS`
  consecutive errors instead of retrying forever. Fix the cause, then
  `POST /api/connectors/bindings/<id>/resume` — or just save the binding again,
  which also clears the pause.
- **Stop everything**: `POST /api/connectors/stop` stops every bot and
  **remembers it across restarts**; `POST /api/connectors/start` re-enables.
  Deliberately not automatic — this is what you reach for when the plugin is
  causing damage.
- Operate bots **hot** (enable/disable, edit, resume). Restarting myagent to fix
  one bot also kills in-flight web turns and any MCP subprocesses.

## Adding another channel

Implement a `BaseConnector` subclass in
`plugin/myagent_connectors/channels/<name>.py` (transport only: receive, send)
and register it in `channels/registry.py`. Access control, commands, session
keys and the agent call are already shared. The plugin contract itself is
documented in [../docs/PLUGINS.md](../docs/PLUGINS.md).

## Security note

Bindings hold bot tokens. Keep myagent bound to `127.0.0.1` (the default) or set
`MYAGENT_API_KEY` before exposing it on a network.
