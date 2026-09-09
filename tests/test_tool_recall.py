#!/usr/bin/env python3
"""What an agent's own tools returned survives into the next turn.

Run: server/.venv/bin/python tests/test_tool_recall.py

`is_scaffolding_message` drops every `role: tool` message from `conversation`
— it must, or the model mimics the tool protocol in later turns — so from the
second turn on the model sees only its own past PROSE. Asked a follow-up it
then answers from what it SAID rather than from what it FOUND. Observed on a
real chat over a folder of medical records: the agent searched, reported the
visit date correctly, then answered "yes, the report exists" (invented) and,
one question later, "I cannot find it" (also invented) — without searching
either time.

The fix is the twin of `## Agent findings`, one level down: quote the recent
tool results verbatim on the SYSTEM prompt. Cases:

  1. tool_history() reads them back out of the session, oldest first, ids t<N>;
  2. `call_agent` is EXCLUDED — delegation_history already covers it verbatim,
     and quoting it twice would double its cost and let the two blocks disagree
     about where it was cut;
  3. a failed call is kept ("we looked and it was not there" is information);
  4. the section quotes the newest result with the most room, declares every
     cut, and points the cut AT THE TOOL — there is no recall_* companion,
     because re-running a search costs one call;
  5. nothing reaches `conversation[]`: it rides on the system suffix, so
     is_scaffolding_message and the rewind endpoint are untouched;
  6. no tool history -> no section at all, i.e. today's prompt exactly;
  7. all four turn paths actually pass it (a dropped kwarg is silent), and the
     autonomous one passes a WINDOWED copy (see NON_FACT_TOOLS below and
     AutonomousConfig.history_messages);
  8. NON_FACT_TOOLS are excluded: a result that expires or that is a receipt for
     something DONE is not a fact to answer from later.
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

from app.engine.executor import AgentExecutor, Stores     # noqa: E402
from app.storage.sessions import NON_FACT_TOOLS, tool_history  # noqa: E402
from app.storage.store import JsonStore                   # noqa: E402
from app.tools.registry import ToolRegistry               # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")

models.save("m", {"id": "m", "name": "M", "provider": "llamacpp", "model": "m",
                  "base_url": "http://localhost:8080"})
agents.save("a", {"id": "a", "name": "A", "model_id": "m", "system_prompt": "P",
                  "tools": ["local_search"]})

LONG = "risultato " * 400          # ~4000 chars, comfortably over every budget

session = {"messages": [
    {"role": "user", "text": "quando la visita?"},
    {"role": "tool", "tool": "local_search",
     "arguments": {"query": "data visita cardiologica"},
     "result": "p:referti/visita.pdf:1 | visita.pdf p.1 | VISITA CARDIOLOGICA 16 Feb 2026"},
    {"role": "assistant", "text": "Il 16 febbraio 2026."},
    {"role": "tool", "tool": "call_agent",
     "arguments": {"agent_id": "librarian", "message": "x"},
     "result": "una risposta del sub-agente"},
    {"role": "tool", "tool": "local_read", "arguments": {"id": "p:referti/visita.pdf:1"},
     "result": "ERROR: file not found"},
    {"role": "tool", "tool": "local_search", "arguments": {"query": "referto"},
     "result": LONG},
    # 8. Neither of these is a fact to answer from later: the date EXPIRES, and
    #    the notify_user result is a receipt for something already done.
    {"role": "tool", "tool": "get_current_time", "arguments": {},
     "result": "2026-09-03 10:05:31"},
    {"role": "tool", "tool": "notify_user", "arguments": {"message": "buongiorno"},
     "result": "Message sent via binding 'master-it' to: Alessandro"},
]}

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# 1 + 2 + 3.
hist = tool_history(session)
check("tool results are read back out of the session", len(hist) == 3)
check("ids are t<N> numbered from the oldest",
      [h["id"] for h in hist] == ["t1", "t2", "t3"])
check("call_agent is excluded — delegation_history owns it",
      all(h["tool"] != "call_agent" for h in hist))
check("a failed call is kept", any("ERROR" in h["result"] for h in hist))
check("the limit keeps the NEWEST, with their original ids",
      [h["id"] for h in tool_history(session, limit=2)] == ["t2", "t3"])

# 8. A result that expires, or that reports what the agent DID, is dropped —
#    quoted as a standing fact the stale date was worse than absent: the agent
#    asked a sub-agent for YESTERDAY's weather forecast and relayed it
#    (observed 2026-08-29..09-04). The excluded names take no id either.
check("get_current_time is excluded — its answer expires",
      all(h["tool"] != "get_current_time" for h in hist))
check("notify_user is excluded — a receipt is not a finding",
      all(h["tool"] != "notify_user" for h in hist))
check("an excluded tool consumes no id, so the ids of the others hold",
      [h["id"] for h in hist] == ["t1", "t2", "t3"])
check("the readers of a memory are NOT excluded — they retrieve facts",
      not {"memory_read", "memory_search"} & NON_FACT_TOOLS)
check("recall_delegation is excluded — the findings block already owns it",
      "recall_delegation" in NON_FACT_TOOLS)


async def main():
    ex = await AgentExecutor.create_for_agent("a", registry, stores)

    # 6. nothing recorded -> no section, i.e. the prompt as it is today.
    check("no tool history -> no section at all",
          ex._build_tool_results_section() == "")

    ex._tool_results = hist
    sec = ex._build_tool_results_section()

    # 4.
    check("the section is headed and present", "## What your tools returned" in sec)
    check("the newest result comes first",
          sec.index("[t3]") < sec.index("[t2]") < sec.index("[t1]"))
    check("the query is quoted so an entry is recognisable",
          "local_search(referto)" in sec)
    check("an oversized result is cut", "… [cut at" in sec)
    check("the cut points AT THE TOOL, not at a recall tool",
          "call local_search again for the rest" in sec)
    check("there is no recall_* companion to pay for",
          "recall_tool" not in sec)
    check("the newest entry gets the most room",
          f"cut at {ex._TOOL_RESULTS_CHARS} of" in sec)
    check("the section tells the model these are its OWN facts",
          "facts YOU obtained" in sec)
    check("...and forbids answering from an earlier reply",
          "never answer from what you said" in sec)

    # the window is bounded and the drop is DECLARED
    ex._tool_results = [{"id": f"t{i}", "tool": "local_search",
                         "arguments": {"query": f"q{i}"}, "result": f"r{i}"}
                        for i in range(1, 9)]
    sec = ex._build_tool_results_section()
    check("the window is bounded", "[t1]" not in sec and "[t8]" in sec)
    check("the ones dropped are declared, not silently missing",
          "earlier tool call(s) not shown" in sec)

    # 5. it must ride on the SYSTEM suffix, never enter conversation[].
    ex._tool_results = hist
    ex._prepare_turn([], None)
    check("the section lands on the system suffix",
          "## What your tools returned" in ex._system_suffix)

    check("a tool message is still scaffolding — nothing about the drop changed",
          AgentExecutor.is_scaffolding_message("tool", "anything at all"))


asyncio.run(main())

# 7. every turn path passes it — a dropped kwarg fails silently, so check the
#    source the way test_voice_transcribed_flag does.
for f in ("server/app/routers/chat.py", "server/app/engine/channel_turn.py"):
    src = (ROOT / f).read_text(encoding="utf-8")
    check(f"{Path(f).name} passes tool_results",
          "tool_results=tool_history(session)" in src)
# The autonomous path passes it too, but WINDOWED: an unbounded copy fed the
# previous wakes back into the prompt regardless of history_messages. Pinned in
# test_recall_delegation.test_wake_findings_follow_the_history_window.
src = (ROOT / "server/app/engine/autonomy.py").read_text(encoding="utf-8")
check("autonomy.py passes tool_results, windowed by history_messages",
      "tool_results=prior_tools" in src
      and "tool_history(session, hist) if hist > 0 else []" in src)
src = (ROOT / "server/app/tools/internal.py").read_text(encoding="utf-8")
check("a sub-agent is NOT given the parent's tool history",
      "tool_results=" not in src)

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — an agent's own tool results survive into the next turn, on the "
      "system prompt, with every cut declared and pointing back at the tool")
