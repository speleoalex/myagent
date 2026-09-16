#!/usr/bin/env python3
"""call_agent names the files the called agent produced.

Born from the heart-on-Telegram task (Orin, 2026-09-15/16): the master
delegated the drawing to the illustrator, generate_image told the illustrator
"do not paste the path", and the illustrator's prose was ALL that came back to
the master — so, asked to send the picture with notify_user, the master
invented ``/workspace/heart_image.png`` twice and sent a text without the
picture the third time. The files travel in the sub-trace for the UI, but the
calling MODEL reads only the reply text.

Pinned here, against a fake sub-executor (no model, no tools):

  * a sub-agent turn that delivered files -> the reply ends with a note naming
    each file once, deeper delegations included;
  * no files -> the reply comes back untouched (no empty note);
  * the sub-trace still reaches the parent unchanged (record_sub_trace).

Run:  server/.venv/bin/python tests/test_call_agent_files.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
os.environ["MYAGENT_HOME"] = tempfile.mkdtemp(prefix="myagent-callagent-")
sys.path.insert(0, str(ROOT / "server"))

from app.engine import executor as executor_mod  # noqa: E402
from app.models import ChatResponse  # noqa: E402
from app.tools import internal  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class FakeParent:
    depth = 0
    model_override = None
    turn_attachments: list = []
    tool_registry = None
    agent = SimpleNamespace(id="master")

    def __init__(self):
        self.stores = SimpleNamespace(agents=SimpleNamespace(
            get=lambda aid: SimpleNamespace(id=aid)))
        self.sub_traces = []

    def can_call(self, target):
        return True

    def record_sub_trace(self, trace):
        self.sub_traces.append(trace)

    def push_sub_event(self, ev):
        pass


def install_fake_sub(response: ChatResponse):
    class FakeSub:
        async def run(self, message, attachments=None, event_sink=None):
            return response

    async def create_for_agent(*a, **k):
        return FakeSub()

    executor_mod.AgentExecutor.create_for_agent = staticmethod(create_for_agent)


HEART = {"path": "a-glowing-red-heart.png", "mime": "image/png",
         "title": "A glowing red heart", "size": 547169}
SKETCH = {"path": "sketch.png", "mime": "image/png", "title": "Sketch", "size": 10}

# --- files delivered, one of them twice and one by a deeper delegation ------
trace = {"agent_id": "illustrator", "iterations": 2, "reply": "A glowing heart.",
         "steps": [
             {"tool": "generate_image", "arguments": {}, "result": "...",
              "resources": [HEART]},
             {"tool": "edit_image", "arguments": {}, "result": "...",
              "resources": [HEART]},
             {"tool": "call_agent", "arguments": {}, "result": "...",
              "sub_trace": {"agent_id": "sketcher", "steps": [
                  {"tool": "generate_image", "resources": [SKETCH]}]}},
         ]}
install_fake_sub(ChatResponse(reply="The image shows a glowing red heart.",
                              iterations=2, trace=trace))
parent = FakeParent()
out = asyncio.run(internal.call_agent_handler("illustrator", "draw a heart",
                                              executor=parent))
print(out)
check(out.startswith("The image shows a glowing red heart."), "the reply text comes first")
check("[files produced by 'illustrator'" in out, "a note names the producing agent")
check(out.count("a-glowing-red-heart.png") == 1, "a file delivered twice is named once")
check("sketch.png" in out, "a file from a deeper delegation is named too")
check("notify_user" in out and "attachments" in out,
      "the note says how to send a file to somebody")
check(parent.sub_traces == [trace], "the sub-trace reaches the parent unchanged")

# --- no files -> reply untouched --------------------------------------------
install_fake_sub(ChatResponse(reply="Rome is the capital of Italy.", iterations=1,
                              trace={"agent_id": "librarian", "steps": [
                                  {"tool": "local_search", "result": "..."}]}))
out = asyncio.run(internal.call_agent_handler("librarian", "capital of Italy?",
                                              executor=FakeParent()))
check(out == "Rome is the capital of Italy.", "no files -> no note")

# --- no trace at all (older ChatResponse) -> reply untouched -----------------
install_fake_sub(ChatResponse(reply="ok", iterations=1, trace=None))
out = asyncio.run(internal.call_agent_handler("x", "hi", executor=FakeParent()))
check(out == "ok", "no trace -> no note")

print()
if failures:
    print(f"{len(failures)} FAILED"); sys.exit(1)
print("all ok")
