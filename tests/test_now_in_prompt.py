#!/usr/bin/env python3
"""`Agent.date_in_prompt` / `time_in_prompt`: state what day it is, opt-in.

Run: server/.venv/bin/python tests/test_now_in_prompt.py
(MYAGENT_* dirs temporary; no network — nothing here streams.)

From a failure on 2026-09-22: a scheduled agent pulled a Float report for
2024-10-17 — its training prior — and mailed it titled with that date, every
number plausible and every one of them wrong. Nothing in the payload said what
day it was, and a model asked for "yesterday" does not report that it had to
guess. (The same turn also lost its mail to a flat output cap; that half lives
in test_context_budget.py.)

The contract:

  1. Off by default. An agent that never resolves a relative date pays nothing:
     the block is the empty string, not a shorter block.
  2. The date block carries the NOTE. Without it a model reads the line as
     decoration and still answers "yesterday" from the prior — the line alone was
     never the fix.
  3. The clock is a SECOND switch and rides on the first: time without date is
     inert. The two are separate because they cost differently — the date string
     is identical all day so the cached prefix survives, the time differs every
     turn and throws the cache (and llama.cpp's --cache-reuse) away with it.
  4. The date is resolved per TURN, not once at import: a server that stays up
     crosses midnight, and a date frozen at startup is wrong for every day after
     the first, in the one way nobody checks.
  5. It rides on `_system_suffix`, so the no-tools fallback — which rebuilds the
     system prompt mid-loop from `_system_prompt_with_tools` alone — keeps it.
     llama.cpp STARTS in that mode.
"""

import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine import prompts                                   # noqa: E402
from app.engine.executor import AgentExecutor                    # noqa: E402
from app.models import Agent                                     # noqa: E402


def section(**flags) -> str:
    """The block as the executor builds it, without standing up a turn."""
    agent = Agent(id="a", name="A", model_id="m", **flags)
    return AgentExecutor._build_now_section(SimpleNamespace(agent=agent))


def test_off_by_default():
    assert section() == "", "an agent that never asks for a date must pay nothing"
    assert Agent(id="a", name="A", model_id="m").date_in_prompt is False


def test_date_block_states_the_day_and_the_rule():
    block = section(date_in_prompt=True)
    today = datetime.now().astimezone()
    assert prompts.SECTION_NOW in block
    assert today.strftime("%Y-%m-%d") in block, "ISO, because that is what tools take"
    assert today.strftime("%A") in block
    # The load-bearing half: the line alone was never the fix.
    assert prompts.NOW_NOTE in block
    assert "YYYY-MM-DD" in block


def test_clock_is_a_second_switch_riding_on_the_first():
    date_only = section(date_in_prompt=True)
    assert "local time" not in date_only.lower()
    both = section(date_in_prompt=True, time_in_prompt=True)
    assert re.search(r"\b\d{2}:\d{2}\b", both), "the clock must actually appear"
    assert both.startswith(date_only[:len(prompts.SECTION_NOW) + 10])
    # Time alone is inert: a clock with no day does not answer "when is now",
    # and the UI unchecks it, so the engine must not be the only guard.
    assert section(time_in_prompt=True) == ""


def test_the_date_is_resolved_per_turn():
    """Not frozen at import: the process outlives the day it started in."""
    src = Path(ROOT / "server" / "app" / "engine" / "executor.py").read_text()
    body = src.split("def _build_now_section", 1)[1].split("\n    def ", 1)[0]
    assert "datetime.now()" in body, "the day must be read when the turn is built"


def test_the_block_is_a_constant_nobody_reformats():
    """The date goes through prompts.py like every other injected string, so a
    change to the wording is one edit and shows up in the trace verbatim."""
    assert prompts.NOW_DATE.format(date="2026-09-22", weekday="Tuesday") \
        == "Today is 2026-09-22 (Tuesday)."


def test_it_rides_on_the_system_suffix():
    """The no-tools fallback rebuilds the prompt from _system_prompt_with_tools
    alone and re-appends _system_suffix; anything built outside it is dropped
    mid-turn, silently, on exactly the provider that starts in that mode."""
    src = Path(ROOT / "server" / "app" / "engine" / "executor.py").read_text()
    prep = src.split("suffix = self._build_now_section()", 1)
    assert len(prep) == 2, "the block must be part of the turn suffix"
    assert "_system_suffix = suffix" in prep[1]


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
