#!/usr/bin/env python3
"""The `transcribed` flag — a spoken, machine-transcribed message must reach the
model with the SECTION_VOICE system note, and a typed one must not.

Run: server/.venv/bin/python tests/test_voice_transcribed_flag.py
(needs the venv: the executor imports pydantic; MYAGENT_* dirs are temporary).

The flag crosses four hand-offs (inbound route → connector.ask → CoreClient.chat
→ ChatRequest → executor), each a keyword argument that Python happily lets you
forget — the failure mode is silence, not an error. So this checks the two ends
and the middle:

  1. executor._prepare_turn injects SECTION_VOICE iff transcribed=True;
  2. ChatRequest carries the field (and defaults to False, so the web UI and
     every existing caller are untouched);
  3. the plugin call sites actually FORWARD it (source-level check: each
     hand-off names the keyword — the cheap guard against a dropped kwarg).
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# Isolate before importing app.config (it resolves paths at import time).
_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine import prompts                              # noqa: E402
from app.engine.executor import AgentExecutor, Stores       # noqa: E402
from app.models import Agent, ChatRequest, ModelConfig      # noqa: E402
from app.tools.registry import ToolRegistry                 # noqa: E402
from app.storage.store import JsonStore                     # noqa: E402

failures = []

# --- 1. the executor end -----------------------------------------------------
tools_dir = Path(_tmp.name) / "tools"
tools_dir.mkdir(parents=True, exist_ok=True)
cfg_dir = Path(_tmp.name) / "config"
ex = AgentExecutor(
    Agent(id="t", name="T", model="m", system_prompt="You are T."),
    ModelConfig(id="m", name="M", provider="llamacpp", model="x"),
    ToolRegistry(tools_dir),
    Stores(agents=JsonStore(cfg_dir / "agents"),
           models=JsonStore(cfg_dir / "models")),
)

spoken, _, _ = ex._prepare_turn([], None, transcribed=True)
typed, _, _ = ex._prepare_turn([], None)

if prompts.SECTION_VOICE not in spoken:
    failures.append("transcribed=True did not inject SECTION_VOICE")
if prompts.SECTION_VOICE in typed:
    failures.append("default turn wrongly carries SECTION_VOICE")
if ex._system_suffix and prompts.SECTION_VOICE in ex._system_suffix:
    # The last _prepare_turn was the TYPED one: the suffix must not leak
    # between turns (the no-tools fallback rebuilds the prompt from it).
    failures.append("SECTION_VOICE leaked into the next turn's suffix")

# --- 2. the request model ----------------------------------------------------
if ChatRequest(agent_id="a", message="ciao").transcribed is not False:
    failures.append("ChatRequest.transcribed must default to False")
if ChatRequest(agent_id="a", message="ciao", transcribed=True).transcribed is not True:
    failures.append("ChatRequest.transcribed=True did not stick")

# --- 3. every hand-off forwards the keyword ----------------------------------
# (source check: a dropped kwarg fails silently, this makes it fail loudly)
PLUGIN = ROOT / "connectors" / "plugin" / "myagent_connectors"
HANDOFFS = [
    (PLUGIN / "routers" / "inbound.py", "transcribed=bool(req.audio_b64)"),
    (PLUGIN / "channels" / "satellite" / "channel.py", "transcribed=transcribed"),
    (PLUGIN / "channels" / "telegram" / "channel.py", "transcribed=transcribed"),
    (PLUGIN / "channels" / "base.py", "transcribed=transcribed"),
    (PLUGIN / "core.py", "transcribed=transcribed"),
    (ROOT / "server" / "app" / "engine" / "channel_turn.py", "transcribed=req.transcribed"),
]
for path, needle in HANDOFFS:
    if needle not in path.read_text(encoding="utf-8"):
        failures.append(f"{path.relative_to(ROOT)}: hand-off '{needle}' missing")

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — SECTION_VOICE rides only on transcribed turns, and every hand-off forwards the flag")
