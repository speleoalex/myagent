# Writing tools

A tool is a **folder** inside `~/myagent/tools/` containing two files:

```text
~/myagent/tools/my_tool/
├── tool.json   # metadata + parameter schema
└── run         # executable script, any language (chmod +x)
```

Tools are **hot-reloaded**: the registry re-scans the folders (with mtime
caching) on every access, so adding or editing a tool needs no restart.

## Native tools and your copies (the overlay)

Tools come from two layers and there is **no install step**:

- the **native catalog** shipped with the app (`server/tools/` in the repo) is
  read-only and always visible — every native tool is usable out of the box;
- `~/myagent/tools/` is your layer: the tools you (or the AI) create, plus
  **local copies of native tools you edited**.

Saving a change to a native tool copies its folder into `~/myagent/tools/`
first (copy-on-write) and writes there; the shipped original is never
modified. From then on your copy wins, and the tool is flagged *modified* in
the UI. **Reset** simply deletes your copy, so the shipped version shows
through again — nothing is re-downloaded or re-imported. Because your layer
lives outside the install dir, your tools and edits survive a redeploy, while
untouched native tools automatically follow the app when it is upgraded.

Native tools cannot be deleted (they belong to the app): reset discards local
changes, and setting `enabled: false` hides a tool from the agents.

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

## Tool groups (categories)

Related tools can share a **group folder**: a subfolder of the tools dir
*without* its own `tool.json`, whose subfolders are ordinary tool folders
(one level deep). The bundled `file_management/` is the reference example:

```text
~/myagent/tools/
├── file_management/        # group — no tool.json here
│   ├── file_read/          #   tool.json + run, as usual
│   ├── file_write/
│   ├── file_append/
│   └── make_dir/
├── library/                # another group: search the offline library…
│   ├── local_search/       #   …then read one result at length
│   └── local_read/
└── shell_exec/             # ungrouped tool, same as before
```

The group name becomes the tools' `category` (shown as a section in the UI);
the folder layout is the single source of truth — a `category` key written in
`tool.json` is ignored. Tool ids stay global (the leaf folder name), so
moving a tool into a group changes nothing for the agents that reference it,
and the same id cannot exist in two places (the flat copy wins).

An agent can be granted a whole group at once with the `<group>/*` wildcard in
its `tools` list — e.g. `"file_management/*"` — the analogue of the MCP
`mcp:<server>/*` wildcard. It expands at each turn to the group's tools *at
that moment*, so a tool later added to the folder is picked up automatically.
In the agent form the group appears with a master checkbox (the wildcard) or
each tool can be ticked individually.

## The `run` contract

- `run` must be executable (`chmod +x`) and start with a shebang
  (`#!/usr/bin/env python3`, `#!/bin/bash`, `#!/usr/bin/env node`, …). The
  shebang is also what the **language badge** in the Tools list reports: for a
  shell launcher (see [Python dependencies](#python-dependencies)) the exec'd
  script decides, so a Python tool started by a `/bin/sh` wrapper still reads
  "Python".
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
(actually executes the tool with sample arguments), `reset` (drop a local copy
of a native tool) and `delete`.

`save` refuses a script without a shebang, rejects Python syntax errors before
writing, and never touches internal tools; it goes through the same
copy-on-write as the UI, so the AI can improve a native tool without ever
damaging the shipped original. `delete` only removes user-created tools (a
native tool is reset, not deleted) and refuses `manage_tools` itself. After a
`save` the result tells the agent the tool is untested, so the normal loop is
**save → test → fix → test**.

Anything it creates is an ordinary tool folder in `~/myagent/tools/`, editable
by hand or from **Tools** in the UI.

## Python dependencies

The tool subprocess uses whatever interpreter its shebang points at. If a
tool needs packages from the app's venv, launch that venv's Python explicitly
(see `server/tools/library/local_search/run` for the pattern):

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

To make a tool part of the repository, add its folder to `server/tools/`: it
is the native catalog, so the tool is immediately available to every install
after an upgrade — no import, no seeding. Users who edit it get a local copy
and can go back with **Reset** in the UI.
