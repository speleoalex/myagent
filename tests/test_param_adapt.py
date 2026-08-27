#!/usr/bin/env python3
"""400-payload adaptation in LLMProvider — the reasoning_effort/tools case.

Run: server/.venv/bin/python tests/test_param_adapt.py
(MYAGENT_* dirs are temporary; no network is ever touched — _adapt_payload and
_build_payload are exercised directly.)

The contract, one case each:
  1. "Function tools with reasoning_effort are not supported ... set
     reasoning_effort to 'none'" -> reasoning_effort:"none" is ADDED and tools
     are KEPT (before the fix this dropped tools and downgraded the whole
     process to the text protocol — observed with gpt-5.6-luna, 2026-08-11);
  2. the fix is memoized process-wide: a NEW provider for the same endpoint
     builds tool payloads with reasoning_effort already applied, and does NOT
     start in text mode (no "tools" sentinel in _PARAM_FIXES);
  3. the add applies only next to `tools`: a plain payload stays untouched
     (forcing reasoning off on ordinary calls would degrade them);
  4. if the endpoint rejects the combination anyway (reasoning_effort already
     "none"), the SAME detail falls through to the no-tools branch instead of
     re-sending an identical payload forever;
  5. regression: a rejected sampling param (temperature) is still dropped
     without touching tools;
  6. a CONTEXT-OVERFLOW 400 is not a tools refusal: the window is learned from
     the refusal, tool results are trimmed oldest-first, tools survive and
     supports_tools is untouched. Before the fix the no-tools branch — which
     catches every unexplained 400 — swallowed it, downgraded the endpoint
     process-wide, failed again on the same overflow, and reported llama.cpp's
     complaint about the REBUILT message list ("Cannot have 2 or more assistant
     messages at the end of the list") instead of the real cause (observed
     2026-08-27, a librarian reading five pages of one PDF);
  7. with nothing left to trim the overflow returns False, so the real error
     surfaces instead of being traded for a misleading one;
  8. _sanitize_messages alternates assistant/user and never ends on an
     assistant message — llama.cpp refuses two trailing assistants outright and
     treats a single one as a prefill to CONTINUE, which would have the model
     extend its own tool-call transcript instead of answering.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine.llm_provider import (                            # noqa: E402
    LLMProvider, _PARAM_FIXES, _CTX_CAPS, _ctx_from_error,
)
from app.models import ModelConfig                              # noqa: E402

REASONING_DETAIL = (
    "Function tools with reasoning_effort are not supported for gpt-5.6-luna "
    "in /v1/chat/completions. To use function tools, use /v1/responses or set "
    "reasoning_effort to 'none'."
)
TEMPERATURE_DETAIL = (
    "Unsupported value: 'temperature' does not support 0.1 with this model. "
    "Only the default (1) value is supported."
)

TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


def make_provider(model: str) -> LLMProvider:
    # A distinct model id per test = a distinct process-wide memo bucket.
    return LLMProvider(ModelConfig(
        id="openai-test", name="T", provider="openai", model=model,
        base_url="http://memo-bucket.invalid/v1", api_key="k",
    ))


def test_reasoning_effort_added_tools_kept():
    p = make_provider("m1")
    payload = {"model": "m1", "messages": [], "tools": list(TOOLS),
               "tool_choice": "auto"}
    assert p._adapt_payload(payload, REASONING_DETAIL) is True
    assert payload["reasoning_effort"] == "none", payload
    assert "tools" in payload, "tools must be kept, not dropped"
    assert p.supports_tools is not False, "must not downgrade to text protocol"


def test_fix_memoized_for_next_provider():
    p = make_provider("m2")
    p._adapt_payload({"tools": list(TOOLS)}, REASONING_DETAIL)

    fresh = make_provider("m2")
    assert fresh.supports_tools is None, "no tools sentinel must be recorded"
    key = (fresh.config.base_url, fresh.config.model)
    assert "tools" not in _PARAM_FIXES.get(key, {})
    payload = fresh._build_payload([], list(TOOLS), None, stream=True,
                                   max_ctx=8192)
    assert payload.get("reasoning_effort") == "none", payload
    assert "tools" in payload


def test_add_only_next_to_tools():
    p = make_provider("m3")
    p._adapt_payload({"tools": list(TOOLS)}, REASONING_DETAIL)
    plain = p._build_payload([], None, None, stream=True, max_ctx=8192)
    assert "reasoning_effort" not in plain, plain


def test_repeated_rejection_falls_to_no_tools():
    p = make_provider("m4")
    payload = {"messages": [], "tools": list(TOOLS), "tool_choice": "auto"}
    assert p._adapt_payload(payload, REASONING_DETAIL) is True
    # Same refusal again: the payload must CHANGE (no infinite identical retry).
    assert p._adapt_payload(payload, REASONING_DETAIL) is True
    assert "tools" not in payload
    assert p.supports_tools is False


def test_sampling_param_still_dropped():
    p = make_provider("m5")
    payload = {"messages": [], "tools": list(TOOLS), "temperature": 0.1}
    assert p._adapt_payload(payload, TEMPERATURE_DETAIL) is True
    assert "temperature" not in payload
    assert "tools" in payload
    assert p.supports_tools is not False


CTX_DETAIL_LLAMACPP = ("request (17450 tokens) exceeds the available context "
                       "size (16384 tokens), try increasing it")
CTX_DETAIL_OPENAI = ("This model's maximum context length is 8192 tokens. "
                     "However, your messages resulted in 8500 tokens. "
                     "Please reduce the length of the messages.")


def _turn_with_pages(pages: int, chars: int = 12000) -> list[dict]:
    """A librarian turn: one search plus `pages` local_read calls on one PDF."""
    msgs = [{"role": "system", "content": "S" * 3000},
            {"role": "user", "content": "Come faccio lo spurgo del gasolio?"}]
    for n in range(pages):
        msgs += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{n}", "type": "function",
                 "function": {"name": "local_read",
                              "arguments": '{"offset": %d}' % (n * 8000)}}]},
            {"role": "tool", "tool_call_id": f"c{n}",
             "content": f"PAGE {n} " + "x" * chars},
        ]
    return msgs


def _est(p: LLMProvider, messages: list[dict]) -> int:
    return sum(p._estimate_tokens(m.get("content") or "") for m in messages)


def test_ctx_overflow_is_parsed_both_ways():
    # Each provider states the pair in the opposite order; a non-overflow pair
    # and an unrelated refusal must not be read as one.
    assert _ctx_from_error(CTX_DETAIL_LLAMACPP) == (17450, 16384)
    assert _ctx_from_error(CTX_DETAIL_OPENAI) == (8500, 8192)
    assert _ctx_from_error(TEMPERATURE_DETAIL) is None
    assert _ctx_from_error("request (100 tokens) exceeds the available "
                           "context size (200 tokens)") is None


def test_ctx_overflow_keeps_tools_and_trims_results():
    p = make_provider("m6")
    payload = {"model": "m6", "messages": _turn_with_pages(5),
               "tools": list(TOOLS), "tool_choice": "auto", "max_tokens": 2048}
    before = _est(p, payload["messages"])

    assert p._adapt_payload(payload, CTX_DETAIL_LLAMACPP) is True
    assert "tools" in payload, "a context overflow says nothing about tools"
    assert p.supports_tools is not False, "must not downgrade to text protocol"
    assert _PARAM_FIXES[p._caps_key].get("tools", "absent") == "absent"
    assert _CTX_CAPS[p._caps_key] == 16384, "the named window must be learned"
    assert p._ctx_budget == 16384

    after = _est(p, payload["messages"])
    assert after < before, (before, after)

    cut = [m for m in payload["messages"] if "cut to fit" in (m.get("content") or "")]
    assert cut, "a trim must DECLARE itself"
    tools_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
    assert "cut to fit" not in (tools_msgs[-1].get("content") or ""), \
        "the newest result is what the model reasons from: trim it last"
    assert "cut to fit" in (tools_msgs[0].get("content") or ""), "oldest first"


def test_ctx_overflow_with_nothing_to_trim_surfaces_the_real_error():
    p = make_provider("m7")
    # No tool results and a system prompt that is already the whole payload:
    # there is nothing this branch can shrink.
    payload = {"model": "m7", "messages": [{"role": "system", "content": "s"}],
               "tools": list(TOOLS), "tool_choice": "auto"}
    assert p._adapt_payload(payload, CTX_DETAIL_LLAMACPP) is False
    assert "tools" in payload, "and it must not fall through to the no-tools branch"
    assert p.supports_tools is not False


def test_sanitized_history_never_ends_on_assistant():
    out = LLMProvider._sanitize_messages(_turn_with_pages(5, chars=20))
    roles = [m["role"] for m in out]
    assert roles[-1] == "user", roles
    doubles = [i for i in range(1, len(roles))
               if roles[i] == roles[i - 1] == "assistant"]
    assert not doubles, f"consecutive assistant messages at {doubles}: {roles}"
    # The call stays on the assistant side, the result moves to the user side —
    # the same shape the executor's own text protocol builds.
    assert out[2]["content"].startswith("[Called tool"), out[2]
    assert out[3]["content"].startswith("TOOL RESULTS:"), out[3]


def test_merge_collapses_a_double_assistant():
    # A forced synthesis appended after a stopped turn can arrive already doubled.
    out = LLMProvider._sanitize_messages([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "part one"},
        {"role": "assistant", "content": "part two"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant"], out
    assert out[-1]["content"] == "part one\n\npart two"


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
