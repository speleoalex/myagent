#!/usr/bin/env python3
"""An agent whose working folder is gone must say so, not improvise.

Orin, 2026-11-25: "cerca-lavoro" already existed with its folder pointed at a
NAS path that was not mounted. Nothing flagged it: the agent list showed it
healthy, and only a failing tool would have told — to the model, which then
improvises. Saving must stay allowed (a mount can be down for a minute), so the
state is REPORTED instead.

Pinned here (temporary MYAGENT_HOME, no model, no transport):

  * FolderConfig.missing: no folder -> False, existing -> False, absent -> True;
  * a folder that does not answer in time counts as missing (a dead network
    mount blocks a stat for minutes), and the caller is not held up;
  * GET /api/agents and GET /api/agents/{id} carry folder_missing, and the
    field is never stored (a PUT that sends it back does not persist it);
  * a wake of an agent with its folder missing fails BEFORE the model runs,
    as an ordinary error (retried with backoff), and its task records why.

Run:  server/.venv/bin/python tests/test_agent_folder_missing.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="myagent-foldermissing-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.engine.autonomy import AutonomyService  # noqa: E402
from app.engine.executor import Stores  # noqa: E402
from app.models import Agent, AutonomousConfig, FolderConfig  # noqa: E402
from app.routers import agents as agents_router  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.storage.tasks import TaskStore  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


THERE = Path(_TMP) / "cv"
THERE.mkdir()
GONE = str(Path(_TMP) / "nas-not-mounted" / "cv")

# ------------------------------------------------------------------ the check
print("FolderConfig.missing")
check(asyncio.run(FolderConfig().missing()) is False, "no folder: nothing is missing")
check(asyncio.run(FolderConfig(path=str(THERE)).missing()) is False, "an existing folder is there")
check(asyncio.run(FolderConfig(path=GONE).missing()) is True, "an absent folder is missing")


def hung_isdir(path):
    time.sleep(1.0)  # a stat on a dead sshfs mount
    return True


async def timed_check():
    # Timed INSIDE the loop: asyncio.run joins the leftover thread when it
    # closes, which a long-lived server never does.
    started = time.monotonic()
    result = await FolderConfig(path=str(THERE)).missing(timeout=0.2)
    return result, time.monotonic() - started


with mock.patch("app.models.os.path.isdir", hung_isdir):
    late, waited = asyncio.run(timed_check())
check(late is True, "a folder that does not answer in time counts as missing")
check(waited < 0.9, f"…and the caller is not held up by it ({waited:.2f}s)")

# ------------------------------------------------------------------- the API
print("agents API")
cfg_dir = Path(_TMP) / "config"
store = JsonStore(cfg_dir / "agents")
store.save("cerca-lavoro", Agent(id="cerca-lavoro", name="Cerca lavoro", model_id="m",
                                 folder={"path": GONE}).model_dump())
store.save("archivio", Agent(id="archivio", name="Archivio", model_id="m",
                             folder={"path": str(THERE)}).model_dump())
store.save("chat", Agent(id="chat", name="Chat", model_id="m").model_dump())

app = FastAPI()
app.state.stores = Stores(agents=store, models=JsonStore(cfg_dir / "models"))
app.include_router(agents_router.router, prefix="/api/agents")
client = TestClient(app)

listed = {a["id"]: a for a in client.get("/api/agents").json()}
check(listed["cerca-lavoro"]["folder_missing"] is True, "the list flags the unreachable folder")
check(listed["archivio"]["folder_missing"] is False, "…not the one that exists")
check(listed["chat"]["folder_missing"] is False, "…nor an agent without a folder")
one = client.get("/api/agents/cerca-lavoro").json()
check(one["folder_missing"] is True, "the single-agent read flags it too")
client.put("/api/agents/cerca-lavoro", json={**one, "description": "edited"})
check("folder_missing" not in store.get("cerca-lavoro"),
      "sent back by a PUT, the field is not stored")

# ------------------------------------------------------------------ the wake
print("wake")


class NoLive:
    """The wake must stop before it needs the run manager."""

    def is_active(self, sid):
        return False

    def start(self, sid, drive):
        raise AssertionError("the model was started")


tasks = TaskStore(cfg_dir / "tasks")
tasks.store.save("task-cv", dict(id="task-cv", agent_id="cerca-lavoro", cron="30 7 * * *",
                                 prompt="Ricerca giornaliera", next_at="2026-11-25T07:30:00",
                                 enabled=True))
service = AutonomyService(stores=app.state.stores, tool_registry=None, named_sessions=None,
                          live=NoLive(), tasks=tasks, base_dir=Path(_TMP) / "autonomy")
agent = Agent(**store.get("cerca-lavoro"))


async def wake() -> None:
    with mock.patch("app.engine.autonomy.AgentExecutor.create_for_agent",
                    side_effect=AssertionError("an executor was built")):
        await service._wake(agent, AutonomousConfig())

asyncio.run(wake())
t = tasks.get("task-cv")
st = service._state("cerca-lavoro")
check(t["last_result"] == "error", "the run is an error, not a run with failing tools")
check("folder is not reachable" in t["last_reply"] and "nas-not-mounted" in t["last_reply"],
      "its reply says which folder")
check(t["next_at"] == "2026-11-25T07:30:00", "the task stays due: it is retried")
check(st.get("consecutive_errors") == 1 and st.get("retry_after"),
      "with the ordinary backoff (and the alert after max_consecutive_errors)")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
