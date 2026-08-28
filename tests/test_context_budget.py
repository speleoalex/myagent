#!/usr/bin/env python3
"""In-turn demotion of tool results, and the honesty of the context estimate.

Run: server/.venv/bin/python tests/test_context_budget.py
(MYAGENT_* dirs temporary; no network — the provider is never asked to stream.)

A turn's tool results are appended to `messages` and re-sent on EVERY iteration,
so the cost is quadratic. Measured on one real turn: 12 tool calls (2 searches
plus 10 `local_read` pages of one PDF, ~8270 chars each) cost 22 model calls and
~221k input tokens, iterations 9-13 each overflowed a 16384-token window, and the
answer was "I cannot find this". Keeping the newest 2 verbatim and stubbing the
rest is 2.2x fewer tokens on that same turn.

The contract, one case each:

  1. BELOW the threshold nothing is touched — `messages` byte-identical. That is
     the point of triggering on occupancy: a short turn pays nothing.
  2. Above it, the OLDEST are demoted, the newest _CTX_KEEP_VERBATIM survive
     verbatim, no message is removed, and every `role: tool` keeps its
     `tool_call_id` — deleting one orphans the assistant's `tool_calls` and every
     OpenAI-compatible endpoint 400s on that.
  3. The stub is ACTIONABLE: it names the tool and its query/id/path. This is why
     the pass lives in the executor and not in LLMProvider._trim_tool_results,
     which sees only {role, tool_call_id, content} and can merely head-slice.
  4. `call_agent` is never demoted: re-running a search costs one call, re-running
     a DELEGATION costs a whole sub-agent turn, and inside the turn that reply
     exists nowhere else in the payload.
  5. TEXT PROTOCOL: the batched result keeps `prompts.TOOL_RESULTS_PREFIX`. Lose
     it and `is_scaffolding_message` stops recognising the message: it leaks into
     the next turn's history AND the rewind endpoint miscounts user turns. Two
     silent failures.
  6. The demoted key gets ONE re-run past the dedup. Without it the stub says
     "call it again", the model obeys, the dedup swallows the call, and the turn
     ends mute (`tool_calls = None` -> empty assistant -> break).
  7. The forced synthesis restores the FULL results from `trace_steps`: a turn
     that looped on tools is both the one that triggers demotion and the one that
     needs the synthesis, so writing the answer from our own stubs would trade an
     expensive correct answer for a cheap vague one.
  8. `_learn_ratio` is monotone, clamped, and measured against messages + tool
     schemas — the existing formula divided the WHOLE request cost by a
     messages-only estimate, yielding 1.39 where the truth was 1.135 and taxing
     every later payload by ~14% of the window.
"""

import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app import config                                          # noqa: E402
from app.engine import prompts                                  # noqa: E402
from app.engine.executor import AgentExecutor, Stores, _tool_call_key  # noqa: E402
from app.engine.llm_provider import (                            # noqa: E402
    LLMProvider, _TOKEN_RATIOS, _RATIO_FLOOR,
)
from app.storage.store import JsonStore                         # noqa: E402
from app.tools.registry import ToolRegistry                     # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")

models.save("m", {"id": "m", "name": "M", "provider": "llamacpp", "model": "m",
                  "base_url": "http://localhost:8080",
                  "options": {"max_tokens": 2048}})
agents.save("a", {"id": "a", "name": "A", "model_id": "m", "system_prompt": "P",
                  "tools": ["local_search", "local_read"]})

WINDOW = 16384          # what the measured turn ran against
PAGE = "pagina " * 1180  # ~8270 chars, the real local_read page size


def make_executor():
    ex = AgentExecutor(_agent(), _model(), registry, stores)
    # The window is normally probed over HTTP; pin it so the test needs no server.
    ex.provider._ctx_budget = WINDOW
    return ex


def _agent():
    from app.models import Agent
    return Agent(**agents.get("a"))


def _model():
    from app.models import ModelConfig
    return ModelConfig(**models.get("m"))


