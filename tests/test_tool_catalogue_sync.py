#!/usr/bin/env python3
"""manage_tools and manage_agents duplicate the tool catalogue — prove they agree.

The two are separate tool folders, and editing a native tool copies ONE leaf
folder into the user layer (copy-on-write), so a module shared between them
would break the moment either is overridden. The duplication is therefore
deliberate — the same reason the library/* helpers are duplicated — and the
risk it carries is drift.

What drift costs here is milder than in the library (nothing computes an id),
but it is still a contradiction the user has to debug: manage_tools lists a
tool that manage_agents then calls unknown, or vice versa. And the pressure to
drift is higher, because these helpers evolve with the registry's grouping
rules.

Compared as CODE: docstrings and comments are stripped first, so each copy
stays free to describe itself in its own words ("an override copy of
manage_tools" vs "of manage_agents").

The declaration is INVERTED with respect to test_library_helpers_sync.py: there
the shared helpers are the majority and the exceptions are listed; here they are
a handful inside two otherwise unrelated tools, so only SHARED_FUNCS is
declared and everything else is free to differ.

A third copy of parse_category_wildcard lives in the registry. It is checked
too, because the rule it encodes is the load-bearing one: **the tool writes an
agent file that the registry reads back, so both must agree on what `<group>/*`
means.** A grant this tool accepts and the registry then ignores is a silently
tool-less agent.

Run:  python3 tests/test_tool_catalogue_sync.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "server" / "tools" / "manage_tools" / "run"
AGENTS = ROOT / "server" / "tools" / "manage_agents" / "run"
REGISTRY = ROOT / "server" / "app" / "tools" / "registry.py"

# The catalogue layer, duplicated verbatim. These decide WHICH tools exist and
# which group each belongs to — the answer both tools give the model.
SHARED_FUNCS = [
    "myagent_home",   # where the runtime layout is rooted
    "tools_dir",      # the user layer
    "bundled_dir",    # the native layer (the one the old bug ignored entirely)
    "_walk_layer",    # flat-then-groups, one level deep, flat wins a duplicate
    "catalogue",      # the overlay itself: user copy wins, bundle owns category
]


def _strip_docstrings(node):
    """Drop docstring expressions everywhere in the tree (comments are already
    gone: they never reach the AST)."""
    for n in ast.walk(node):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module)):
            continue
        body = getattr(n, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            # Never leave an empty body behind (a docstring-only function).
            n.body = body[1:] or [ast.Pass()]
    return node


def _normalize(node):
    """Code of one def, without its docstring or decorators. Decorators go
    because the registry's copy is a @staticmethod on a class and the tools'
    are plain module functions — an irrelevant difference here."""
    node = _strip_docstrings(node)
    node.decorator_list = []
    return ast.unparse(node)


def _top_level(path):
    """{name: normalized code} for top-level defs. The files have no .py
    extension (they are tool `run` scripts), which changes nothing for ast."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: _normalize(n) for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _method(path, class_name, func_name):
    """One method out of a class, normalized the same way."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == class_name:
            for n in cls.body:
                if isinstance(n, ast.FunctionDef) and n.name == func_name:
                    return _normalize(n)
    return None


def main():
    tools, agents = _top_level(TOOLS), _top_level(AGENTS)
    failures = []

    for name in SHARED_FUNCS:
        a, b = tools.get(name), agents.get(name)
        if a is None or b is None:
            missing = "manage_tools" if a is None else "manage_agents"
            failures.append(
                f"{name}: missing from {missing} — if it was renamed or removed, "
                f"do the same in the other tool and update SHARED_FUNCS here")
        elif a != b:
            failures.append(
                f"{name}: DIFFERS between manage_tools and manage_agents. The two "
                f"would then disagree on which tools exist or which group they are "
                f"in. Port the change to both copies.")

    # The third copy. Compared on the FUNCTION BODY only: the tools take a
    # plain argument, the registry's is a @staticmethod, so signatures differ
    # by `self`-less framing but the rule must not.
    reg = _method(REGISTRY, "ToolRegistry", "parse_category_wildcard")
    own = agents.get("parse_category_wildcard")
    if reg is None:
        failures.append(
            "parse_category_wildcard: not found as a ToolRegistry method — if it "
            "moved, point this test at its new home")
    elif own is None:
        failures.append("parse_category_wildcard: missing from manage_agents")
    elif reg != own:
        failures.append(
            "parse_category_wildcard: manage_agents and ToolRegistry disagree on "
            "what '<group>/*' means. manage_agents writes the agent file that the "
            "registry reads back, so a grant one accepts and the other ignores "
            "produces an agent with silently missing tools.")

    if failures:
        print(f"FAIL — the tool catalogue has drifted ({len(failures)} problem(s)):\n")
        for f in failures:
            print(f"  * {f}")
        print("\nSee the note at the top of both tools' module docstrings.")
        return 1
    print(f"OK — {len(SHARED_FUNCS)} duplicated helpers agree between manage_tools "
          f"and manage_agents, and the '<group>/*' rule matches ToolRegistry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
