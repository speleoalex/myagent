# Security

> **MyAgent includes tools that execute shell commands as the server user, and
> there is no sandbox.** That is what makes "a tool is a folder with a `run`
> script" powerful, and it is why the API binds to `127.0.0.1` by default.
> Treat the API as equivalent to a shell on the machine.

## The threat model

One trusted user, on their own machine. There are no accounts, no roles and no
per-agent permissions beyond which tools you grant. Anyone who can reach the
API can:

- run shell commands and read/write files as the server user;
- read your conversations, your library and your address book;
- reconfigure agents, including granting them more tools.

So the only real boundary is **who can reach the port**.

Two of the bundled agents hold `shell_exec` and are reachable by delegation —
**System Administrator** and **Coder** — and Coder is the natural destination
for an everyday "write me a script" request. That does not add a capability the
platform did not already have, but it does make shell execution a routine path
rather than one you pick deliberately. It matters because anything that reaches
Master can try to steer a delegation: a web page a research agent fetched, a
message arriving through a connector, a document you handed it. If that is more
than you want, set `callable: false` on either agent in its **Advanced** tab —
you can still select it yourself in the chat.

## API key

By default the API has no authentication. Before exposing it beyond localhost,
set a key: every `/api` request then requires it, either as an
`Authorization: Bearer <key>` header or as an `?api_key=<key>` query parameter.

The web UI asks for the key on first use — or open it once as
`http://host:8888/?api_key=<key>`, and the key is stored in the browser and
stripped from the URL.

Two ways to set it, and they are **not** interchangeable:

- **Settings → API key** generates, changes or removes it from the UI. Stored
  in `~/myagent/config/api_key` (`0600`), it takes effect on the next request —
  no restart, so in-flight turns survive. The page also shows the `?api_key=`
  link to open on another device.
- **`MYAGENT_API_KEY`** pins it at the deployment level (systemd drop-in,
  container env, read-only install). When set it **wins**, and the Settings box
  turns read-only: the process's own configuration is not something an API call
  gets to overwrite.

A key containing spaces is rejected: it travels both in a header and in a query
parameter, and one of the two would silently eat it.

For anything internet-facing, still prefer an authenticating reverse proxy on
top. An API key is a lock on a door, not a security perimeter.

## Transport

Over plain http the key travels in clear on every request. Either keep the
traffic inside a VPN, or give MyAgent a certificate and let it serve HTTPS
itself — `MYAGENT_SSL_CERTFILE` / `MYAGENT_SSL_KEYFILE`, no reverse proxy
required. See
[INSTALL.md](INSTALL.md#installing-from-another-device), which also covers how
to get a *trusted* certificate for a private address (a self-signed one you
click through is not enough for the browser).

## Exposing it on the network

If you set `MYAGENT_HOST=0.0.0.0`, set an API key in the same change. The two
belong together; the systemd unit written by `deploy.sh` says so in a comment
right above the line.

## MCP servers

Adding a server with the `stdio` transport means **MyAgent runs that command
locally**, as the server user, and its tool descriptions become part of your
agents' prompts — a prompt-injection surface as well as a code-execution one.
Only add servers you trust. See [MCP.md](MCP.md).

## Files served to the browser

`GET /api/files/{path}` serves workspace files read-only (it is how images and
pages a tool delivered to the chat reach the browser). It sits behind the API
key like the rest of `/api/`, and grants nothing the key holder could not
already read through an agent with `file_read`. Every response carries
`Content-Security-Policy: sandbox`, so a generated HTML page opened from there
runs in an **opaque origin**: its scripts work, but it cannot read the UI's
localStorage (where the browser keeps the API key) or make authenticated calls.
The chat opens HTML resources through `viewer.html`, which fetches with the key
in a header and renders in a sandboxed iframe — the key never enters a URL.
Image tags do append `?api_key=` (an image cannot read its own URL); those URLs
are built at render time and never persisted.

## Where the secrets are

| What | Where | Mode |
|---|---|---|
| API key of this server | `~/myagent/config/api_key` | `0600` |
| Model API keys (OpenAI, Anthropic …) | `~/myagent/config/models/*.json` | `0600` |
| MCP tokens and headers | `~/myagent/config/mcp/` | `0600` |
| Bot tokens, device keys | `~/myagent/connectors/bindings/` | `0600` |

All of them are masked by the API and never sent back to the browser — with one
deliberate exception: `GET /api/system/api-key` returns this server's own key in
clear, because the caller just presented it, and masking it would only stop you
from copying it to your phone.

Bot tokens are also kept out of logs: an httpx error message contains the
request URL, which for Telegram contains the token, so those are redacted and
httpx's own request logging is turned down.

## Backups contain everything

`~/myagent/config/` and `~/myagent/connectors/` hold every credential above.
Encrypt the backup, or keep it somewhere as trusted as the machine itself.
