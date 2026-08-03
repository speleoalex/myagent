# MCP servers

Besides its own [folder-based tools](TOOLS.md), MyAgent speaks the
[Model Context Protocol](https://modelcontextprotocol.io): add a server under
**Tools → MCP servers** and its tools show up in the agent editor like any
other tool.

Two transports are supported — **stdio**, where the server runs as a local
child process, and **HTTP** (Streamable HTTP) for remote or LAN servers.

```text
Command:    /usr/bin/npx
Arguments:  -y
            @modelcontextprotocol/server-filesystem
            /home/me/documents
```

## Adding one

**Test connection** probes the server with the values in the form — saved or
not — and lists the tools it found, with the exact name each one will have for
the model (`mcp_<server>_<tool>`).

**Import JSON** takes an `mcpServers` block straight from a Claude Desktop /
VS Code / Cursor configuration, reporting per entry what it created and what it
skipped. An entry using the deprecated `sse` transport is skipped rather than
imported as something that would only fail later.

## Granting them to an agent

In the agent editor each server appears as a group with an *all tools from this
server* entry: pick that and tools added on the server later are picked up
automatically, or select them one by one.

**Selecting fewer is often better.** Every tool description ends up in the
model's prompt, which matters a lot with a small local model — a server that
exposes forty tools can cost more context than the conversation. Each server
also has *allowed / excluded tools* fields for the same reason.

## Lifecycle

A server is connected when it is saved or edited (so its tools show up in the
agent editor right away) and otherwise only when an agent that uses it runs a
turn. All of them are shut down with MyAgent.

If a server becomes unreachable the agent keeps working with its other tools —
the failure is reported to the model as a tool error and shown in the servers
list — and its previously discovered tools stay on offer until a refresh
succeeds. That is deliberate: an agent's tool list rebuilt from a temporarily
dead server would silently lose those grants.

## Notes and limits

- `npx` needs `-y`, otherwise it waits for a confirmation nobody can give.
  Under systemd/launchd prefer an absolute path (`/usr/bin/npx`): the service's
  `PATH` is minimal.
- The first connection to an `npx -y` server can take tens of seconds while it
  downloads. Use *Test connection* first; the turn that triggers it does not
  wait forever, it just runs without that server.
- Authentication is a static bearer token or custom headers; OAuth flows are
  not supported.
- Tokens in `env`/`headers` are stored under `~/myagent/config/mcp/` with
  `0600` permissions and are never sent back to the browser.

## Security

Adding a `stdio` server means **MyAgent runs that command locally**, as the
server user, and its tool descriptions become part of your agents' prompts.
Only add servers you trust — the same rule as
[the rest of the tool system](../README.md#security).
