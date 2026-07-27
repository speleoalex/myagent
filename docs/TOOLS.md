# Writing tools

A tool is a **folder** inside `~/myagent/tools/` containing two files:

```text
~/myagent/tools/my_tool/
├── tool.json   # metadata + parameter schema
└── run         # executable script, any language (chmod +x)
```

Tools are **hot-reloaded**: the registry re-scans the folder (with mtime
caching) on every access, so adding or editing a tool needs no restart. The
bundled tools in the repo's `server/tools/` are only the seed template copied
to `~/myagent/tools/` on first run — the runtime folder is what agents
actually use, and it's also where the AI can create its own tools.

## `tool.json`

```json
{
  "name": "Read File",
  "description": "Read the contents of a file and return it as text.",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Absolute or relative file path to read"
      }
    },
    "required": ["path"]
  },
  "timeout": 10,
  "max_output": 10000
}
```

| Field | Default | Meaning |
|---|---|---|
| `name` | — | human-readable name shown in the UI |
| `description` | `""` | what the tool does — this is what the model reads to decide when to call it, so make it precise |
| `parameters` | empty object schema | JSON Schema of the arguments (OpenAI function-calling format) |
| `timeout` | `30` | seconds before the subprocess is killed |
| `max_output` | `10000` | output characters beyond which the result is truncated |
| `enabled` | `true` | disabled tools are hidden from agents |
| `internal` | `false` | `true` = handled by a Python handler in-process, no `run` script (only `call_agent` today) |

The folder name is the tool id — it's what agents list in their `tools` array
and the function name the model calls.

## The `run` contract

- `run` must be executable (`chmod +x`) and start with a shebang
  (`#!/usr/bin/env python3`, `#!/bin/bash`, `#!/usr/bin/env node`, …).
- **Input:** the call arguments arrive as a single JSON object on **stdin**.
- **Output:** print the result (plain text) to **stdout**. That string is
  returned verbatim to the model.
- **Errors:** exit non-zero and write the message to **stderr** — it comes
  back to the model as `ERROR (exit N): <stderr>`. A timeout or a broken
  shebang is also reported in-band as an `ERROR: ...` string, never as a
  crashed chat turn.
- **Working directory:** tools run in the agent workspace
  (`~/myagent/workspace/`), so relative paths written or read by a tool land
  there. Absolute paths work as usual.
- **Environment:** the process inherits the server's environment plus:
  - `MYAGENT_APP_DIR` — the app's `server/` directory (lets a launcher find
    the app venv: `$MYAGENT_APP_DIR/.venv/bin/python`)
  - `MYAGENT_WORKDIR` — the workspace path

## Minimal example

```bash
mkdir ~/myagent/tools/word_count
cd ~/myagent/tools/word_count

cat > tool.json <<'EOF'
{
  "name": "Word Count",
  "description": "Count the words in a text.",
  "parameters": {
    "type": "object",
    "properties": {
      "text": { "type": "string", "description": "Text to count" }
    },
    "required": ["text"]
  },
  "timeout": 5
}
EOF

cat > run <<'EOF'
#!/usr/bin/env python3
import json, sys
args = json.load(sys.stdin)
print(len(args.get("text", "").split()))
EOF

chmod +x run
```

Enable it in an agent's tool list and it's immediately callable — no restart.

## Letting the AI write the tool

The bundled **Tool Manager** agent does all of the above for you: describe the
tool you want and it writes the folder through the `manage_tools` tool, which
covers `list`, `get` (metadata + run script), `save` (create/update), `test`
(actually executes the tool with sample arguments) and `delete`.

`save` refuses a script without a shebang, rejects Python syntax errors before
writing, and never touches internal tools; `delete` refuses `manage_tools`
itself. After a `save` the result tells the agent the tool is untested, so the
normal loop is **save → test → fix → test**.

Anything it creates is an ordinary tool folder in `~/myagent/tools/`, editable
by hand or from **Tools** in the UI.

## Python dependencies

The tool subprocess uses whatever interpreter its shebang points at. If a
tool needs packages from the app's venv, launch that venv's Python explicitly
(see `server/tools/local_search/run` for the pattern):

```sh
#!/bin/sh
PY="${MYAGENT_APP_DIR:+$MYAGENT_APP_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
DIR="$(dirname "$(readlink -f "$0")")"
exec "$PY" "$DIR/main.py"
```

Node tools resolve `node_modules` relative to their own folder (`__dirname`),
not the working directory — see `server/tools/browse_web/`.

## Shipping a tool with the app

To make a tool part of the repository, add its folder to `server/tools/`
too: that template seeds `~/myagent/tools/` on first run, and users can later
import or reset it from **Tools → Native tools** in the UI.
