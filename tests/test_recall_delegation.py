"""What a sub-agent reported must survive the turn it was said in.

A `tool` message never enters conversation[] (is_scaffolding_message drops it,
or the model would mimic the protocol next turn), so the reply of a delegated
agent used to vanish the moment master's own answer was written: asked "what
did you find?" the turn after, a router that HAD the data answered "I have no
data" while the UI still showed it (observed 2026-08-19).

Two halves cover it, and each case below pins one of them:
  1. the newest few delegations are quoted VERBATIM in the system prompt
     (executor._build_findings_section) — no decision asked of the model;
  2. the full text is served on demand by recall_delegation.
Plus the third, unrelated-but-adjacent win: a background compaction is
CANCELLED when a new turn starts, instead of being paid for and discarded.

Run with the server venv (the app imports pydantic/httpx):

    server/.venv/bin/python tests/test_recall_delegation.py
"""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-test-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine import memory_compactor  # noqa: E402
from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.sessions import delegation_history, new_session  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.internal import recall_delegation_handler  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BUNDLED = _ROOT / "server" / "tools"


# --------------------------------------------------------------- fixtures

def _tool_msg(agent_id: str, message: str, result: str, ts: str,
              tool: str = "call_agent") -> dict:
    """A stored tool message in the shape tool_message_from_step writes."""
    return {
        "role": "tool",
        "tool": tool,
        "arguments": {"agent_id": agent_id, "message": message},
        "result_preview": result[:200],
        "result": result,
        "ts": ts,
        "sub_trace": {"agent_id": agent_id, "reply": result, "steps": []},
    }


def _session_with(n: int) -> dict:
    s = new_session("s1", "master")
    for i in range(1, n + 1):
        s["messages"].append({"role": "user", "text": f"q{i}", "ts": f"2026-08-19T20:0{i}:00"})
        s["messages"].append(_tool_msg("web-researcher", f"question {i}",
                                       f"finding number {i}", f"2026-08-19T20:0{i}:30"))
        s["messages"].append({"role": "assistant", "text": f"a{i}"})
    return s


def _executor(tools) -> AgentExecutor:
    """A real executor over the BUNDLED tool catalogue (so `recall_delegation`
    resolves through expand_tool_ids exactly as it does in production)."""
    agent = Agent(id="router", name="router", tools=tools, memory_enabled=False)
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    registry = ToolRegistry(Path(_TMP) / "tools", bundled_dir=BUNDLED)
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"))
    return AgentExecutor(agent, model, registry, stores)


# ------------------------------------------------------------------ cases

def test_history_extraction_and_stable_ids():
    s = _session_with(2)
    # A non-delegation tool call must not enter the history, and must not
    # consume an id either (the model quotes these ids back to us).
    s["messages"].append(_tool_msg("", "", "shell output", "2026-08-19T20:03:00",
                                   tool="shell_exec"))
    got = delegation_history(s)
    assert [d["id"] for d in got] == ["d1", "d2"], got
    assert got[0]["agent_id"] == "web-researcher"
    assert got[0]["message"] == "question 1"
    assert got[1]["reply"] == "finding number 2"

    # Appending a NEW delegation must not renumber the existing ones.
    s["messages"].append(_tool_msg("librarian", "question 3", "finding number 3",
                                   "2026-08-19T20:04:00"))
    after = delegation_history(s)
    assert [d["id"] for d in after] == ["d1", "d2", "d3"], after
    assert after[0]["reply"] == got[0]["reply"]

    # A trimmed window keeps the ORIGINAL ids, or they would stop resolving.
    assert [d["id"] for d in delegation_history(s, limit=2)] == ["d2", "d3"]

    # Only the display log is read: nothing is expected in conversation[].
    assert s["conversation"] == []
    print("ok: delegation_history reads `messages`, ids are stable and skip other tools")


