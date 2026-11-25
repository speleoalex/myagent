#!/usr/bin/env python3
"""One agent, exported as a file and imported on another instance.

"cerca-lavoro" on the Orin is four things in four places: its JSON, two user
tools (job_search, send_email), a daily task and the tools' settings. Its user
tools were versioned nowhere, and moving it meant remembering all four.

Pinned here, through the real agents router on two separate MYAGENT_HOMEs
(the second one is "the other machine"; no model, no network):

  * the package carries the agent, its tasks without run state, the user-layer
    tools it uses (grouped or not, executable bit kept, __pycache__ left out)
    and their settings WITHOUT secrets; MCP tools are only named;
  * import installs all of it with live OFF, and reports what is still missing
    there (the model, MCP tools, secrets to fill in);
  * a second import conflicts (409) and writes nothing; overwrite replaces,
    dropping files the new version no longer has;
  * settings already configured on the target are not overwritten;
  * a package with a link, an absolute or a ".." path is refused before
    anything is written.

Run:  server/.venv/bin/python tests/test_agent_package.py
"""

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(tempfile.mkdtemp(prefix="myagent-pkg-src-"))
TARGET = Path(tempfile.mkdtemp(prefix="myagent-pkg-dst-"))

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# Each side runs in its own process: app.config binds MYAGENT_HOME at import,
# so two homes cannot share one interpreter. The child prints JSON results.
CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["ROOT"] + "/server")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import config
from app.engine.executor import Stores
from app.routers import agents as agents_router
from app.storage.store import JsonStore
from app.storage.tasks import TaskStore
from app.tools.registry import ToolRegistry

home = Path(os.environ["MYAGENT_HOME"])
app = FastAPI()
app.state.stores = Stores(agents=JsonStore(config.CONFIG_DIR / "agents"),
                          models=JsonStore(config.CONFIG_DIR / "models"))
app.state.tasks = TaskStore(config.CONFIG_DIR / "tasks")
app.state.tool_registry = ToolRegistry(config.TOOLS_DIR,
    bundled_dir=Path(os.environ["ROOT"]) / "server" / "tools",
    tool_env=config.tool_env(), state_dir=config.STATE_DIR)
app.include_router(agents_router.router, prefix="/api/agents")
client = TestClient(app)
exec(sys.stdin.read())
'''


def side(home: Path, code: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", CHILD], input=code, capture_output=True, text=True,
        env={**os.environ, "MYAGENT_HOME": str(home), "ROOT": str(_ROOT)})
    if out.returncode != 0:
        print(out.stderr[-2000:])
        raise SystemExit("child failed")
    return json.loads(out.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ the source side
tools = SOURCE / "tools"
(tools / "lavoro" / "job_search").mkdir(parents=True)
(tools / "lavoro" / "job_search" / "tool.json").write_text(json.dumps({
    "name": "Job search", "description": "x", "parameters": {"type": "object"},
    "config": [{"key": "api_url", "label": "API"},
               {"key": "api_token", "label": "Token", "secret": True}]}))
(tools / "lavoro" / "job_search" / "run").write_text("#!/bin/sh\necho ok\n")
(tools / "lavoro" / "job_search" / "run").chmod(0o755)
(tools / "lavoro" / "job_search" / "__pycache__").mkdir()
(tools / "lavoro" / "job_search" / "__pycache__" / "x.pyc").write_bytes(b"junk")
(tools / "send_email").mkdir()
(tools / "send_email" / "tool.json").write_text(json.dumps(
    {"name": "Send email", "description": "x", "parameters": {"type": "object"}}))
(tools / "send_email" / "run").write_text("#!/bin/sh\necho sent\n")
(tools / "send_email" / "run").chmod(0o755)

exported = side(SOURCE, r'''
from app.models import Agent, Task
from app.tools import tool_config
reg = app.state.tool_registry
app.state.stores.agents.save("cerca-lavoro", Agent(
    id="cerca-lavoro", name="Cerca lavoro", model_id="deepseek-cloud", live=True,
    tools=["lavoro/*", "send_email", "file_read", "mcp_catasto_query"]).model_dump())
app.state.tasks.save(Task(id="task-cv", agent_id="cerca-lavoro", cron="30 7 * * *",
                          prompt="Ricerca giornaliera"))
app.state.tasks.advance("task-cv", "acted", "fatto")
tool_config.save("job_search", reg.get_definition("job_search"),
                 {"api_url": "https://jobs.example/api", "api_token": "SECRET-123"})
r = client.get("/api/agents/cerca-lavoro/export")
Path(os.environ["MYAGENT_HOME"], "pkg.tar.gz").write_bytes(r.content)
print(json.dumps({"status": r.status_code, "type": r.headers["content-type"],
                  "missing": client.get("/api/agents/nope/export").status_code}))
