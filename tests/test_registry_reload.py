#!/usr/bin/env python3
"""The registry's hot reload, and the one thing mtime cannot see.

The tool catalogue is a live overlay of two folders, rescanned at most once per
``_SCAN_TTL`` and re-parsing only the ``tool.json`` whose mtime changed. Write
paths say "I changed something" with ``mark_dirty()`` instead of waiting out the
TTL.

mtime alone is not enough, and the gap is deliberate to test: a file rewritten
twice within the filesystem's timestamp granularity keeps its old mtime, so the
"unchanged" test passes on a file that changed. That is not hypothetical — it is
what made tests/test_lazy_tool_activation.py fail intermittently on one machine
and not on another, because the second write landed in the same tick as the
first.

``mark_dirty()`` therefore clears the mtime caches. This file pins that: the
same mtime is forced with ``os.utime`` so the tool case fails deterministically
— without the fix the second write is invisible — instead of depending on how
fast the filesystem stamps. The ``group.json`` case is the same guard for the
other cache, and passes on a filesystem whose float mtime does not round-trip;
it exists so the fix cannot be undone later without a red test somewhere.

Run:  server/.venv/bin/python tests/test_registry_reload.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-registry-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.tools.registry import ToolRegistry  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def _write_tool(folder: Path, description: str, *, mtime: float | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    tool_json = folder / "tool.json"
    tool_json.write_text(json.dumps({
        "name": "Widget", "description": description,
        "parameters": {"type": "object", "properties": {}},
    }))
    run = folder / "run"
    run.write_text("#!/bin/sh\necho ok\n")
    run.chmod(0o755)
    if mtime is not None:
        os.utime(tool_json, (mtime, mtime))
    return tool_json


def case_second_write_same_mtime_is_seen():
    print("a tool.json rewritten within one mtime tick is re-read after mark_dirty")
    user = Path(_TMP) / "user"
    tool_json = _write_tool(user / "widget", "the first description")
    stamp = tool_json.stat().st_mtime

    registry = ToolRegistry(user)
    check(registry.get_definition("widget")["description"] == "the first description",
          "the first version is loaded")

    _write_tool(user / "widget", "the second description", mtime=stamp)
    check(registry.get_definition("widget")["description"] == "the first description",
          "before mark_dirty the mtime cache still answers (the debounce, as designed)")

    registry.mark_dirty()
    check(registry.get_definition("widget")["description"] == "the second description",
          "after mark_dirty the second version wins, same mtime or not")


def case_group_rewritten_same_mtime_is_seen():
    print("the same for group.json")
    user = Path(_TMP) / "user2"
    _write_tool(user / "gadgets" / "widget", "does widget things")
    group = user / "gadgets" / "group.json"
    group.write_text(json.dumps({"name": "Gadgets", "description": "v1"}))
    stamp = group.stat().st_mtime

    registry = ToolRegistry(user)
    check(registry.group_meta("gadgets") == {"name": "Gadgets", "description": "v1"},
          "the first group description is loaded")

    group.write_text(json.dumps({"name": "Gadgets", "description": "v2"}))
    os.utime(group, (stamp, stamp))
    registry.mark_dirty()
    check((registry.group_meta("gadgets") or {}).get("description") == "v2",
          "after mark_dirty the rewritten group.json is re-read")


def case_a_real_mtime_change_still_reloads():
    print("a plain mtime change needs no mark_dirty (hot reload)")
    user = Path(_TMP) / "user3"
    tool_json = _write_tool(user / "widget", "old")
    registry = ToolRegistry(user)
    registry._last_scan = 0.0          # stand in for the TTL expiring
    _write_tool(user / "widget", "new")  # different mtime, no mark_dirty
    check(registry.get_definition("widget")["description"] == "new",
          "a new mtime alone is enough")


if __name__ == "__main__":
    for case in (case_second_write_same_mtime_is_seen,
                 case_group_rewritten_same_mtime_is_seen,
                 case_a_real_mtime_change_still_reloads):
        case()
    if failures:
        print(f"\n{len(failures)} FAILED")
        sys.exit(1)
    print("\nall ok")