def test_findings_section():
    ex = _executor(["call_agent", "recall_delegation"])
    assert ex._build_findings_section() == "", "no delegations -> no section"

    ex._delegations = delegation_history(_session_with(5))
    sec = ex._build_findings_section()
    # Window: the newest three, newest first.
    assert "[d5]" in sec and "[d4]" in sec and "[d3]" in sec, sec
    assert "[d2]" not in sec and "[d1]" not in sec, sec
    assert sec.index("[d5]") < sec.index("[d4]") < sec.index("[d3]"), sec
    # The two older ones are DECLARED, not silently dropped.
    assert "2 earlier delegation(s) not shown" in sec, sec
    assert "recall_delegation" in sec, sec
    # The load-bearing line: without it the model reads the block as
    # background and still answers "I have no information".
    assert "answer from them" in sec, sec

    # Truncation says how much was cut AND how to get the rest — at the cut,
    # not only in the note below. Budget is recency-weighted: the newest reply
    # gets room to arrive whole, older ones stay one-liners.
    ex._delegations = [{"id": "d1", "agent_id": "a", "message": "q",
                        "reply": "x" * 3000, "ts": ""},
                       {"id": "d2", "agent_id": "b", "message": "q",
                        "reply": "y" * 3000, "ts": ""}]
    sec = ex._build_findings_section()
    assert "[cut at 800 of 3000 chars — recall_delegation id=d2 for the rest]" in sec, sec
    assert "[cut at 200 of 3000 chars — recall_delegation id=d1 for the rest]" in sec, sec
    assert len(sec) < 1600, len(sec)
    print("ok: findings section quotes the newest 3 verbatim, declares cap and truncation")


def test_findings_section_is_not_gated_on_memory():
    """Session state, not long-term memory: an agent that delegates needs it
    whether or not it keeps a memory (memory_enabled is False in _executor).

    The "not granted" half uses an agent with NO tools, because `call_agent`
    alone can no longer produce one (see the next case)."""
    ex = _executor([])                      # recall_delegation NOT granted
    assert ex.agent.memory_enabled is False
    ex._delegations = delegation_history(_session_with(1))
    sec = ex._build_findings_section()
    assert "[d1]" in sec and "finding number 1" in sec, sec
    # The pointer is only advertised when the tool is actually granted.
    assert "recall_delegation" not in sec, sec
    print("ok: findings survive memory_enabled=False; tool line only when granted")


def test_call_agent_implies_recall_delegation():
    """`call_agent` carries `recall_delegation` with it, in the registry.

    The UI pairs the two behind the *can delegate* switch, but that only
    reaches an agent someone opens and re-saves: every agent written before
    the pairing — and every agent file written by manage_agents or by hand —
    kept `call_agent` alone, and so delegated with a findings block that
    declares a truncation nothing can undo. The rule therefore lives in
    ToolRegistry.expand_tool_ids, where both _granted_tools() and the schemas
    sent to the model read it.
    """
    ex = _executor(["call_agent"])
    assert ex.agent.tools == ["call_agent"], "the stored grant is left alone"
    assert "recall_delegation" in ex._granted_tools()

    # Not just claimed: the model is actually sent the schema, or the prompt
    # would advertise a tool whose call comes back "unknown tool".
    names = {d["id"] for d in
             ex.tool_registry.get_definitions_for_agent(ex.agent.tools)}
    assert names == {"call_agent", "recall_delegation"}, names

    # And the findings block offers the way to the full text.
    ex._delegations = delegation_history(_session_with(1))
    assert "recall_delegation" in ex._build_findings_section()

    # It works the other way round too: holding both explicitly is unchanged,
    # and the implied id is never listed twice.
    both = ex.tool_registry.expand_tool_ids(["call_agent", "recall_delegation"])
    assert both == ["call_agent", "recall_delegation"], both

    # An agent that does NOT delegate gains nothing.
    assert "recall_delegation" not in _executor(["shell_exec"])._granted_tools()
    print("ok: call_agent implies recall_delegation without rewriting the agent")


async def test_recall_tool():
    ex = _executor(["call_agent", "recall_delegation"])

    # Empty branch: a refusal that asks for a retry in THIS turn.
    out = await recall_delegation_handler(executor=ex)
    assert out.startswith("NOTHING RECALLED"), out
    assert "call_agent" in out, out

    ex._delegations = delegation_history(_session_with(12))

    idx = await recall_delegation_handler(executor=ex)
    assert "8 of 12" in idx, idx                    # cap declared
    assert idx.index("d12") < idx.index("d11"), idx  # newest first
    assert "d4" not in idx, idx
    assert len(idx) < 1600, len(idx)

    full = await recall_delegation_handler(id="d7", executor=ex)
    assert full.startswith("d7 |"), full
    assert "web-researcher" in full and "finding number 7" in full, full
    assert "question 7" in full, full

    bad = await recall_delegation_handler(id="d99", executor=ex)
    assert bad.startswith("NOTHING RECALLED") and "d1, d2" in bad, bad

    ex._delegations.append({"id": "d13", "agent_id": "librarian", "message": "q",
                            "reply": "from the library", "ts": "2026-08-19T21:00:00"})
    filtered = await recall_delegation_handler(agent_id="librarian", executor=ex)
    assert "1 of 1" in filtered and "librarian" in filtered, filtered
    missing = await recall_delegation_handler(agent_id="coder", executor=ex)
    assert missing.startswith("NOTHING RECALLED") and "coder" in missing, missing

    # A long reply is cut HERE, with the cut declared — the registry's
    # max_output would only append "... [truncated]".
    ex._delegations = [{"id": "d1", "agent_id": "a", "message": "q",
                        "reply": "y" * 9000, "ts": ""}]
    long = await recall_delegation_handler(id="d1", executor=ex)
    assert "9000 chars in full" in long, long[-120:]
    print("ok: recall lists (cap declared), reads in full, filters, refuses with a retry")