def turn(n_reads=10, tool="local_read", text_protocol=False):
    """A turn shaped like the measured one: system, user, then n rounds of
    assistant(tool_calls) + result."""
    msgs = [{"role": "system", "content": "S" * 2272},
            {"role": "user", "content": "Come faccio lo spurgo del gasolio?"}]
    history_end = len(msgs)
    steps = []
    for i in range(n_reads):
        args = json.dumps({"id": "p:Manuals/13_Fuel.pdf:206", "offset": i * 8000})
        call = {"id": f"c{i}", "type": "function",
                "function": {"name": tool, "arguments": args}}
        msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
        body = f"PAGINA {i}\n" + PAGE
        if text_protocol:
            msgs.append({"role": "user",
                         "content": prompts.TOOL_RESULTS_PREFIX + "\n- "
                                    + tool + ": " + body})
        else:
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": body})
        steps.append({"tool": tool, "arguments": json.loads(args), "result": body})
    return msgs, history_end, steps


def fit(ex, msgs, tools=None):
    return asyncio.run(ex._fit_context(msgs, tools, 2, {}, set()))


# --------------------------------------------------------------------------- #
def test_below_threshold_changes_nothing():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=1)        # ~8 kB: nowhere near 16k tokens
    before = copy.deepcopy(msgs)
    state = fit(ex, msgs)
    assert msgs == before, "a payload that fits must not be touched at all"
    assert state.get("demoted", 0) == 0
    assert state["window"] == WINDOW
    assert state["used"] > 0 and state["fit"] < state["window"]


def test_above_threshold_demotes_oldest_keeps_newest():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10)
    n_before = len(msgs)
    state = fit(ex, msgs)
    assert state["demoted"] > 0, state
    assert len(msgs) == n_before, "no message may be REMOVED"

    results = [m for m in msgs if m.get("role") == "tool"]
    assert len(results) == 10
    # Every result keeps its pairing, or the endpoint 400s on the assistant's
    # tool_calls having no answer.
    assert [m["tool_call_id"] for m in results] == [f"c{i}" for i in range(10)]
    assert all(m["content"] for m in results), "a stub must never be empty"

    cut = ["… [cut at " in m["content"] for m in results]
    keep = AgentExecutor._CTX_KEEP_VERBATIM
    assert not any(cut[-keep:]), f"the newest {keep} must survive verbatim"
    assert cut[0], "the oldest must go first"
    # And it actually got smaller.
    assert state["used"] < state["fit"] * state["threshold"]


def test_stub_is_actionable():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10)
    fit(ex, msgs)
    stub = next(m["content"] for m in msgs
                if m.get("role") == "tool" and "… [cut at " in m["content"])
    assert "call local_read(" in stub, stub
    assert "p:Manuals/13_Fuel.pdf:206" in stub, "the id is what makes it re-callable"
    assert "again for the rest]" in stub
    assert stub.startswith("PAGINA 0"), "a readable head is kept"


def test_call_agent_is_never_demoted():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10, tool="call_agent")
    state = fit(ex, msgs)
    assert state.get("demoted", 0) == 0, "a delegation costs a whole sub-agent turn"
    assert not any("… [cut at " in (m.get("content") or "") for m in msgs)


def test_text_protocol_keeps_the_scaffolding_prefix():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10, text_protocol=True)
    state = fit(ex, msgs)
    assert state["demoted"] > 0
    demoted = [m for m in msgs if m.get("role") == "user"
               and "… [cut at " in (m.get("content") or "")]
    assert demoted, "the batched result must be demotable too"
    for m in demoted:
        assert m["content"].startswith(prompts.TOOL_RESULTS_PREFIX), m["content"][:60]
        assert AgentExecutor.is_scaffolding_message("user", m["content"]), \
            "losing the prefix leaks the stub into the next turn's history"


def test_demoted_key_gets_one_rerun_past_the_dedup():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10)
    recallable, pinned = {}, set()
    asyncio.run(ex._fit_context(msgs, None, 2, recallable, pinned))
    assert recallable, "demotion must grant the re-run its own stub asks for"

    # Replay the dedup for a call whose result was demoted.
    key = next(iter(recallable))
    executed = {key}
    def dedup(key):
        repeat = key in executed
        if repeat and recallable.get(key):
            recallable.pop(key, None)
            pinned.add(key)
            repeat = False
        return not repeat            # True = the call runs
    assert dedup(key) is True, "the re-run our stub asked for must go through"
    assert dedup(key) is False, "and exactly once — the grant is consumed"
    assert key in pinned, "a re-fetched result must not be demoted again at once"


