# Why MyAgent is built this way

Most self-hosted agent platforms assume connectivity and a frontier model.
MyAgent assumes neither, and that assumption is the whole design.

## Small local models are the target, not a compatibility mode

This is the part a feature list can't show. Everything below exists because it
broke with a real 8B model, not because it seemed prudent:

- **Tool calls are parsed from plain model text** when the model has no native
  function calling — so a model without tool support is still an agent, not a
  chatbot.
- **A reply that *tried* to be a tool call and didn't parse** is handed back for
  a retry instead of becoming the answer. One stray quote used to end the turn
  with JSON as the user-visible reply.
- **Identical consecutive calls are dropped**, so a small model can't loop.
- **Textual tool documentation is injected only when the model has no native
  tool support.** Serving both protocols at once makes models write JSON as
  prose — we watched an agent answer `{"error": ...}` instead of calling the
  tool.
- **Deciding and answering use separate temperatures**, and the switch happens
  only when no further tool call is possible: after a tool result the model is
  still deciding.
- **The context window is probed from the backend**, not typed in and hoped
  for. llama.cpp reports its per-slot window; Ollama reports what it loaded.
- **An autonomous wake gets no chat history.** A recurring task's history is
  dozens of near-identical copies of itself, and a small model reading its own
  last output will reproduce it — one agent reported the same stale failure for
  three wakes after the problem was fixed.
- **Reasoning is a separate channel.** A thinking model's chain-of-thought
  never reaches the conversation, the next prompt, or a voice device's speaker.

## Tools are files

A tool is a folder with a `tool.json` and an executable `run` in any language,
reading JSON on stdin and writing to stdout. You version it with git, edit it
with your editor, and it hot-reloads — no admin UI, no database row, no build
step. The AI writes new ones in exactly that format, so there is nothing to
register and nothing to migrate.

See [TOOLS.md](TOOLS.md) for the contract.

## Offline is the default path, not a degraded mode

The bundled Master routes general-knowledge questions to the Librarian *before*
the Web Researcher, and the web tools are quarantined in one agent you can
delete. Nothing in the core path needs a socket to the internet; the online
extras are optional and marked as such.

The same principle decides smaller things: the library downloader is a separate
top-level folder you can delete, and `install.sh` draws the line at *content* —
it offers to install a missing package, but the gigabytes of archives are only
ever a command it prints, onto a disk only you can choose.

## Deliberately not here

- **No visual workflow builder.** An agent is a model, a prompt and a list of
  tools. If you want to draw a graph, [Dify](https://dify.ai),
  [Flowise](https://flowiseai.com) and [n8n](https://n8n.io) do it well.
- **Semantic search is optional, and off by default.** The library is searched
  with the ZIM full-text index and a keyword scorer; choosing an embedding
  model in Settings adds vector search on top of that, over your own documents
  only (an encyclopedia already has a full-text index — there is no reason to
  embed millions of articles). The original reason for leaving it out has not
  gone away, which is why it stays opt-in: on a disconnected box a second
  resident model costs exactly the VRAM your chat model needs, and while the
  index builds it competes with that model for the same backend. The embedding
  model must be a **local** one — indexing sends the contents of your documents
  to the endpoint, not just your question.
- **No multi-user accounts or RBAC.** One trusted user — see
  [SECURITY.md](SECURITY.md).
- **No sandbox.** Tools run as the server user. That's what makes "a folder
  with a `run` script" powerful, and it's why the API binds to `127.0.0.1`.
- **Nothing to compile.** Python standard library + FastAPI on the backend,
  vanilla JS + Bootstrap on the frontend, four runtime dependencies.