async def test_cancellation():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    memory_compactor.schedule_background(slow(), key="compact:s1")
    await started.wait()
    assert memory_compactor.cancel_background("compact:s1") is True
    await asyncio.sleep(0)  # let the cancellation land
    assert memory_compactor._BG_BY_KEY.get("compact:s1") is None
    # Cancelling twice, or an unknown key, is a no-op and never raises.
    assert memory_compactor.cancel_background("compact:s1") is False
    assert memory_compactor.cancel_compaction("nope") is False

    # CancelledError must NOT be swallowed by compact_session's catch-all, or
    # cancellation would be cooperative in name only.
    src = (_ROOT / "server/app/engine/memory_compactor.py").read_text()
    assert "except asyncio.CancelledError" not in src, src
    print("ok: a background compaction is cancellable and stays cancellable")


def test_call_sites():
    """Four turn paths own a session; each must hand the history in AND cancel
    a running compaction. A forgotten kwarg fails nowhere at runtime: the
    findings section would just be empty forever.

    The autonomy path is the exception, and deliberately so: a wake quotes the
    previous wakes only as far as AutonomousConfig.history_messages allows (see
    test_wake_findings_follow_the_history_window), so it holds the call in a
    local instead of inlining it. It still has to pass the kwarg.
    """
    for rel, runs in (("server/app/routers/chat.py", 2),
                      ("server/app/engine/channel_turn.py", 1)):
        src = (_ROOT / rel).read_text()
        assert len(re.findall(r"delegations=delegation_history\(session\)", src)) == runs, rel
        assert len(re.findall(r"cancel_compaction\(", src)) == runs, rel

    auto = (_ROOT / "server/app/engine/autonomy.py").read_text()
    assert re.search(r"delegations=\w+", auto), auto[-2000:]
    assert len(re.findall(r"cancel_compaction\(", auto)) == 1

    # A sub-agent starts empty: between agents only message/reply travel.
    sub_src = (_ROOT / "server/app/tools/internal.py").read_text()
    call = sub_src[sub_src.index("sub_executor.run("):]
    assert "delegations" not in call[:400], call[:400]
    print("ok: all four turn paths pass the history and cancel compaction")


def test_wake_findings_follow_the_history_window():
    """On an autonomous wake the two blocks obey AutonomousConfig.history_messages.

    They restore exactly what is_scaffolding_message strips out of the message
    window, so they have to share its bound. Built from the whole session file
    instead, they let the previous wakes back in through a side door: a recurring
    task is self-similar, so the newest entries are always the SAME task's last
    run, and "## Agent findings" then offers yesterday's answer under a note that
    says to answer from it rather than admit no information. Observed
    2026-08-29..09-04 on a daily weather task: a forecast for a date four days
    stale, re-sent to two people every morning.
    """
    s = _session_with(3)

    # The trap the call site has to work around: limit=0 is FALSY, so asking for
    # "no history" through the limit argument returns ALL of it.
    assert len(delegation_history(s, 0)) == 3
    assert len(delegation_history(s, 2)) == 2

    auto = (_ROOT / "server/app/engine/autonomy.py").read_text()
    assert "delegation_history(session, hist) if hist > 0 else []" in auto, auto
    assert "tool_history(session, hist) if hist > 0 else []" in auto, auto
    assert "hist = cfg.history_messages" in auto, auto

    # The interactive paths are NOT windowed: there the history is the chat the
    # user can see, and dropping it is the bug this whole file is about.
    for rel in ("server/app/routers/chat.py", "server/app/engine/channel_turn.py"):
        src = (_ROOT / rel).read_text()
        assert "delegation_history(session)" in src, rel
        assert "history_messages" not in src, rel
    print("ok: a wake quotes previous wakes only within history_messages")


if __name__ == "__main__":
    test_history_extraction_and_stable_ids()
    test_findings_section()
    test_findings_section_is_not_gated_on_memory()
    test_call_agent_implies_recall_delegation()
    asyncio.run(test_recall_tool())
    asyncio.run(test_cancellation())
    test_call_sites()
    test_wake_findings_follow_the_history_window()
    print("all tests passed")
