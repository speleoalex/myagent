#!/usr/bin/env python3
"""What a turn was GIVEN, as notify_user's URL attachments check it.

Observed 2026-11-05 on the catasto mailbox: asked for a cave's PDF sheet, the
model copied the pattern of other records' pdf_url with the cadastral number
(571) in place of the id (575), and mailed the sheet of another cave — the
download worked, so nothing downstream could tell. notify_user now downloads
an address only if ``AgentExecutor.saw_text`` finds it in what the turn put in
front of the model. Pinned here, on a real run_stream with a scripted
provider:

  * a URL in a tool result of this turn counts, and so does one in the user's
    message and one in an earlier USER turn of the history;
  * a URL that only the model wrote — in an earlier reply, or in the
    notify_user call itself — does not;
  * the record is per turn: the next run starts from its own messages.

Run:  server/.venv/bin/python tests/test_attachment_provenance.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-provenance-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ChatMessage, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


TOOL_URL = "https://catasto.example/caves/view/575/?export_pdf=1&format=pdf"
USER_URL = "https://example.org/user-given.pdf"
HISTORY_USER_URL = "https://example.org/from-an-earlier-question.pdf"
MODEL_URL = "https://catasto.example/caves/view/571/?export_pdf=1&format=pdf"


def _call(name: str, args: str) -> dict:
    return {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": f"c{name}", "type": "function",
        "function": {"name": name, "arguments": args}}]}}]}


def _text(s: str) -> dict:
    return {"choices": [{"delta": {"content": s}}]}


class FakeProvider:
    supports_tools = True
    trace_label = ""

    def __init__(self, script):
        self.script = list(script)

    async def chat_completion_stream(self, messages, tools, temperature):
        for c in (self.script.pop(0) if self.script else [_text("done")]):
            yield c

    async def context_state(self, messages, tools):
        return {"fit": 1_000_000, "used": 10, "window": 1_000_000}

    def token_ratio(self):
        return 4.0

    def explain_error(self, e):
        return str(e)

    async def close(self):
        pass


def _executor(script) -> AgentExecutor:
    agent = Agent(id="cur", name="c", tools=["shell_exec"], memory_enabled=False,
                  max_tool_calls=4, max_iterations=6)
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"))
    ex = AgentExecutor(agent, model, ToolRegistry(Path(_TMP) / "tools",
                                                  bundled_dir=_ROOT / "server" / "tools"),
                       stores)
    ex.provider = FakeProvider(script)

    async def fake_exec(func_name, func_args):
        yield "result", f"field,value\ncode,LI571\npdf_url,{TOOL_URL}\n"
    ex._execute_streaming = fake_exec
    return ex


async def main():
    print("What a turn was given:")
    ex = _executor([[_call("shell_exec", '{"command": "get 575"}')],
                    [_text("ecco")]])
    history = [
        ChatMessage(role="user", content=f"guarda {HISTORY_USER_URL}"),
        ChatMessage(role="assistant", content=f"La scheda è {MODEL_URL}"),
    ]
    async for _ in ex.run_stream(f"allega la scheda e {USER_URL}", conversation=history):
        pass
    check(ex.saw_text(TOOL_URL), "a URL in this turn's tool result")
    check(ex.saw_text(USER_URL), "a URL in the user's message")
    check(ex.saw_text(HISTORY_USER_URL), "a URL in an earlier user turn")
    check(not ex.saw_text(MODEL_URL), "NOT a URL only the model wrote, in an earlier reply")

    print("Per turn:")
    ex.provider = FakeProvider([[_text("ok")]])
    async for _ in ex.run_stream("altra domanda"):
        pass
    check(not ex.saw_text(TOOL_URL) and not ex.saw_text(USER_URL),
          "the next run starts from its own messages")


asyncio.run(main())

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
