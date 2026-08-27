#!/usr/bin/env python3
"""The debug trace: one switch, in Settings.

Run: server/.venv/bin/python tests/test_debug_toggle.py

Tracing writes the COMPLETE prompt, tool schemas and tool results of every
iteration to a file — the full text of the user's conversations. That shapes
every decision here:

  1. ONE source, settings.json `debug`. There is deliberately no environment
     override: a second way to set it only creates the question "why is it on
     when the UI says off";
  2. resolved on EVERY call, never cached at import — a debug switch that needs
     a restart is useless exactly when you reach for it, mid-incident with
     turns in flight;
  3. the file APPENDS across turns. It used to be truncated at every top-level
     turn, which meant it could only ever answer "what did the LAST turn do" —
     while tracing is mostly opened to explain something that already happened;
  4. so it is bounded by ROTATION instead, one generation kept;
  5. with tracing off, nothing is written at all.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name
os.environ.pop("MYAGENT_DEBUG", None)

from app import config                                    # noqa: E402
from app.models import Settings                           # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# 1. the setting drives it, live.
config.settings = Settings(debug=False)
check("off by default", config.debug_enabled() is False)
config.settings = Settings(debug=True)
check("turning the setting on takes effect with no restart",
      config.debug_enabled() is True)
check("a Settings without the field is simply off",
      Settings().debug is False)

# 2. no environment override exists any more.
check("MYAGENT_DEBUG is not read at all",
      "MYAGENT_DEBUG\"" not in (ROOT / "server" / "app" / "config.py").read_text()
      and "DEBUG_PINNED" not in (ROOT / "server" / "app" / "config.py").read_text())
os.environ["MYAGENT_DEBUG"] = "1"
import importlib                                          # noqa: E402
importlib.reload(config)
config.settings = Settings(debug=False)
check("an old MYAGENT_DEBUG=1 in the environment is simply ignored",
      config.debug_enabled() is False)
os.environ.pop("MYAGENT_DEBUG", None)
importlib.reload(config)

# 3 + 4. append, then rotate.
config.settings = Settings(debug=True)
config.ensure_debug_dir()
log = config.DEBUG_LOG_FILE
prev = log.with_suffix(log.suffix + ".1")

log.write_text("turn one\n")
config.rotate_debug_log()
check("a small file is not rotated", log.exists() and not prev.exists())
with open(log, "a") as f:
    f.write("turn two\n")
check("a second turn APPENDS instead of truncating",
      log.read_text() == "turn one\nturn two\n")

log.write_text("x" * (config.DEBUG_MAX_BYTES + 10))
config.rotate_debug_log()
check("over the cap the file is rotated away", not log.exists())
check("...into one kept generation", prev.exists())
config.rotate_debug_log()
check("rotating with no current file does not raise", not log.exists())

# The two files are separate and rotate independently: the narrative and the
# raw calls answer different questions and drown each other when interleaved.
api = config.API_LOG_FILE
check("there are exactly two trace files",
      set(config.debug_files()) == {"debug", "api"})
check("...and they are different files", api != log)
api.write_text("y" * (config.DEBUG_MAX_BYTES + 10))
config.rotate_debug_log(api)
check("the api log rotates on its own", not api.exists()
      and api.with_suffix(api.suffix + ".1").exists())

# 5. off means silent — checked through the executor, which is what writes.
import asyncio                                            # noqa: E402
from app.engine.executor import AgentExecutor, Stores     # noqa: E402
from app.storage.store import JsonStore                   # noqa: E402
from app.tools.registry import ToolRegistry               # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
models.save("m", {"id": "m", "name": "M", "provider": "llamacpp", "model": "m",
                  "base_url": "http://localhost:8080"})
agents.save("a", {"id": "a", "name": "A", "model_id": "m", "system_prompt": "p"})
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")


async def main():
    ex = await AgentExecutor.create_for_agent("a", registry, stores)
    for p in (log, prev):
        p.unlink(missing_ok=True)

    config.settings = Settings(debug=False)
    ex._debug_reset("segreto")
    ex._debug_log("segreto")
    check("with tracing off nothing is written at all", not log.exists())

    config.settings = Settings(debug=True)
    ex._debug_reset("ciao")
    ex._debug_log("una riga")
    body = log.read_text()
    # The raw-call log is written by LLMProvider, so ANY caller lands in it —
    # the auto-routing classifier included, which is the whole point.
    from app.engine import trace                          # noqa: E402
    trace.call("auto-route (3 candidates)", "http://x/v1/chat/completions",
               {"model": "m", "messages": [{"role": "user", "content": "ciao"}]})
    trace.reply("auto-route (3 candidates)", "salute")
    api_body = config.API_LOG_FILE.read_text()
    check("a call made outside any turn is logged", "auto-route" in api_body)
    check("...with its payload", "ciao" in api_body)
    check("...and its answer", "salute" in api_body)
    check("the raw calls do NOT pollute the turn narrative",
          "auto-route" not in log.read_text())
    check("with tracing on the turn header is written", "TURN" in body)
    check("...naming the agent and the model", "'a'" in body and "m" in body)
    check("...and the user message", "ciao" in body)
    check("...and the logged line", "una riga" in body)

    ex._debug_reset("secondo turno")
    check("a second turn is appended below the first",
          "ciao" in log.read_text() and "secondo turno" in log.read_text())

    # A sub-agent must not reset the file: it would cut the parent's turn in half.
    deep = await AgentExecutor.create_for_agent("a", registry, stores, depth=1)
    before = log.read_text()
    deep._debug_reset("delega")
    check("a sub-agent does not open a new turn", log.read_text() == before)


asyncio.run(main())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — one switch in Settings, live with no restart, and the trace grows "
      "by turns under rotation")
