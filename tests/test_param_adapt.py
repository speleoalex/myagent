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
     without touching tools.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine.llm_provider import LLMProvider, _PARAM_FIXES   # noqa: E402
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
