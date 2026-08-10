#!/usr/bin/env python3
"""The per-chat model override — ChatRequest.model_override.

Run: server/.venv/bin/python tests/test_model_override.py
(needs the venv; MYAGENT_* dirs are temporary, and no network is ever touched:
every case below resolves a model DIRECTLY — resolve_default, which probes
backends, is only reached when an agent is on "default" WITHOUT an override,
which is exactly the pre-existing path this feature must not disturb.)

The contract, one case each:
  1. an agent on the "default" sentinel + override -> runs the override;
  2. an agent PINNED to a model ignores the override (pinning is the stronger,
     per-agent choice) — but still CARRIES it, so call_agent can hand it to a
     "default" sub-agent behind a pinned router;
  3. an override naming a deleted model refuses with a readable error, it does
     not fall back silently;
  4. no override = field defaults keep every existing caller untouched;
  5. call_agent's create_for_agent call site forwards executor.model_override
     (source check: a dropped kwarg fails silently).
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
from app.models import ChatRequest                          # noqa: E402
from app.tools.registry import ToolRegistry                 # noqa: E402
from app.storage.store import JsonStore                     # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")

models.save("model-a", {"id": "model-a", "name": "A", "provider": "llamacpp",
                        "model": "a", "base_url": "http://localhost:8080"})
models.save("model-b", {"id": "model-b", "name": "B", "provider": "llamacpp",
                        "model": "b", "base_url": "http://localhost:8080"})
agents.save("on-default", {"id": "on-default", "name": "D", "model_id": "default",
                           "system_prompt": "x"})
agents.save("pinned", {"id": "pinned", "name": "P", "model_id": "model-a",
                       "system_prompt": "x"})

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


async def main():
    # 1. default agent + override -> the override serves the turn.
    ex = await AgentExecutor.create_for_agent(
        "on-default", registry, stores, model_override="model-b")
    check("override applies to a 'default' agent",
          ex.model_config.id == "model-b")
    check("an explicit pick must not raise the fallback notice",
          ex.notice is None)

    # 2. pinned agent: keeps its model, carries the override for delegation.
    ex = await AgentExecutor.create_for_agent(
        "pinned", registry, stores, model_override="model-b")
    check("a pinned agent keeps its own model",
          ex.model_config.id == "model-a")
    check("a pinned agent still carries the override for its sub-agents",
          ex.model_override == "model-b")

    # 3. override naming a deleted model -> readable refusal, never a silent
    #    fallback (the user picked it; substituting is the notice-worthy sin).
    try:
        await AgentExecutor.create_for_agent(
            "on-default", registry, stores, model_override="gone")
        failures.append("a dead override must raise, not fall back")
    except ValueError as e:
        check("the refusal names the model and the fix",
              "gone" in str(e) and "available" in str(e))

    # 4. defaults: nothing set, nothing carried.
    ex = await AgentExecutor.create_for_agent("pinned", registry, stores)
    check("no override by default", ex.model_override is None)
    check("ChatRequest defaults to no override",
          ChatRequest(agent_id="a", message="x").model_override is None)
    check("empty string means no override (the UI's 'default' option)",
          ChatRequest(agent_id="a", message="x", model_override="").model_override is None)

    # 5. the delegation call site forwards it (source check, same rationale as
    #    test_voice_transcribed_flag: a forgotten kwarg is silent).
    internal = (ROOT / "server" / "app" / "tools" / "internal.py").read_text(encoding="utf-8")
    check("call_agent forwards executor.model_override",
          "model_override=executor.model_override" in internal)


asyncio.run(main())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — the chat's model pick reaches 'default' agents (directly and through"
      " delegation), never overrides a pinned one, and dies loudly when stale")