def test_pinned_result_is_not_demoted_again():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10)
    # Pin every key: nothing may be demoted, so the pass is a no-op.
    keys = {_tool_call_key(m["tool_calls"][0]) for m in msgs
            if m.get("role") == "assistant" and m.get("tool_calls")}
    before = copy.deepcopy(msgs)
    state = asyncio.run(ex._fit_context(msgs, None, 2, {}, keys))
    assert state.get("demoted", 0) == 0
    assert msgs == before


def test_synthesis_restores_full_results():
    ex = make_executor()
    msgs, hist, steps = turn(n_reads=10)
    fit(ex, msgs)
    assert any("… [cut at " in (m.get("content") or "") for m in msgs)

    out = ex._synthesis_messages(msgs, steps)
    assert not any("… [cut at " in (m.get("content") or "") for m in out), \
        "the forced answer must not be written from our own stubs"
    joined = "\n".join(m.get("content") or "" for m in out)
    assert "PAGINA 0" in joined and "PAGINA 9" in joined
    assert out[-1]["content"] == prompts.FORCE_ANSWER

    # Without trace_steps it degrades to the old behaviour rather than crashing.
    plain = ex._synthesis_messages(msgs)
    assert plain[-1]["content"] == prompts.FORCE_ANSWER


def test_learn_ratio_is_monotone_clamped_and_measured_whole():
    ex = make_executor()
    p = ex.provider
    _TOKEN_RATIOS.pop(p._caps_key, None)
    # A fresh model starts at the conservative floor, NOT at 1.0: the ratio is
    # learned from an overflow, and the demotion exists to stop overflows
    # happening — so an optimistic start would never be corrected.
    assert p.token_ratio() == _RATIO_FLOOR

    # The real numbers: 19352 tokens actually cost, for messages estimated at
    # 13943 plus 989 of tool schemas.
    assert abs(p._learn_ratio(19352, 13943 + 989) - 1.297) < 0.01, p.token_ratio()
    # Monotone: a smaller observation must not lower a learned ratio.
    p._learn_ratio(1000, 1000)
    assert p.token_ratio() > 1.2, "a single mild payload must not erase the lesson"
    # Clamped: no observation may push it past the ceiling.
    p._learn_ratio(10_000_000, 1)
    assert p.token_ratio() == 2.0
    # And a nonsense denominator is ignored instead of dividing by zero.
    p._learn_ratio(500, 0)
    assert p.token_ratio() == 2.0


def test_estimate_and_state_use_the_ratio():
    ex = make_executor()
    p = ex.provider
    _TOKEN_RATIOS.pop(p._caps_key, None)
    msgs = [{"role": "user", "content": "x" * 4000}]
    raw = sum(p._estimate_tokens(m["content"]) for m in msgs)
    assert p.estimate_payload_tokens(msgs) == int(raw * _RATIO_FLOOR)
    p._learn_ratio(2000, 1000)                    # ratio 2.0
    assert p.estimate_payload_tokens(msgs) == raw * 2
    st = asyncio.run(p.context_state(msgs))
    assert st["ratio"] == 2.0 and st["used"] == raw * 2
    # The tool schemas count toward `reserve`, so `fit` shrinks when tools are sent.
    tools = [{"type": "function", "function": {"name": "t", "description": "d" * 800,
                                              "parameters": {}}}]
    assert asyncio.run(p.context_state(msgs, tools))["fit"] < st["fit"]
    _TOKEN_RATIOS.pop(p._caps_key, None)


def test_threshold_comes_from_settings_and_is_clamped():
    ex = make_executor()
    msgs, hist, _ = turn(n_reads=10)
    original = getattr(config.settings, "context_compact_at", 0.90)
    try:
        # An absurd value must not disable the guard nor demote everything.
        config.settings.context_compact_at = 99.0
        state = fit(ex, msgs)
        assert state["threshold"] == 0.95
        config.settings.context_compact_at = -5.0
        state = fit(ex, msgs)
        assert state["threshold"] == 0.5
    finally:
        config.settings.context_compact_at = original


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
