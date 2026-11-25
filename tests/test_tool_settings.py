#!/usr/bin/env python3
"""A tool's settings and its memory between runs have a fixed place.

Before: every user tool invented its own. On the Orin, send_email kept SMTP
credentials in a hand-made config/send_email.json and job_search kept the ads
it had already seen in a seen.json in the workspace, among the user's files.

Pinned here, with the real ToolRegistry running a real `run` script that
reports its environment, and the real tools router (temporary MYAGENT_HOME):

  * the "config" block of tool.json is what the form shows; bad keys dropped;
  * values reach the run as TOOL_<KEY>, defaults included, empty ones absent;
  * a declared key cannot shadow the process environment (PATH stays PATH);
  * secrets are masked on read; the mask sent back keeps the stored value;
    an undeclared key is refused; the file is 0600;
  * every run gets MYAGENT_STATE_DIR = state/<tool_id>, created, and what the
    tool writes there is there on the next run;
  * deleting the tool drops its settings;
  * an MCP-less unknown tool is a 404 on the settings routes.

Run:  server/.venv/bin/python tests/test_tool_settings.py
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-toolsettings-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.routers import tools as tools_router  # noqa: E402
from app.routers.secrets import SECRET_MASK  # noqa: E402
from app.tools import tool_config  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


tools_dir = Path(_TMP) / "tools"
tool = tools_dir / "send_report"
tool.mkdir(parents=True)
(tool / "tool.json").write_text(json.dumps({
    "name": "Send report",
    "description": "test",
    "parameters": {"type": "object", "properties": {}},
    "config": [
        {"key": "smtp_host", "label": "SMTP server", "default": "smtp.example.com"},
        {"key": "smtp_user", "label": "User"},
        {"key": "smtp_password", "label": "Password", "secret": True},
        {"key": "path", "label": "Tries to shadow PATH"},
        {"key": "Bad-Key", "label": "dropped"},
    ],
}))
(tool / "run").write_text(
    "#!/usr/bin/env python3\n"
    "import json, os, pathlib\n"
    "state = pathlib.Path(os.environ['MYAGENT_STATE_DIR'])\n"
    "count = state / 'count'\n"
    "n = int(count.read_text()) + 1 if count.exists() else 1\n"
    "count.write_text(str(n))\n"
    "print(json.dumps({'env': {k: v for k, v in os.environ.items() if k.startswith('TOOL_')},\n"
    "                  'path': os.environ.get('PATH'), 'state': str(state), 'runs': n}))\n")
(tool / "run").chmod(0o755)

registry = ToolRegistry(tools_dir, tool_env=config.tool_env(), state_dir=config.STATE_DIR)
meta = registry.get_definition("send_report")

print("declared fields")
keys = [f["key"] for f in tool_config.fields(meta)]
check(keys == ["smtp_host", "smtp_user", "smtp_password", "path"],
      f"valid keys kept, the malformed one dropped: {keys}")
check(registry.to_openai_format([meta])[0]["function"].keys() == {"name", "description", "parameters"},
      "the model's schema carries none of it")

print("through the API")
app = FastAPI()
app.state.tool_registry = registry
app.include_router(tools_router.router, prefix="/api/tools")
client = TestClient(app)
got = client.get("/api/tools/send_report/config").json()
check(got["values"] == {"smtp_host": "smtp.example.com", "smtp_user": "",
                        "smtp_password": "", "path": ""},
      "unset values: defaults, else empty")
r = client.put("/api/tools/send_report/config",
               json={"smtp_user": "bot@example.com", "smtp_password": "s3cret", "path": "/evil"})
check(r.status_code == 200 and r.json()["values"]["smtp_password"] == SECRET_MASK,
      "a stored secret comes back masked")
stored_file = config.TOOL_CONFIG_DIR / "send_report.json"
check(stat.S_IMODE(stored_file.stat().st_mode) == 0o600, "the settings file is 0600")
client.put("/api/tools/send_report/config",
           json={"smtp_host": "mail.example.org", "smtp_password": SECRET_MASK})
check(json.loads(stored_file.read_text())["smtp_password"] == "s3cret",
      "the mask sent back keeps the stored secret")
r = client.put("/api/tools/send_report/config", json={"smtp_pasword": "typo"})
check(r.status_code == 400 and "smtp_pasword" in r.json()["detail"],
      "an undeclared key is refused, not silently stored")
check(client.get("/api/tools/nope/config").status_code == 404, "unknown tool: 404")

print("what a run sees")
first = json.loads(asyncio.run(registry.execute("send_report", {})))
check(first["env"] == {"TOOL_SMTP_HOST": "mail.example.org",
                       "TOOL_SMTP_USER": "bot@example.com",
                       "TOOL_SMTP_PASSWORD": "s3cret",
                       "TOOL_PATH": "/evil"},
      f"every non-empty setting as TOOL_<KEY>: {first['env']}")
check(first["path"] == os.environ.get("PATH"), "a key named 'path' cannot replace PATH")
check(first["state"] == str(config.STATE_DIR / "send_report"),
      "MYAGENT_STATE_DIR is the tool's own folder under state/")
second = json.loads(asyncio.run(registry.execute("send_report", {})))
check(first["runs"] == 1 and second["runs"] == 2, "what it writes there is kept between runs")

print("delete")
check(client.delete("/api/tools/send_report").status_code == 200, "the tool is deleted")
check(not stored_file.exists(), "…and its settings with it")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