''')
blob = (SOURCE / "pkg.tar.gz").read_bytes()
with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
    names = {m.name: m for m in tar}
    manifest = json.load(tar.extractfile("manifest.json"))
    task = json.load(tar.extractfile("tasks/task-cv.json"))
    settings = json.load(tar.extractfile("tool_settings/job_search.json"))

print("export")
check(exported["status"] == 200 and exported["type"] == "application/gzip",
      "GET /export answers a .tar.gz")
check(exported["missing"] == 404, "an unknown agent is a 404")
check({"agent.json", "tasks/task-cv.json", "tools/lavoro/job_search/run",
       "tools/lavoro/job_search/tool.json", "tools/send_email/run"} <= set(names),
      "agent, task and both user tools (one inside its group)")
check(not any("__pycache__" in n for n in names), "__pycache__ is left out")
check(not any(n.startswith("tools/file_read") for n in names),
      "a native tool nobody edited is not carried")
check(names["tools/lavoro/job_search/run"].mode & 0o100, "the executable bit is kept")
check("last_result" not in task and "next_at" not in task, "tasks carry no run state")
check(settings == {"api_url": "https://jobs.example/api"}, "settings travel, the secret does not")
check(b"SECRET-123" not in blob, "the secret is nowhere in the package")
check(manifest["external_tools"] == ["mcp_catasto_query"], "MCP tools are named, not carried")

# ------------------------------------------------------------ the target side
print("import")
res = side(TARGET, r'''
from app.tools import tool_config
blob = Path(os.environ["BLOB"]).read_bytes()
first = client.post("/api/agents/import", content=blob)
again = client.post("/api/agents/import", content=blob)
agent = app.state.stores.agents.get("cerca-lavoro")
run = Path(os.environ["MYAGENT_HOME"], "tools/lavoro/job_search/run")
reg = app.state.tool_registry
print(json.dumps({
    "first": first.status_code, "body": first.json(),
    "again": again.status_code, "again_detail": again.json().get("detail", ""),
    "live": agent["live"], "exec": os.access(run, os.X_OK),
    "task": app.state.tasks.get("task-cv"),
    "settings": tool_config.values("job_search", reg.get_definition("job_search")),
}))
'''.replace('os.environ["BLOB"]', repr(str(SOURCE / "pkg.tar.gz"))))
body = res["body"]
check(res["first"] == 200 and body["agent_id"] == "cerca-lavoro", "the package is installed")
check(res["live"] is False, "the agent arrives switched OFF")
check(res["exec"], "the tool's run script is executable again")
check(res["task"]["next_at"] != "" and res["task"]["last_result"] == "",
      "the task is scheduled afresh, with no inherited outcome")
check(sorted(body["tools"]) == ["job_search", "send_email"], "both tools installed")
check(body["missing_model"] == "deepseek-cloud", "the missing model is reported")
check(body["missing_settings"] == {"job_search": ["api_token"]},
      "the secret to fill in is reported")
check(body["external_tools"] == ["mcp_catasto_query"], "the MCP tool is reported")
check(res["settings"]["api_url"] == "https://jobs.example/api", "settings arrive")
check(res["again"] == 409 and {"agent cerca-lavoro", "task task-cv", "tool job_search"}
      <= set(res["again_detail"]["conflicts"]),
      "a second import conflicts and lists everything that collides")

print("overwrite")
(TARGET / "tools" / "lavoro" / "job_search" / "stale.txt").write_text("old")
res = side(TARGET, r'''
from app.tools import tool_config
reg = app.state.tool_registry
tool_config.save("job_search", reg.get_definition("job_search"), {"api_url": "https://local/api"})
r = client.post("/api/agents/import?overwrite=true", content=Path(BLOB).read_bytes())
print(json.dumps({"status": r.status_code,
    "stale": Path(os.environ["MYAGENT_HOME"], "tools/lavoro/job_search/stale.txt").exists(),
    "api_url": tool_config.values("job_search", reg.get_definition("job_search"))["api_url"]}))
'''.replace("BLOB", repr(str(SOURCE / "pkg.tar.gz"))))
check(res["status"] == 200, "overwrite=true replaces")
check(not res["stale"], "a file the new version does not have is gone")
check(res["api_url"] == "https://local/api", "a setting configured here is kept")


# ------------------------------------------------------------ hostile packages
def evil(add) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("manifest.json", b'{"format": 1}'),
                           ("agent.json", b'{"id": "x", "name": "x", "model_id": ""}')):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        add(tar)
    return buf.getvalue()


def with_link(tar):
    info = tarfile.TarInfo("tools/x/tool.json")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    tar.addfile(info)


def with_path(name):
    def add(tar):
        info = tarfile.TarInfo(name)
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    return add


print("hostile packages")
cases = {"link": evil(with_link), "dotdot": evil(with_path("tools/../../escape.json")),
         "absolute": evil(with_path("/tmp/escape.json")), "garbage": b"not a tarball"}
for label, data in cases.items():
    (TARGET / f"{label}.bin").write_bytes(data)
res = side(TARGET, r'''
out = {}
for label in ("link", "dotdot", "absolute", "garbage"):
    r = client.post("/api/agents/import", content=Path(os.environ["MYAGENT_HOME"], label + ".bin").read_bytes())
    out[label] = r.status_code
out["agent_x"] = app.state.stores.agents.get("x") is not None
print(json.dumps(out))
''')
check(all(res[k] == 400 for k in cases), f"each is refused with 400: {res}")
check(res["agent_x"] is False, "…and nothing was written")
check(not (TARGET.parent / "escape.json").exists() and not Path("/tmp/escape.json").exists(),
      "no file escaped the target")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
