#!/usr/bin/env python3
"""An unattended turn that spends its tool budget still gets to deliver.

tmind, 2026-09-21. The daily report task of `sistemista-techmakers` (20 tool
calls per run) hit the limit at call 21 in both runs: the model was still
gathering (six of the calls were one Float query cut in 2-day windows), the
executor forced the answer, the answer said "non ho potuto inviare la
notifica" — and, this being a wake, the reply was only logged. The connector,
the contacts and the SMTP were all fine; notify_user was simply never called.

Pinned here:

  * unattended + notify_user held: at the budget the refused calls are dropped,
    the model gets prompts.DELIVERY_ROUND and a payload holding notify_user
    ALONE, and its notify_user calls then run (up to _DELIVERY_CALLS_MAX);
  * the offer is made once: a second non-delivery call forces the answer as
    before;
  * an attended turn is unchanged: the budget forces the answer, no extra round;
  * an unattended agent WITHOUT notify_user is unchanged too.

Run:  server/.venv/bin/python tests/test_delivery_round.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-delivery-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine import executor as ex_mod  # noqa: E402
from app.engine import prompts  # noqa: E402
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


# ------------------------------------------------------------ fake provider
def _call(name: str, args: str, idx: int = 0) -> dict:
    return {"choices": [{"delta": {"tool_calls": [{
        "index": idx, "id": f"c{name}{idx}", "type": "function",
        "function": {"name": name, "arguments": args}}]}}]}


def _text(s: str) -> dict:
    return {"choices": [{"delta": {"content": s}}]}


class FakeProvider:
    """Replays a script: one list of chunks per LLM round. Records what each
    round was sent, which is what the assertions read."""
    supports_tools = True
    trace_label = ""

    def __init__(self, script):
        self.script = list(script)
        self.rounds: list[dict] = []

    async def chat_completion_stream(self, messages, tools, temperature):
        self.rounds.append({"messages": [dict(m) for m in messages],
                            "tools": [t["function"]["name"] for t in (tools or [])]})
        if not self.script:
            chunks = [_text("(script exhausted)")]
        else:
            chunks = self.script.pop(0)
        for c in chunks:
            yield c

    async def context_state(self, messages, tools):
        return {"fit": 1_000_000, "used": 10, "window": 1_000_000}

    def token_ratio(self):
        return 4.0

    def explain_error(self, e):
        return str(e)

    async def close(self):
        pass


def _executor(script, *, unattended: bool, tools=("shell_exec", "notify_user"),
              max_tool_calls: int = 1) -> tuple[AgentExecutor, FakeProvider, list]:
    agent = Agent(id="wake", name="w", tools=list(tools), memory_enabled=False,
                  max_tool_calls=max_tool_calls, max_iterations=12)
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"))
    ex = AgentExecutor(agent, model, ToolRegistry(Path(_TMP) / "tools", bundled_dir=BUNDLED),
                       stores)
    ex.unattended = unattended
    prov = FakeProvider(script)
    ex.provider = prov
    ran: list[str] = []

    async def fake_exec(func_name, func_args):
        ran.append(func_name)
        yield "result", f"{func_name} ok"
    ex._execute_streaming = fake_exec
    return ex, prov, ran


async def _run(ex: AgentExecutor) -> dict:
    done = None
    async for ev in ex.run_stream("[AUTONOMOUS WAKE] do the report"):
        if ev.get("type") == "done":
            done = ev["data"]
    return done


def _delivery_rounds(prov: FakeProvider) -> list[dict]:
    return [r for r in prov.rounds
            if r["messages"][-1].get("content") == prompts.DELIVERY_ROUND]


# ------------------------------------------------------------------ cases
def case_unattended_delivers():
    print("unattended, notify_user held: the budget buys a delivery round")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],         # call 1: runs
        [_call("shell_exec", '{"command": "b"}')],         # call 2: over budget
        [_call("notify_user", '{"text": "r", "to": "A"}'),  # delivery round
         _call("notify_user", '{"text": "r", "to": "S"}', 1)],
        [_text("Report inviato ad Alessandro e Stefano.")],
    ], unattended=True)
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec", "notify_user", "notify_user"],
          f"the refused call is dropped and both deliveries run: {ran}")
    rounds = _delivery_rounds(prov)
    check(len(rounds) == 1, "the delivery prompt is sent exactly once")
    check(rounds and rounds[0]["tools"] == ["notify_user"],
          f"the delivery round sees notify_user alone: {rounds and rounds[0]['tools']}")
    check(done is not None and done["reply"].startswith("Report inviato"),
          "the model's own closing line is the reply")
    check(prov.script == [], "no round was skipped")


def case_offer_is_made_once():
    print("unattended: a second non-delivery call forces the answer")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],   # over budget -> offer
        [_call("shell_exec", '{"command": "c"}')],   # ignores the offer
        [_text("sintesi forzata")],                  # forced synthesis
    ], unattended=True)
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec"], f"nothing else runs: {ran}")
    check(len(_delivery_rounds(prov)) == 1, "the offer is not repeated")
    check(done is not None and done["reply"] == "sintesi forzata",
          "the turn then ends with the forced answer, as before")


def case_delivery_cap():
    print("unattended: the free notify_user calls are capped")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_call("notify_user", f'{{"text": "r", "to": "{i}"}}', i)
         for i in range(ex_mod._DELIVERY_CALLS_MAX + 2)],
        [_text("fatto")],
    ], unattended=True)
    asyncio.run(_run(ex))
    check(ran.count("notify_user") == ex_mod._DELIVERY_CALLS_MAX,
          f"{ex_mod._DELIVERY_CALLS_MAX} deliveries run, the rest are refused: {ran}")


def case_attended_unchanged():
    print("attended: the budget forces the answer, no extra round")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_text("risposta forzata")],
    ], unattended=False)
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec"], f"one call ran: {ran}")
    check(_delivery_rounds(prov) == [], "no delivery prompt in an attended turn")
    check(done is not None and done["reply"] == "risposta forzata", "forced answer as before")


def case_without_notify_user_unchanged():
    print("unattended but notify_user not held: nothing to offer")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_text("risposta forzata")],
    ], unattended=True, tools=("shell_exec",))
    done = asyncio.run(_run(ex))
    check(_delivery_rounds(prov) == [], "no delivery prompt without the tool")
    check(done is not None and done["reply"] == "risposta forzata", "forced answer as before")


if __name__ == "__main__":
    for case in (case_unattended_delivers, case_offer_is_made_once, case_delivery_cap,
                 case_attended_unchanged, case_without_notify_user_unchanged):
        case()
    if failures:
        print(f"\n{len(failures)} FAILED")
        sys.exit(1)
    print("\nall ok")
