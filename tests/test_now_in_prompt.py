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
  6. The date is computed in the agent's OWN zone, falling back to the install's
     and then to the machine's. The host zone is an accident of the image the
     server was built from — the production node runs Etc/UTC while the team it
     answers is on Europe/Rome — so for the three hours before midnight the
     "correct" date injected there is yesterday's. A name the machine cannot
     resolve degrades to the host zone and logs; it is the SAVE path that
     refuses it, so one typo in a config file cannot take an agent off the air.
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

from app import config                                            # noqa: E402
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
    """Not frozen at import: the process outlives the day it started in.

    The clock reading moved into config.now_in_timezone when the zone became
    configurable, so both halves are checked — the block must CALL the resolver
    per turn, and the resolver must read the clock rather than a module-level
    constant."""
    src = Path(ROOT / "server" / "app" / "engine" / "executor.py").read_text()
    body = src.split("def _build_now_section", 1)[1].split("\n    def ", 1)[0]
    assert "config.now_in_timezone(" in body, "the day must be read per turn"
    src = Path(ROOT / "server" / "app" / "config.py").read_text()
    body = src.split("def now_in_timezone", 1)[1].split("\ndef ", 1)[0]
    assert "datetime.now(" in body


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


def test_the_zone_is_the_agents_own():
    """Two agents on one server, two clocks. The point of the field: the same
    install answers a team in Rome and a job reporting to a US account, and
    they disagree about what "yesterday" is for six hours a day."""
    rome = section(date_in_prompt=True, time_in_prompt=True, timezone="Europe/Rome")
    utc = section(date_in_prompt=True, time_in_prompt=True, timezone="UTC")
    assert rome != utc, "the zone must reach the rendered block"
    assert "CEST" in rome or "CET" in rome
    assert "UTC" in utc
    # And the date itself, not only the printed abbreviation: a zone that only
    # relabelled the hour would still hand over the wrong day in the evening.
    auckland = section(date_in_prompt=True, timezone="Pacific/Auckland")
    honolulu = section(date_in_prompt=True, timezone="Pacific/Honolulu")
    assert auckland != honolulu


def test_the_zone_falls_back_agent_then_settings_then_machine():
    assert config.timezone_name("Europe/Rome") == "Europe/Rome"
    saved = config.settings.timezone
    try:
        config.settings.timezone = "America/New_York"
        assert config.timezone_name("Europe/Rome") == "Europe/Rome", "the agent wins"
        assert config.timezone_name("") == "America/New_York", "then the install"
    finally:
        config.settings.timezone = saved
    assert config.timezone_name("") == "", "empty = the machine's, not a guess"
    # Empty resolves to an AWARE now: a naive one prints no %Z, and an hour with
    # no zone beside it is worse than no hour at all.
    assert config.now_in_timezone("").tzinfo is not None


def test_an_unresolvable_name_degrades_instead_of_killing_the_turn():
    """A hand-edited config must not take the agent off the air. The API is
    where a bad name is refused (test_it_is_refused_on_save)."""
    block = section(date_in_prompt=True, timezone="Mars/Olympus")
    assert prompts.NOW_NOTE in block, "the block still renders"
    assert config.is_valid_timezone("Mars/Olympus") is False
    assert config.is_valid_timezone("Europe/Rome") is True
    assert config.is_valid_timezone("") is True, "empty means 'the machine', not missing"


def test_it_is_refused_on_save():
    """Both write paths check, because both would otherwise store a name that
    silently resolves to the host zone — a confidently wrong date, which is the
    exact failure this feature exists to prevent, just moved."""
    agents = Path(ROOT / "server" / "app" / "routers" / "agents.py").read_text()
    assert agents.count("_check_timezone(agent)") == 2, "POST and PUT"
    assert "is_valid_timezone" in agents
    system = Path(ROOT / "server" / "app" / "routers" / "system.py").read_text()
    body = system.split("async def update_settings", 1)[1]
    assert "is_valid_timezone" in body


def test_the_default_is_inherit_everywhere():
    assert Agent(id="a", name="A", model_id="m").timezone == ""
    from app.models import Settings
    assert Settings().timezone == ""


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
