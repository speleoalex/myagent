#!/usr/bin/env python3
"""Memory and history take a share of the context, whatever the threshold says.

PLAN-compaction-dream.md, part A1. With a 4k window (fit ~2000 tokens) a memory
agent sent up to 200 history messages and memory.md + six 1200-char session
summaries (~2800 tokens): the request overflowed before compaction ever
triggered, and _truncate_messages cut the user's own message instead.

Pinned here:

  * newest_turns keeps the newest WHOLE turns that fit; the newest one alone
    over budget is cut, never dropped, and the cut is declared;
  * a turn sends at most Agent.history_share % of fit as history, while the
    conversation handed back to the caller stays whole;
  * the memory block stays within Agent.memory_share % of fit: memory.md first,
    then this chat's summaries newest first.

Run:  server/.venv/bin/python tests/test_context_shares.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-shares-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.executor import AgentExecutor, Stores, newest_turns  # noqa: E402
from app.models import Agent, ChatMessage, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

FIT = 2000  # what a 4k window leaves to the messages
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def tokens(text: str) -> int:
    return len(text) // 4


class FakeProvider:
    supports_tools = True
    trace_label = ""

    def __init__(self):
        self.rounds: list[list[dict]] = []

    async def chat_completion_stream(self, messages, tools, temperature):
        self.rounds.append([dict(m) for m in messages])
        yield {"choices": [{"delta": {"content": "ok"}}]}

    async def context_state(self, messages, tools):
        return {"fit": FIT, "used": 10, "window": 4096}

    def token_ratio(self):
        return 1.0

    def estimate_payload_tokens(self, messages, tools=None):
        return sum(tokens(str(m.get("content") or "")) for m in messages)

    def explain_error(self, e):
        return str(e)

    async def close(self):
        pass


class FakeMemory:
    def __init__(self, md: str):
        self.md = md

    def get_memory_md(self, agent_id):
        return self.md


def _executor(memory_md: str = "") -> tuple[AgentExecutor, FakeProvider]:
    agent = Agent(id="a", name="a", tools=[], memory_enabled=bool(memory_md))
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"),
                    memory=FakeMemory(memory_md) if memory_md else None)
    ex = AgentExecutor(agent, model, ToolRegistry(Path(_TMP) / "tools"), stores)
    prov = FakeProvider()
    ex.provider = prov
    return ex, prov


def case_newest_turns():
    print("newest_turns: whole turns, newest first, newest cut not dropped")
    msgs = []
    for i in range(5):
        msgs += [{"role": "user", "content": f"q{i} " + "x" * 396},      # 100 tokens
                 {"role": "assistant", "content": f"a{i} " + "y" * 396}]
    kept = newest_turns(msgs, 450, tokens)
    check([m["content"][:2] for m in kept] == ["q3", "a3", "q4", "a4"],
          f"the two newest turns fit in 450: {[m['content'][:2] for m in kept]}")
    kept = newest_turns(msgs, 100, tokens)
    check(len(kept) == 2 and kept[0]["content"].startswith("q4"),
          "a budget below one turn still keeps the newest turn")
    check(all(m["content"].endswith("[cut]") for m in kept)
          and sum(tokens(m["content"]) for m in kept) <= 110,
          "... cut to the budget, and the cut is declared")
    check(msgs[-1]["content"].endswith("y"), "the input is not modified")
    check(newest_turns([], 100, tokens) == [], "no history, nothing")


def case_history_share():
    print("a turn sends at most history_share % of fit as history")
    ex, prov = _executor()
    conversation = []
    for i in range(100):  # 100 turns of 200 tokens: ten times the room
        conversation += [ChatMessage(role="user", content=f"q{i} " + "x" * 396),
                         ChatMessage(role="assistant", content=f"a{i} " + "y" * 396)]
    ex.agent.memory_enabled = True  # the 200-message window, not the 10 one
    done = None

    async def run():
        nonlocal done
        async for ev in ex.run_stream("now", conversation):
            if ev.get("type") == "done":
                done = ev["data"]
    asyncio.run(run())
    sent = prov.rounds[0][1:-1]  # minus system and this turn's message
    budget = FIT * ex.agent.history_share // 100
    check(sum(tokens(m["content"]) for m in sent) <= budget,
          f"history sent fits {budget} tokens: "
          f"{sum(tokens(m['content']) for m in sent)}")
    check(sent and sent[-1]["content"].startswith("a99"), "the newest turn is there")
    check(len(sent) == 2 * (budget // 200), f"whole turns only: {len(sent)} messages")
    back = [m for m in done["conversation"] if m["role"] in ("user", "assistant")]
    check(len(back) >= 200, f"the conversation handed back stays whole: {len(back)}")


def case_memory_share():
    print("the memory block stays within memory_share % of fit")
    budget = FIT * 15 // 100  # 300 tokens
    ex, _ = _executor(memory_md="## Profile\n" + "p" * 4000)  # 1000 tokens
    ex._turn_fit = FIT
    block = ex._build_memory_section([{"ts": "2026-11-01", "summary": "s" * 400}])
    check(tokens(block) <= budget + 40, f"oversized memory.md is cut: {tokens(block)} tokens")
    check("## Profile" in block and "[cut]" in block, "... from the end, cut declared")
    check("Earlier in this conversation" not in block, "no room left for summaries")

    ex, _ = _executor(memory_md="## Profile\nshort")
    ex._turn_fit = FIT
    summaries = [{"ts": f"2026-11-0{i}", "summary": f"subject{i} " + "s" * 396}
                 for i in range(1, 7)]
    block = ex._build_memory_section(summaries)
    # This chat's archived turns are an INDEX now: one subject per turn, not
    # the prose. The prose is in the topic store when it matters, and two
    # renderings of one fact drift apart.
    check("[2026-11-06]" in block and "[2026-11-01]" in block,
          "every archived turn is indexed")
    check("s" * 396 not in block, "... and the prose is NOT repeated here")
    check(block.index("2026-11-01") < block.index("2026-11-06"),
          "... kept in date order")
    check(tokens(block) <= budget + 40,
          f"the index fits the memory share on its own: {tokens(block)} tokens")

    ex._turn_fit = 0
    block = ex._build_memory_section(summaries)
    check("[2026-11-01]" in block, "fit not measured: no cap")


def case_compaction_threshold():
    print("A2: the compactor triggers at min(threshold, history share)")
    ex, _ = _executor()
    ex.agent.memory_threshold = 4000
    ex._turn_fit = 0
    check(ex.compaction_threshold() == 4000, "fit not measured: the threshold")
    ex._turn_fit = FIT  # 4k window: share 700 < 4000
    check(ex.compaction_threshold() == 700, f"small window: the share ({ex.compaction_threshold()})")
    ex._turn_fit = 900_000  # share 315k > 4000
    check(ex.compaction_threshold() == 4000, "large window: the threshold is the ceiling")

    from app.engine.memory_compactor import should_compact, _select_head
    ex.agent.memory_enabled = True
    conv = []
    for i in range(6):
        conv += [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": "y" * 400}]
    # 6 turns x 200 raw tokens = 1200; x1.5 ratio = 1800
    check(not should_compact(ex.agent, conv, 1500, 1.0), "1200 tokens under 1500: no")
    check(should_compact(ex.agent, conv, 1500, 1.5), "the same chat at ratio 1.5 is 1800: yes")
    head, n = _select_head(conv, 1500, 1.5)
    check(n == 4, f"cut to half (750) in corrected tokens: {n} turns archived")


case_newest_turns()
case_history_share()
case_memory_share()
case_compaction_threshold()
print()
if failures:
    print(f"{len(failures)} FAILED"); sys.exit(1)
print("all passed")
