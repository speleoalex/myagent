#!/usr/bin/env python3
"""An autonomous wake named tools that were not in its payload.

Orin, 2026-09-17, Qwen3-VL-4B, `master` with Agent.lazy_tools on. The wake
prompt tells the agent, in so many words, "use the notify_user tool" and "with
the manage_tasks tool" — but under lazy activation those are only catalogue
entries, and the first iteration carries `activate_tools` alone. So the whole
wake hangs on one guess. Seven real wakes, split by what that opening
`activate_tools` picked:

  * picked "autonomy" .... 4/4 delivered (07:30 heart, drawn and attached;
                           10:01; 20:56; 22:30 Sylvia)
  * picked "web" ......... 0/3 delivered (09:00 weather, 20:57 and 21:20 heart)

The failures share one shape, and it is not a near miss: having opened `web`,
the model never called notify_user at all. Handed "Manda un'immagine di un
cuore ad Alessandro su Telegram" it read a question about Telegram, spent its
five `max_tool_calls` on web_research and browse_web ("sendPhoto - Telegram Bot
PHP SDK", "Lesson 2. Photo Bot") and closed with a tutorial: "Per inviare
un'immagine... 1. Creare un bot Telegram: apri Telegram e cerca @BotFather".
The same shape ate the 09:00 weather task, which gathered the forecast and then
told nobody. Unattended there is no user to notice the wrong turn and ask again.

So the tools the prompt NAMES are preloaded rather than advertised. What is
pinned here:

  * an executor preloads nothing unless somebody asks (the flag stays inert for
    every ordinary turn, lazy or not);
  * `preload_tools` switches on the categories holding those ids, before the
    first payload — so their schemas are in iteration 1 and notify_user can be
    called without a guess;
  * the gate survives it: what was NOT preloaded is still only a catalogue
    entry, which is the whole point of the flag;
  * the wake sets it, beside `unattended` and `due_task_ids`, and only for what
    the agent actually holds;
  * WAKE_PROMPT_TOOLS and the prompt agree — every name in the tuple appears in
    the text, so the list cannot rot into a lie.

Run:  server/.venv/bin/python tests/test_wake_preloaded_tools.py
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-preload-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.autonomy import (AutonomyService, WAKE_PROMPT_TOOLS,  # noqa: E402
                                 build_wake_prompt)
from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BUNDLED = _ROOT / "server" / "tools"
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


cfg = Path(_TMP) / "config"
agents = JsonStore(cfg / "agents")
model = ModelConfig(id="m", name="m", provider="llamacpp",
                    base_url="http://127.0.0.1:9")  # never contacted
registry = ToolRegistry(Path(_TMP) / "tools", bundled_dir=BUNDLED)
stores = Stores(agents=agents, models=JsonStore(cfg / "models"))

# master's real grants on the Orin.
TOOLS = ["images/*", "web/*", "notify_user", "manage_tasks", "autonomy_control",
         "memory/*", "call_agent", "recall_delegation"]


def executor(preload=(), lazy=True):
    a = Agent(id="master", name="master", tools=list(TOOLS),
              lazy_tools=lazy, memory_enabled=False)
    ex = AgentExecutor(a, model, registry, stores)
    ex.preload_tools = set(preload)
    return ex


def sent(ex):
    """The tool ids of the FIRST payload of a turn."""
    full = registry.get_definitions_for_agent(ex.agent.tools)
    ex._lazy_begin_turn(full)
    return {d["id"] for d in ex._lazy_filter(full)}


# ------------------------------------------------------------------ defaults
print("an ordinary turn is untouched")
check(executor().preload_tools == set(),
      "an executor preloads nothing unless somebody says so")

bare = sent(executor())
check(bare == {"activate_tools"},
      f"lazy and unasked: the payload is the gate alone (got {sorted(bare)})")

eager = sent(executor(lazy=False))
check("notify_user" in eager and "generate_image" in eager and len(eager) > 10,
      "flag off: every schema still goes out")

# ------------------------------------------------------------- the preloading
print("\nwhat the prompt names is in hand")
ex = executor(WAKE_PROMPT_TOOLS)
payload = sent(ex)
for t in WAKE_PROMPT_TOOLS:
    check(t in payload, f"{t} is in the first payload, not behind a guess")

check("autonomy" in ex._lazy_active,
      "the category holding notify_user is switched on before iteration 1")
check("generate_image" not in payload and "web_research" not in payload,
      "images and web are still only catalogue entries")
check("activate_tools" in payload,
      "the gate is still offered — the wrong first guess must stay recoverable")

gate = ex._lazy_gate_def()
offered = gate["parameters"]["properties"]["category"]["enum"]
check("autonomy" not in offered,
      "what is already on is not offered again")
check("images" in offered and "web" in offered,
      f"the rest of the catalogue is intact (offered: {sorted(offered)})")

# The saving is the point of the flag: preloading must not undo it.
full_n = len(registry.get_definitions_for_agent(TOOLS))
check(len(payload) < full_n / 2,
      f"still a fraction of the full set ({len(payload)} of {full_n})")

print("\nonly what the agent holds")
thin = Agent(id="thin", name="thin", tools=["notify_user"], lazy_tools=True,
             memory_enabled=False)
ex2 = AgentExecutor(thin, model, registry, stores)
ex2.preload_tools = set(WAKE_PROMPT_TOOLS)
full2 = registry.get_definitions_for_agent(thin.tools)
ex2._lazy_begin_turn(full2)
got = {d["id"] for d in ex2._lazy_filter(full2)}
check("notify_user" in got, "an agent without call_agent still gets notify_user")
check("call_agent" not in got, "a preload it was never granted stays absent")

# ------------------------------------------------------------------ the wake
print("\nthe wake is what asks")
src = inspect.getsource(AutonomyService._wake)
check("executor.preload_tools" in src, "_wake hands the executor its preloads")
check("WAKE_PROMPT_TOOLS" in src and "if t in granted" in src,
      "_wake preloads only the tools the agent was granted")

print("\nthe list and the prompt agree")
agent = Agent(id="master", name="master", tools=list(TOOLS), memory_enabled=False)
task = dict(id="t", agent_id="master", cron="30 7 * * *", enabled=True,
            prompt="Manda un'immagine di un cuore ad Alessandro su Telegram",
            next_at="2026-09-17T07:30:00")
text = build_wake_prompt(agent, [task], set(registry.expand_tool_ids(TOOLS)))
for t in WAKE_PROMPT_TOOLS:
    check(t in text, f"the prompt really does name {t}")

none_granted = build_wake_prompt(
    Agent(id="x", name="x", tools=[], memory_enabled=False), [task], set())
check(not any(t in none_granted for t in WAKE_PROMPT_TOOLS),
      "a prompt that names nothing has nothing to preload")

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("test_wake_preloaded_tools.py: all ok")
