#!/usr/bin/env python3
"""The tool budget can never be larger than the rounds that could spend it.

`max_iterations` bounds model calls and `max_tool_calls` bounds actions — two
resources — but a round usually carries ONE call, so N rounds reach about N
actions. With the iteration number below the tool number the turn stops at the
smaller one and part of the tool budget is unreachable, silently.

Measured on a production install, 2026-11-27: the agent working on the Orin was
at 10 / 20. It spent ten rounds reading, reached the iteration cap with fifteen
tool calls of twenty, and ended with the report it had composed unwritten — and
could not even say which limit had stopped it.

Pinned here, through the real agents router:

  * creating such an agent is refused, and the message names both numbers;
  * `iterations >= tool calls` is accepted, and so is ANY pair for an agent with
    no tools — there the iteration budget IS the budget;
  * an EDIT that leaves the two numbers alone is accepted even when the stored
    file is incoherent: existing configs must stay editable, which is why this
    is a route check and not a model validator;
  * changing one of the numbers into an incoherent pair is refused;
  * the incoherent agent still LOADS (GET), so a file written by hand or by an
    older version does not take an agent off the air.

Run:  server/.venv/bin/python tests/test_agent_budgets.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp(prefix="myagent-budgets-"))

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


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

app = FastAPI()
app.state.stores = Stores(agents=JsonStore(config.CONFIG_DIR / "agents"),
                          models=JsonStore(config.CONFIG_DIR / "models"))
app.include_router(agents_router.router, prefix="/api/agents")
client = TestClient(app)
exec(sys.stdin.read())
'''


def run(code: str) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", CHILD], input=code, capture_output=True, text=True,
        env={**os.environ, "MYAGENT_HOME": str(HOME), "ROOT": str(_ROOT)})
    if out.returncode != 0:
        print(out.stderr[-2000:])
        raise SystemExit("child failed")
    return json.loads(out.stdout.strip().splitlines()[-1])


def agent(**kw):
    base = {"id": "a", "name": "A", "description": "", "model_id": "",
            "system_prompt": "x", "tools": ["shell_exec"],
            "max_iterations": 10, "max_tool_calls": 5}
    base.update(kw)
    return base


# Two agents written straight to disk, the way an older version or a hand edit
# would leave them: one incoherent, one coherent.
(HOME / "config" / "agents").mkdir(parents=True, exist_ok=True)
(HOME / "config" / "agents" / "legacy.json").write_text(json.dumps(
    agent(id="legacy", max_iterations=10, max_tool_calls=20)))
(HOME / "config" / "agents" / "naked.json").write_text(json.dumps(
    agent(id="naked", tools=[], max_iterations=4, max_tool_calls=20)))

RESULT = run(r'''
r = {}
r["create_incoherent"] = client.post("/api/agents", json=%s).status_code
r["create_incoherent_body"] = client.post("/api/agents", json=%s).json().get("detail", "")
r["create_equal"] = client.post("/api/agents", json=%s).status_code
r["create_no_tools"] = client.post("/api/agents", json=%s).status_code
r["load_legacy"] = client.get("/api/agents/legacy").status_code
r["edit_untouched"] = client.put("/api/agents/legacy", json=%s).status_code
r["edit_raises_calls"] = client.put("/api/agents/legacy", json=%s).status_code
r["edit_fixes"] = client.put("/api/agents/legacy", json=%s).status_code
print(json.dumps(r))
''' % (json.dumps(agent(id="bad", max_iterations=10, max_tool_calls=20)),
       json.dumps(agent(id="bad2", max_iterations=10, max_tool_calls=20)),
       json.dumps(agent(id="ok1", max_iterations=20, max_tool_calls=20)),
       json.dumps(agent(id="ok2", tools=[], max_iterations=4, max_tool_calls=20)),
       json.dumps(agent(id="legacy", max_iterations=10, max_tool_calls=20,
                        system_prompt="changed only this")),
       json.dumps(agent(id="legacy", max_iterations=10, max_tool_calls=25)),
       json.dumps(agent(id="legacy", max_iterations=25, max_tool_calls=25))))

check(RESULT["create_incoherent"] == 400,
      f"an incoherent pair is refused on create: {RESULT['create_incoherent']}")
body = RESULT["create_incoherent_body"]
check("20" in body and "10" in body and "max_iterations" in body,
      f"and the message names both numbers: {body[:80]}…")
check(RESULT["create_equal"] == 201,
      f"iterations == tool calls is accepted: {RESULT['create_equal']}")
check(RESULT["create_no_tools"] == 201,
      f"an agent with no tools may have any pair: {RESULT['create_no_tools']}")
check(RESULT["load_legacy"] == 200,
      f"an incoherent agent already on disk still loads: {RESULT['load_legacy']}")
check(RESULT["edit_untouched"] == 200,
      f"editing it WITHOUT touching the numbers is allowed: {RESULT['edit_untouched']}")
check(RESULT["edit_raises_calls"] == 400,
      f"changing it into a worse pair is refused: {RESULT['edit_raises_calls']}")
check(RESULT["edit_fixes"] == 200,
      f"fixing it is allowed: {RESULT['edit_fixes']}")

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall ok")
