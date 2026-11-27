#!/usr/bin/env python3
"""An unattended turn that spends its tool budget still gets to deliver.

Production install, 2026-09-21. The daily report task of a sysadmin agent (20
tool calls per run) hit the limit at call 21 in both runs: the model was still
gathering (six of the calls were one time-tracking query cut in 2-day windows), the
executor forced the answer, the answer said "non ho potuto inviare la
notifica" — and, this being a wake, the reply was only logged. The connector,
the contacts and the SMTP were all fine; notify_user was simply never called.

Pinned here:

  * unattended + notify_user held: at the budget the refused calls are dropped,
    the model gets prompts.DELIVERY_ROUND and a payload holding notify_user
    ALONE, and its notify_user calls then run (up to _DELIVERY_CALLS_MAX);
  * the offer is made once: a second non-delivery call forces the answer as
    before;
  * an attended turn is unchanged: the budget forces the answer, no extra round
    — unless the agent holds write_pdf (catasto mailbox, 2026-11-11: asked for a
    PDF report, 25 calls of 25 went into queries): then the round is offered
    with prompts.DOCUMENT_ROUND and write_pdf alone, at no extra LLM call when
    the model just answers;
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

    def estimate_payload_tokens(self, messages, tools=None):
        return sum(len(str(m.get("content") or "")) for m in messages) // 4

    def explain_error(self, e):
        return str(e)

    async def close(self):
        pass


def _executor(script, *, unattended: bool, tools=("shell_exec", "notify_user"),
              max_tool_calls: int = 1,
              max_iterations: int = 12) -> tuple[AgentExecutor, FakeProvider, list]:
    agent = Agent(id="wake", name="w", tools=list(tools), memory_enabled=False,
                  max_tool_calls=max_tool_calls, max_iterations=max_iterations)
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
    check(ex.budget_hit == "max_tool_calls",
          "the turn records that the budget cut it short (the run log shows it)")


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


def case_attended_document_round():
    print("attended, write_pdf held: the budget buys a document round")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],          # over budget
        [_call("write_pdf", '{"path": "r.pdf", "content": "# R"}')],
        [_text("In allegato il report.")],
    ], unattended=False, tools=("shell_exec", "notify_user", "write_pdf"))
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec", "write_pdf"], f"the PDF is written past the budget: {ran}")
    rounds = [r for r in prov.rounds
              if r["messages"][-1].get("content") == prompts.DOCUMENT_ROUND]
    check(len(rounds) == 1 and rounds[0]["tools"] == ["write_pdf"],
          f"the document round sees write_pdf alone: {rounds and rounds[0]['tools']}")
    check(done is not None and done["reply"] == "In allegato il report.",
          "the model's own closing line is the reply")

    print("attended, write_pdf held, no document asked: same rounds as a forced answer")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_text("risposta")],
    ], unattended=False, tools=("shell_exec", "write_pdf"))
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec"] and len(prov.rounds) == 3 and done["reply"] == "risposta",
          f"the plain answer ends the turn, no extra LLM call: {len(prov.rounds)} rounds")

    print("within budget: nothing is recorded as cut short")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_text("fatto")],
    ], unattended=True, max_tool_calls=3)
    asyncio.run(_run(ex))
    check(ex.budget_hit == "", f"no limit hit: {ex.budget_hit!r}")


def case_both_limits_reached_together():
    print("the turn hits its last iteration AND the budget: the round still opens")
    # The Orin, 2026-11-27: 20 tool calls and 10 iterations reached together,
    # file_write refused, and no delivery round — because the round was gated
    # on `iterations < max_iterations` at the exact moment it was false.
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],                        # iter 1
        [_call("file_write", '{"path": "r.md", "content": "# R"}')],      # iter 2, refused
        [_call("file_write", '{"path": "r.md", "content": "# R"}')],      # the round
        [_text("Scritto r.md")],
    ], unattended=False, tools=("shell_exec", "file_write"),
        max_tool_calls=1, max_iterations=2)
    done = asyncio.run(_run(ex))
    check(prov.rounds[2]["tools"] == ["file_write"],
          f"the delivery round is opened past max_iterations: {prov.rounds[2]['tools']}")
    check(ran == ["shell_exec", "file_write"],
          f"and the file is written: {ran}")
    check(done is not None and done["reply"] == "Scritto r.md", "closing line")
    check(ex.budget_hit == "max_tool_calls", f"the cut is recorded: {ex.budget_hit!r}")


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


def case_attended_save_round():
    print("attended, file_write held (declared delivery): the budget buys a save round")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],          # over budget
        [_call("file_write", '{"path": "todo.md", "content": "# TODO"}')],
        [_text("Scritto todo.md.")],
    ], unattended=False, tools=("shell_exec", "file_write"))
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec", "file_write"],
          f"the file is written past the budget: {ran}")
    want = prompts.SAVE_ROUND.format(tool="file_write")
    rounds = [r for r in prov.rounds if r["messages"][-1].get("content") == want]
    check(len(rounds) == 1 and rounds[0]["tools"] == ["file_write"],
          f"the save round sees file_write alone: {rounds and rounds[0]['tools']}")
    check(done is not None and done["reply"] == "Scritto todo.md.",
          "the model's own closing line is the reply")


def case_prefers_write_pdf_over_file_write():
    print("attended, both declared: the REFUSED call picks the round's tool")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],           # budget is 1
        [_call("write_pdf", '{"path": "r.pdf", "content": "# R"}')],  # refused
        [_call("write_pdf", '{"path": "r.pdf", "content": "# R"}')],  # delivery
        [_text("Fatto")],
    ], unattended=False, tools=("shell_exec", "file_write", "write_pdf"))
    done = asyncio.run(_run(ex))
    check(prov.rounds[2]["tools"] == ["write_pdf"],
          f"the tool it was reaching for is the one offered: {prov.rounds[2]['tools']}")
    check(ran == ["shell_exec", "write_pdf"] and done["reply"] == "Fatto",
          f"and the same file is then written: {ran}")

    print("attended, both declared, the refused call is a plain reader")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_text("Fatto")],
    ], unattended=False, tools=("shell_exec", "file_write", "write_pdf"))
    asyncio.run(_run(ex))
    check(prov.rounds[2]["tools"] == ["file_write"],
          f"with no writer refused, the default order leads with file_write: "
          f"{prov.rounds[2]['tools']}")
    seen = prov.rounds[2]["messages"]
    check(not any(m.get("role") == "tool" and m.get("content") == prompts.NOT_EXECUTED
                  for m in seen),
          "a refused call to a tool OUTSIDE the round is not replayed: an "
          "assistant turn citing absent tools is what some runtimes reject")


def case_no_delivery_tool_explains_the_cut():
    print("attended, no delivery tool: the refusal is explained, not swallowed")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_call("shell_exec", '{"command": "b"}')],
        [_text("Ho letto i documenti; non ho potuto scrivere il file.")],
    ], unattended=False, tools=("shell_exec",))
    done = asyncio.run(_run(ex))
    check(ran == ["shell_exec"], f"nothing else runs: {ran}")
    rounds = [r for r in prov.rounds
              if r["messages"][-1].get("content") == prompts.BUDGET_EXHAUSTED]
    check(len(rounds) == 1, "the model is told the budget is gone, exactly once")
    check(rounds and rounds[0]["tools"] == [],
          f"that round carries no tool schemas: {rounds and rounds[0]['tools']}")
    check(done is not None and done["reply"].startswith("Ho letto"),
          "and the honest answer is the reply")


def case_budget_countdown():
    print("the countdown rides the tool result before the budget runs out")
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],   # 2 of 3 left -> warn
        [_text("fatto")],
    ], unattended=False, tools=("shell_exec", "file_write"), max_tool_calls=3)
    asyncio.run(_run(ex))
    results = [m["content"] for m in prov.rounds[1]["messages"]
               if m.get("role") == "tool"]
    check(results and "tool budget: 2 of 3" in results[-1],
          f"the model sees how many calls are left: {results[-1][-120:] if results else None}")
    check(results and "Keep one for delivering" not in results[-1]
          and "At zero only file_write still runs" in results[-1],
          "with a delivery tool held, it NAMES it instead of saying 'keep one'")

    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],
        [_text("fatto")],
    ], unattended=False, tools=("shell_exec",), max_tool_calls=3)
    asyncio.run(_run(ex))
    results = [m["content"] for m in prov.rounds[1]["messages"]
               if m.get("role") == "tool"]
    check(results and "At zero only" not in results[-1]
          and "further calls are refused" in results[-1],
          "without one, no tool is named and the cut is stated plainly")


def case_refused_call_stays_in_the_conversation():
    print("the refused call is kept, paired with the result it never got")
    big = "# TODO\n" + ("- una riga di todo molto lunga\n" * 400)   # > 6000 chars
    args = ('{"path": "todo.md", "content": "%s"}'
            % big.replace("\n", "\\n"))
    ex, prov, ran = _executor([
        [_call("shell_exec", '{"command": "a"}')],                 # budget is 1
        [_call("file_write", args)],                               # refused
        [_call("file_write", args)],                               # replayed
        [_text("Scritto todo.md")],
    ], unattended=False, tools=("shell_exec", "file_write"))
    done = asyncio.run(_run(ex))
    seen = prov.rounds[2]["messages"]
    assistant = [m for m in seen if m.get("role") == "assistant" and m.get("tool_calls")]
    results = [m for m in seen if m.get("role") == "tool"]
    check(bool(assistant), "the assistant turn carrying the call is still there")
    check(results and results[-1]["content"] == prompts.NOT_EXECUTED,
          f"and its result says it was not run: {results[-1]['content'][:60] if results else None}")
    check(assistant and len(assistant[-1]["tool_calls"][0]["function"]["arguments"]) > 6000,
          "the whole document survives — no size threshold")
    check(ran == ["shell_exec", "file_write"],
          f"so the same call, sent again, writes the file: {ran}")
    check(done is not None and done["reply"] == "Scritto todo.md", "closing line")


def case_no_delivery_tool_explains_the_cut_dup():
    pass


if __name__ == "__main__":
    for case in (case_unattended_delivers, case_offer_is_made_once, case_delivery_cap,
                 case_attended_unchanged, case_attended_document_round,
                 case_attended_save_round, case_prefers_write_pdf_over_file_write,
                 case_refused_call_stays_in_the_conversation,
                 case_no_delivery_tool_explains_the_cut, case_budget_countdown,
                 case_both_limits_reached_together,
                 case_without_notify_user_unchanged):
        case()
    if failures:
        print(f"\n{len(failures)} FAILED")
        sys.exit(1)
    print("\nall ok")
