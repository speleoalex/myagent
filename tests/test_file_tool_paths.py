#!/usr/bin/env python3
"""The file_management tools must expand a leading '~' before touching disk.

The registry runs every tool with cwd = the workspace, so a "~/myagent/..."
path — which small models produce constantly, because their prompt names the
workspace that way — was taken as RELATIVE and mkdir(parents=True) materialized
a directory called literally '~' (seen in production 2026-08-10:
workspace/~/myagent/workspace/itinerario/). No error, and the success line even
printed a resolved path, so nothing looked wrong.

The fix is four lines in four leaf folders (the CoW override copies ONE folder,
so a shared helper would break at the first override) — i.e. exactly the kind of
duplication that drifts. Hence this guard. It asserts BEHAVIOUR, not text:

  1. a leading '~' resolves to $HOME, in every tool
  2. a relative path still resolves against the cwd (no '~' logic side effects)
  3. an unresolvable '~user' degrades to the literal name instead of raising
     RuntimeError out of the tool — a traceback is not something a model can act on

Run: python3 tests/test_file_tool_paths.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "server/tools/file_management"
FAILURES: list[str] = []


def run(tool: str, args: dict, cwd: str, home: str) -> str:
    """Invoke a tool the way the registry does: JSON on stdin, cwd = workspace."""
    env = {**os.environ, "HOME": home}
    p = subprocess.run([str(TOOLS / tool / "run")], input=json.dumps(args),
                       capture_output=True, text=True, cwd=cwd, env=env, timeout=30)
    return (p.stdout + p.stderr).strip()


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        work = os.path.join(tmp, "workspace")
        os.makedirs(home)
        os.makedirs(work)

        print("1. a leading '~' resolves to $HOME, not to a directory named '~'")
        run("file_write", {"path": "~/deep/probe.txt", "content": "ciao"}, work, home)
        check("file_write lands in $HOME",
              os.path.isfile(os.path.join(home, "deep/probe.txt")))
        check("no '~' directory left in the workspace",
              not os.path.exists(os.path.join(work, "~")),
              str(list(Path(work).iterdir())))

        out = run("file_read", {"path": "~/deep/probe.txt"}, work, home)
        check("file_read reads back what file_write wrote", out == "ciao", out)

        run("file_append", {"path": "~/deep/probe.txt", "content": "+piu"}, work, home)
        check("file_append appends in $HOME",
              Path(home, "deep/probe.txt").read_text() == "ciao+piu")

        run("make_dir", {"path": "~/deep/sub"}, work, home)
        check("make_dir creates in $HOME", os.path.isdir(os.path.join(home, "deep/sub")))
        check("still no '~' directory in the workspace",
              not os.path.exists(os.path.join(work, "~")))

        print("2. a relative path still resolves against the workspace")
        run("file_write", {"path": "rel/x.txt", "content": "rel"}, work, home)
        check("file_write keeps relative paths in the cwd",
              os.path.isfile(os.path.join(work, "rel/x.txt")))
        check("and does NOT put them in $HOME",
              not os.path.exists(os.path.join(home, "rel")))

        print("3. an unresolvable '~user' stays literal, and never raises")
        for tool, args in (("file_write", {"path": "~nosuchuser42/a.txt", "content": "x"}),
                           ("file_append", {"path": "~nosuchuser42/b.txt", "content": "x"}),
                           ("make_dir", {"path": "~nosuchuser42/c"})):
            out = run(tool, args, work, home)
            check(f"{tool} degrades without a traceback",
                  "Traceback" not in out and "RuntimeError" not in out, out)
        check("the literal '~nosuchuser42' name is used as-is",
              os.path.isfile(os.path.join(work, "~nosuchuser42/a.txt")))

    if FAILURES:
        print("\nFAILED:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("\nOK — the four file_management tools expand '~' and keep relative "
          "paths in the workspace.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
