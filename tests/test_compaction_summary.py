#!/usr/bin/env python3
"""Archived-turn summaries a small model can actually write.

PLAN-compaction-dream.md, part B. The first compaction with a large threshold
sent the whole head in ONE call: on a 4k model the input was truncated and the
summary covered part of it, silently. A summary cut by the output cap was
stored as if whole, and nothing checked that the numbers, URLs and names the
user gave survived.

Pinned here:

  * the transcript goes in pieces within half the summarizer's room, in
    order, each later call updating the summary so far;
  * a summary cut by the output cap is never stored: one retry on half-size
    pieces, then give up (retried next turn);
  * missing details trigger one corrective call; a summary still missing
    them is kept and flagged degraded, in the chunk file too;
  * the summary length follows the memory share.

Run:  server/.venv/bin/python tests/test_compaction_summary.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-summary-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine import memory_compactor as mc  # noqa: E402
from app.engine.llm_provider import OUTPUT_TRUNCATED  # noqa: E402
from app.models import ModelConfig  # noqa: E402
from app.storage.memory import MemoryStore  # noqa: E402

failures: list[str] = []
MODEL = ModelConfig(id="m", name="m", provider="llamacpp", base_url="http://127.0.0.1:9")


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def tokens(text: str) -> int:
    return len(text) // 4


class FakeProvider:
    """Answers from a script (a function of the call), records every call."""
    fit = 1000
    script = None
    calls: list[dict] = []

    def __init__(self, cfg):
        self.trace_label = ""
        self.reasoning = None

    async def context_state(self, messages, tools):
        return {"fit": FakeProvider.fit}

    def estimate_payload_tokens(self, messages, tools=None):
        return sum(tokens(str(m.get("content") or "")) for m in messages)

    async def chat_completion_stream(self, messages, tools, temperature):
        call = {"system": messages[0]["content"], "user": messages[1]["content"]}
        FakeProvider.calls.append(call)
        yield {"choices": [{"delta": {"content": FakeProvider.script(call, len(FakeProvider.calls))}}]}

    async def close(self):
        pass


mc.LLMProvider = FakeProvider


def run(cleaned, max_chars=600, fit=1000, script=None):
    FakeProvider.fit = fit
    FakeProvider.calls = []
    FakeProvider.script = script or (lambda call, n: f"Summary number {n} of the conversation.")
    return asyncio.run(mc.summarize(MODEL, cleaned, max_chars))


def dialogue(turns: int, size: int = 400) -> list[dict]:
    out = []
    for i in range(turns):
        out += [{"role": "user", "content": f"question {i} " + "q" * size},
                {"role": "assistant", "content": f"answer {i} " + "a" * size}]
    return out


def case_pieces():
    print("the transcript goes in pieces within half the room, in order")
    summary, degraded = run(dialogue(10), fit=1000)   # ~2000 tokens, budget 500
    calls = FakeProvider.calls
    check(len(calls) > 3, f"several calls: {len(calls)}")
    check(all(tokens(c["user"]) <= 500 for c in calls),
          f"each call within half the room: {max(tokens(c['user']) for c in calls)}")
    check(calls[0]["system"].startswith("Summarize")
          and all(c["system"].startswith("Update") for c in calls[1:]),
          "first call summarizes, the others update")
    check("Summary number 1" in calls[1]["user"], "each update carries the summary so far")
    check("question 0" in calls[0]["user"] and "answer 9" in calls[-1]["user"],
          "in order, nothing left out")
    check(summary == f"Summary number {len(calls)} of the conversation.",
          "the result is the last update")

    run(dialogue(2), fit=100_000)
    check(len(FakeProvider.calls) == 1, "a head that fits goes in one call")

    big = [{"role": "user", "content": "z" * 8000}]    # 2000 tokens, one message
    run(big, fit=1000)
    check(len(FakeProvider.calls) >= 4
          and all(tokens(c["user"]) <= 500 for c in FakeProvider.calls),
          "a single oversized message is cut into parts of its own")


def case_truncated():
    print("a cut summary is never stored")
    cut = lambda call, n: ("partial summary of things" + f"\n\n{OUTPUT_TRUNCATED} (2048)."
                           if n == 1 else f"Whole summary number {n} here.")
    # ~800 tokens: one piece in half of 2000, several in a quarter
    summary, _ = run(dialogue(4), fit=2000, script=cut)
    check(summary is not None and "Whole" in summary and "partial" not in summary,
          f"retried and replaced: {summary!r}")
    check(len(FakeProvider.calls) > 2
          and all(tokens(c["user"]) <= 500 for c in FakeProvider.calls[1:]),
          f"the retry used half-size pieces: {len(FakeProvider.calls)} calls")

    always_cut = lambda call, n: "partial summary of things" + f"\n\n{OUTPUT_TRUNCATED} (2048)."
    summary, _ = run(dialogue(4), fit=4000, script=always_cut)
    check(summary is None, "cut twice: nothing, retried next turn")


def case_details():
    print("details the user gave must survive")
    conv = [{"role": "user", "content": "Il codice è 48213, la pagina è "
                                        "https://example.org/a/b e il referente è Marco Rossi."},
            {"role": "assistant", "content": "Ricevuto."}]
    details = mc._details(conv)
    check("48213" in details and "https://example.org/a/b" in details and "Marco" in details,
          f"numbers, URLs and names found: {details}")
    check("Il" not in details, "a capital opening the sentence is not a name")
    check(mc._missing_details(["1200"], "costa 1.200 euro") == [],
          "thousand separators do not count as a loss")

    fixing = lambda call, n: ("Si parla di un codice." if n == 1 else
                              "Codice 48213, pagina https://example.org/a/b, referente Marco Rossi.")
    summary, degraded = run(conv, script=fixing)
    check(len(FakeProvider.calls) == 2 and "48213" in FakeProvider.calls[1]["system"]
          and "Marco Rossi" in FakeProvider.calls[1]["user"],
          "one corrective call, with the missing details and the user's lines")
    check("48213" in summary and not degraded, "fixed: kept, not degraded")

    stubborn = lambda call, n: "Si parla di un codice e di una pagina."
    summary, degraded = run(conv, script=stubborn)
    check(len(FakeProvider.calls) == 2, "only one corrective call")
    check(summary and degraded, "still missing: kept, flagged degraded")

    good = lambda call, n: "Codice 48213, pagina https://example.org/a/b, referente Marco Rossi."
    run(conv, script=good)
    check(len(FakeProvider.calls) == 1, "nothing missing: no extra call")

    store = MemoryStore(Path(_TMP) / "memory")
    cid = store.write_chunk("a", body="x", degraded=True)
    check(store.read_chunk("a", cid)["meta"].get("degraded") == "true",
          "the chunk file records degraded")
    cid = store.write_chunk("a", body="y")
    check("degraded" not in store.read_chunk("a", cid)["meta"], "... only when it is")


def case_summary_chars():
    print("the summary length follows the memory share")
    check(mc.summary_chars(0, 1.0) == mc.SUMMARY_MAX_CHARS, "share not measured: the ceiling")
    check(mc.summary_chars(300, 1.0) == 300, "4k window (share 300 tokens): 300 chars")
    check(mc.summary_chars(100, 1.0) == mc.SUMMARY_MIN_CHARS, "never below the floor")
    check(mc.summary_chars(4600, 1.2) == mc.SUMMARY_MAX_CHARS, "32k window: the ceiling")


case_pieces()
case_truncated()
case_details()
case_summary_chars()
print()
if failures:
    print(f"{len(failures)} FAILED"); sys.exit(1)
print("all passed")
