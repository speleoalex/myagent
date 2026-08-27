#!/usr/bin/env python3
"""Agent.folder — the per-agent working folder for the search tools.

Run: server/.venv/bin/python tests/test_agent_folder.py
(needs the venv; MYAGENT_* dirs are temporary and no network is touched — the
one tool executed is a throwaway `run` script that just dumps its environment.)

The contract, one case each:
  1. no folder -> tool_env_overrides() is empty, so the subprocess environment
     is what it is today, key for key;
  2. a folder -> MYAGENT_AGENT_DIR carries it, and `~` is expanded once, in the
     model, so every reader sees the same absolute path;
  3. the value actually REACHES the subprocess (the registry used to drop
     **extra before _execute_external — that was the whole gap);
  4. MYAGENT_WORKSPACE and the cwd are UNCHANGED. This is the load-bearing one:
     the resource channel (tools/resources.py, routers/files.py) has exactly one
     root, and a second one would make every [[resource:...]] written outside it
     vanish silently;
  5. both library tools resolve the same default root from that variable — an id
     minted by local_search must open in local_read (source check against the
     shared helper the sync test already guards).
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine.executor import AgentExecutor, Stores       # noqa: E402
from app.tools.registry import ToolRegistry                 # noqa: E402
from app.storage.store import JsonStore                     # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)

workspace = base / "workspace"
tools_dir = base / "tools"
registry = ToolRegistry(tools_dir, workdir=workspace)

# A tool that reports the environment it was actually launched with.
probe = tools_dir / "envprobe"
probe.mkdir(parents=True)
(probe / "tool.json").write_text(
    '{"name": "envprobe", "description": "d", "parameters": {"type": "object", '
    '"properties": {}}}')
run = probe / "run"
run.write_text(
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'agent_dir': os.environ.get('MYAGENT_AGENT_DIR'),\n"
    "                  'workspace': os.environ.get('MYAGENT_WORKSPACE'),\n"
    "                  'cwd': os.getcwd()}))\n")
run.chmod(0o755)

models.save("m", {"id": "m", "name": "M", "provider": "llamacpp", "model": "m",
                  "base_url": "http://localhost:8080"})
agents.save("plain", {"id": "plain", "name": "P", "model_id": "m",
                      "system_prompt": "x", "tools": ["envprobe"]})
agents.save("scoped", {"id": "scoped", "name": "S", "model_id": "m",
                       "system_prompt": "x", "tools": ["envprobe"],
                       "folder": {"path": "~/Documents/L300"}})
agents.save("blank", {"id": "blank", "name": "B", "model_id": "m",
                      "system_prompt": "x", "tools": ["envprobe"],
                      "folder": {"path": "   "}})

expanded = os.path.expanduser("~/Documents/L300")
failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


async def main():
    import json

    plain = await AgentExecutor.create_for_agent("plain", registry, stores)
    scoped = await AgentExecutor.create_for_agent("scoped", registry, stores)
    blank = await AgentExecutor.create_for_agent("blank", registry, stores)

    # 1 + 2. What the executor hands the registry.
    check("no folder -> no per-agent environment at all",
          plain.tool_env_overrides() == {})
    check("a blank path is the same as no folder",
          blank.tool_env_overrides() == {})
    check("a folder -> MYAGENT_AGENT_DIR, '~' already expanded",
          scoped.tool_env_overrides() == {"MYAGENT_AGENT_DIR": expanded})

    # 3 + 4. What the subprocess actually sees.
    got = json.loads(await registry.execute("envprobe", {}, executor=scoped))
    check("MYAGENT_AGENT_DIR reaches the tool subprocess",
          got["agent_dir"] == expanded)
    check("MYAGENT_WORKSPACE still points at the workspace",
          got["workspace"] == str(workspace))
    check("the cwd is still the workspace, not the agent's folder",
          Path(got["cwd"]).resolve() == workspace.resolve())

    base_env = json.loads(await registry.execute("envprobe", {}, executor=plain))
    check("an agent with no folder sees no MYAGENT_AGENT_DIR",
          base_env["agent_dir"] is None)
    check("an agent with no folder sees today's workspace and cwd",
          (base_env["workspace"], Path(base_env["cwd"]).resolve())
          == (str(workspace), workspace.resolve()))

    # A tool called with no executor at all (a code path that predates this)
    # must still run.
    no_ex = json.loads(await registry.execute("envprobe", {}))
    check("no executor -> the tool still runs, with no per-agent environment",
          no_ex["agent_dir"] is None)

    # 5. The two library tools must agree on the root, or an id handed over by
    #    local_search opens a DIFFERENT document. tests/test_library_helpers_sync
    #    proves the two copies match; this proves they read the new variable.
    for tool, mod in (("local_search", "search.py"), ("local_read", "read.py")):
        src = (ROOT / "server" / "tools" / "library" / tool / mod).read_text(encoding="utf-8")
        check(f"{tool} defaults its root to MYAGENT_AGENT_DIR",
              'os.environ.get("MYAGENT_AGENT_DIR")' in src)


asyncio.run(main())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — the agent's folder reaches its search tools and nothing else moves:"
      " workspace and cwd are untouched")
