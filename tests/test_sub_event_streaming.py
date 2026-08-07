"""Live sub-agent event streaming: the executor must drain push_sub_event()
envelopes WHILE a tool task runs (not after), the wire envelope must be
re-rooted (never double-wrapped) across delegation levels, and closing the
stream must cancel the running tool task (Stop must not leave a sub-agent
generating headless).

Run with the server venv (the app imports pydantic/httpx):

    server/.venv/bin/python tests/test_sub_event_streaming.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-test-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.internal import _forward_sub_event  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402


def _make_executor(handler) -> AgentExecutor:
    tools_dir = Path(_TMP) / "tools"
    tool_dir = tools_dir / "fake_delegate"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "tool.json").write_text(json.dumps({
        "name": "fake_delegate",
        "description": "test stand-in for call_agent",
        "internal": True,
        "parameters": {"type": "object", "properties": {}},
    }))
    registry = ToolRegistry(tools_dir)
    registry.register_internal("fake_delegate", handler)
    agent = Agent(id="tester", name="tester", tools=["fake_delegate"])
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"))
    return AgentExecutor(agent, model, registry, stores)


async def test_events_drain_while_tool_runs():
    """Ping-pong: the handler blocks until the consumer has SEEN its first
    event. If draining were post-hoc (old _pending_sub_events behavior) the
    handler would deadlock and wait_for would time out."""
    seen_first = asyncio.Event()

    async def handler(executor=None, **kwargs):
        executor.push_sub_event({"type": "agent_event",
                                 "data": {"path": ["sub"], "event": {"type": "token", "data": "a"}}})
        await asyncio.wait_for(seen_first.wait(), timeout=5)
        executor.push_sub_event({"type": "agent_event",
                                 "data": {"path": ["sub"], "event": {"type": "token", "data": "b"}}})
        return "TOOL DONE"

    ex = _make_executor(handler)
    items = []
    async for kind, item in ex._execute_streaming("fake_delegate", {}):
        items.append((kind, item))
        if kind == "event" and not seen_first.is_set():
            seen_first.set()

    kinds = [k for k, _ in items]
    assert kinds == ["event", "event", "result"], kinds
    assert items[0][1]["data"]["event"]["data"] == "a"
    assert items[1][1]["data"]["event"]["data"] == "b"
    assert items[2][1] == "TOOL DONE"
    print("ok: events drain live, interleaved before the result")


async def test_close_cancels_tool_task():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(executor=None, **kwargs):
        executor.push_sub_event({"type": "agent_event", "data": {}})
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    ex = _make_executor(handler)
    agen = ex._execute_streaming("fake_delegate", {})
    kind, _ = await agen.__anext__()
    assert kind == "event"
    await asyncio.wait_for(started.wait(), timeout=5)
    await agen.aclose()  # what a Stop does to the whole generator chain
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    print("ok: closing the stream cancels the running tool task")


def test_forward_sub_event_envelope():
    class FakeExec:
        def __init__(self):
            self.pushed = []

        def push_sub_event(self, ev):
            self.pushed.append(ev)

    fx = FakeExec()
    # Plain inner event -> wrapped once with this delegation's id.
    _forward_sub_event(fx, "researcher", {"type": "token", "data": "x"})
    # Already-wrapped (from a deeper level) -> path prepended, no double wrap.
    _forward_sub_event(fx, "researcher", {
        "type": "agent_event",
        "data": {"path": ["browser"], "event": {"type": "tool_start", "data": {"tool": "t"}}},
    })
    # Non-forwarded types are dropped.
    _forward_sub_event(fx, "researcher", {"type": "done", "data": {}})
    _forward_sub_event(fx, "researcher", {"type": "notice", "data": "n"})

    assert len(fx.pushed) == 2, fx.pushed
    assert fx.pushed[0]["data"]["path"] == ["researcher"]
    assert fx.pushed[0]["data"]["event"]["type"] == "token"
    assert fx.pushed[1]["data"]["path"] == ["researcher", "browser"]
    assert fx.pushed[1]["data"]["event"]["type"] == "tool_start"
    print("ok: agent_event envelope wraps once and re-roots on nesting")


if __name__ == "__main__":
    asyncio.run(test_events_drain_while_tool_runs())
    asyncio.run(test_close_cancels_tool_task())
    test_forward_sub_event_envelope()
    print("all tests passed")
