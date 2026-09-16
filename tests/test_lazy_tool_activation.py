"""`Agent.lazy_tools`: send the tool SCHEMAS only once the model asks for them.

Every iteration of a turn resends the whole schema array, and
`LLMProvider.context_state` charges it twice, to `used` AND to `reserve`: the
schemas are the fixed cost of a turn and the part that grows without carrying
new information. Measured over the 29 bundled tools the full schemas are ~7.9k
tokens, 63% of it the `parameters` block — the half that only matters at the
instant a tool is called.

With the flag on the turn opens with a catalogue of activatable categories plus
the single `activate_tools` schema; the real schemas arrive when the model
switches a category on and stay for the rest of the turn.

The load-bearing property, and the reason most cases below exist: this is a
VISIBILITY filter, never a capability one. `ToolRegistry.execute` is not gated
by the definitions, `_granted_tools()` keeps returning the full expanded grant,
and a call to a tool the agent holds but has not loaded is honoured. A mistake
costs a round trip, never a capability.

Run with the server venv (the app imports pydantic/httpx):

    server/.venv/bin/python tests/test_lazy_tool_activation.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-test-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.executor import AgentExecutor, Stores, directory_entry  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools.internal import activate_tools_handler  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BUNDLED = _ROOT / "server" / "tools"

# A grant that spans the three entry kinds the catalogue knows: two whole
# groups, each speaking through its own `group.json`, and one flat tool.
GRANT = ["file_management/*", "memory/*", "shell_exec"]


# --------------------------------------------------------------- fixtures

def _registry(user_dir: Path | None = None) -> ToolRegistry:
    return ToolRegistry(user_dir or (Path(_TMP) / "tools"), bundled_dir=BUNDLED)


def _executor(tools=GRANT, lazy=True, registry=None) -> AgentExecutor:
    """A real executor over the BUNDLED catalogue: the ids, the groups and the
    `group.json` files under test are the ones that actually ship."""
    agent = Agent(id="lazy" if lazy else "eager", name="a", tools=list(tools),
                  lazy_tools=lazy, memory_enabled=False)
    model = ModelConfig(id="m", name="m", provider="llamacpp",
                        base_url="http://127.0.0.1:9")  # never contacted
    stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                    models=JsonStore(Path(_TMP) / "models"))
    return AgentExecutor(agent, model, registry or _registry(), stores)


def _defs(ex: AgentExecutor) -> list[dict]:
    return ex.tool_registry.get_definitions_for_agent(ex.agent.tools)


def _begin(ex: AgentExecutor):
    """What the turn does at :1760 — build the catalogue, then filter."""
    full = _defs(ex)
    ex._lazy_begin_turn(full)
    return full, ex._lazy_filter(full)


def _ids(defs) -> list[str]:
    return [d["id"] for d in defs]


def _bytes(defs) -> int:
    """Schema weight, the quantity the flag exists to reduce. Bytes, not
    tokens: no tokenizer is available offline and the ratio is what matters."""
    return len(json.dumps(defs))


# ------------------------------------------------------------------ cases

def test_flag_off_changes_nothing():
    ex = _executor(lazy=False)
    full, sent = _begin(ex)
    assert sent == full, "an agent that did not ask for it must see today's payload"
    assert ex._lazy_catalogue == [], ex._lazy_catalogue
    assert ex._lazy_callable_defs(sent) == full
    assert ex._is_gate_call({"function": {"name": "activate_tools"}}) is False
    assert ex._lazy_autoload("file_read") == []
    print("ok: flag off -> definitions identical to today, no gate, no state")


def test_first_payload_is_the_catalogue_alone():
    ex = _executor()
    full, sent = _begin(ex)
    assert _ids(sent) == ["activate_tools"], _ids(sent)
    ratio = _bytes(sent) / _bytes(full)
    assert ratio < 0.30, f"first payload is {ratio:.0%} of the full one"

    # The catalogue rides in the `category` enum, which is also what makes the
    # text protocol work for free: _build_tools_prompt renders an enum as
    # "(one of: ...)".
    prop = sent[0]["parameters"]["properties"]["category"]
    assert sorted(prop["enum"]) == ["file_management", "memory", "shell_exec"], prop["enum"]
    # A described group states what it is FOR; a flat tool is its own name.
    assert "read, write, edit" in prop["description"], prop["description"]
    assert "shell_exec" in prop["description"]
    print(f"ok: first payload is the gate alone, {ratio:.0%} of the full schemas")


def test_capabilities_are_untouched():
    """The invariant the whole design rests on: what the agent MAY run, and how
    it is described to others, cannot depend on a visibility flag."""
    lazy, eager = _executor(lazy=True), _executor(lazy=False)
    _begin(lazy)
    assert lazy._granted_tools() == eager._granted_tools()
    assert lazy._granted_tools() >= {"file_read", "memory_search", "shell_exec"}

    raw = {"id": "a", "description": "d", "tools": GRANT}
    assert (directory_entry(raw, lazy.tool_registry)
            == directory_entry(raw, eager.tool_registry))
    print("ok: _granted_tools() and directory_entry() ignore the flag")


async def test_activation_cycle():
    ex = _executor()
    full, sent = _begin(ex)

    out = await activate_tools_handler(category="file_management", executor=ex)
    assert "file_read" in out and "file_write" in out, out
    assert ex._lazy_dirty is True, "the turn must rebuild its payload"

    sent = ex._lazy_filter(full)
    assert "file_read" in _ids(sent) and "file_write" in _ids(sent), _ids(sent)
    assert "memory_search" not in _ids(sent), "only the category asked for"
    # The gate survives, minus the entry just spent: an enum that still offered
    # it would invite the model to pay a round trip for nothing.
    gate = sent[0]
    assert gate["id"] == "activate_tools"
    assert sorted(gate["parameters"]["properties"]["category"]["enum"]) \
        == ["memory", "shell_exec"], gate

    # An unknown key is refused in a way that names the alternatives, and a
    # refusal must not spend the turn's only chance.
    bad = await activate_tools_handler(category="nope", executor=ex)
    assert bad.startswith("NOTHING ACTIVATED"), bad
    assert "memory" in bad, bad

    # Once everything is on, the gate is gone and the payload IS the full set.
    await activate_tools_handler(category="memory shell_exec", executor=ex)
    final = ex._lazy_filter(full)
    assert "activate_tools" not in _ids(final), _ids(final)
    assert sorted(_ids(final)) == sorted(_ids(full)), _ids(final)
    print("ok: activating adds schemas, spends the entry, and ends at the full set")


async def test_activation_is_not_charged_and_stays_for_the_turn():
    ex = _executor()
    full, _ = _begin(ex)
    call = {"function": {"name": "activate_tools", "arguments": "{}"}}
    assert ex._is_gate_call(call) is True, "the gate is free of max_tool_calls"
    assert ex._is_gate_call({"function": {"name": "file_read"}}) is False

    await activate_tools_handler(category="memory", executor=ex)
    ex._lazy_dirty = False                      # the turn consumed the rebuild
    assert "memory_search" in _ids(ex._lazy_filter(full)), "activation lasts the turn"

    # A new turn starts from the catalogue again.
    ex._lazy_begin_turn(full)
    assert ex._lazy_active == set()
    assert _ids(ex._lazy_filter(full)) == ["activate_tools"]
    print("ok: gate is free, activation lasts the turn and no longer")


def test_unloaded_tool_still_runs():
    """The safety net, and the reason the text protocol survives: a local model
    that names a tool its agent holds must have it executed, not read as prose."""
    ex = _executor()
    full, sent = _begin(ex)

    # _parse_text_tool_calls and _looks_like_tool_call work off THIS list.
    callable_defs = ex._lazy_callable_defs(sent)
    assert "file_read" in _ids(callable_defs), _ids(callable_defs)
    assert "activate_tools" in _ids(callable_defs)

    assert ex._lazy_autoload("file_read") == ["file_management"]
    assert "file_read" in _ids(ex._lazy_filter(full))
    assert ex._lazy_autoload("file_read") == [], "already on, nothing to do"
    print("ok: an unloaded but granted tool is callable and switches itself on")


def test_per_agent_enum_does_not_leak():
    """The definitions live in the registry's cache: an enum written in place
    would follow another agent into its turn."""
    registry = _registry()
    a = _executor(tools=GRANT, registry=registry)
    b = _executor(tools=["memory/*"], registry=registry)
    _begin(a)
    _begin(b)
    gate_a, gate_b = a._lazy_gate_def(), b._lazy_gate_def()
    assert len(gate_a["parameters"]["properties"]["category"]["enum"]) == 3
    assert gate_b["parameters"]["properties"]["category"]["enum"] == ["memory"]

    cached = registry.get_definition("activate_tools")
    assert "enum" not in cached["parameters"]["properties"]["category"], cached
    assert "Pick by what you need to DO" not in json.dumps(cached), cached
    print("ok: each agent's enum is its own, the registry's copy stays pristine")


def test_group_description_falls_back_to_names():
    """`group.json` is optional. Without one a category describes itself with
    its members' names — terse, deterministic, and exactly the names the model
    will see once it activates."""
    user = Path(_TMP) / "userlayer"
    (user / "gadgets" / "widget").mkdir(parents=True, exist_ok=True)
    (user / "gadgets" / "widget" / "tool.json").write_text(json.dumps({
        "name": "Widget", "description": "does widget things",
        "parameters": {"type": "object", "properties": {}},
    }))
    (user / "gadgets" / "widget" / "run").write_text("#!/bin/sh\necho ok\n")
    (user / "gadgets" / "widget" / "run").chmod(0o755)

    registry = _registry(user)
    assert registry.group_meta("gadgets") is None
    entry = [e for e in registry.activation_catalogue(["gadgets/*"])
             if e["key"] == "gadgets"][0]
    assert entry["description"] == "widget", entry
    assert entry["tool_ids"] == ["widget"], entry

    # With one, the category speaks for itself and the names stay out.
    (user / "gadgets" / "group.json").write_text(json.dumps(
        {"name": "Gadgets", "description": "fiddle with gadgets."}))
    registry.mark_dirty()
    assert registry.group_meta("gadgets")["name"] == "Gadgets"
    entry = [e for e in registry.activation_catalogue(["gadgets/*"])
             if e["key"] == "gadgets"][0]
    assert entry["description"] == "fiddle with gadgets.", entry

    # Malformed metadata degrades to the fallback rather than failing the scan.
    (user / "gadgets" / "group.json").write_text("{ not json")
    registry.mark_dirty()
    assert registry.group_meta("gadgets") is None
    entry = [e for e in registry.activation_catalogue(["gadgets/*"])
             if e["key"] == "gadgets"][0]
    assert entry["description"] == "widget", entry
    print("ok: group.json is optional, wins when present, and never breaks the scan")


def test_partial_group_grant_drops_the_group_description():
    """`group.json` describes the group ENTIRE. An agent holding one member of
    it must not read the whole group's purpose and switch the category on
    expecting tools it was never granted."""
    registry = _registry()
    whole = [e for e in registry.activation_catalogue(["file_management/*"])
             if e["key"] == "file_management"][0]
    assert "read, write, edit" in whole["description"], whole
    assert len(whole["tool_ids"]) > 1

    part = [e for e in registry.activation_catalogue(["list_dir"])
            if e["key"] == "file_management"][0]
    assert part["tool_ids"] == ["list_dir"], part
    assert part["description"] == "list_dir", part
    print("ok: a partial grant describes itself with the names it really holds")


def test_flat_tool_carries_its_first_sentence():
    """A bare id reads fine for `shell_exec` and says nothing about
    `recall_delegation`. The catalogue is read to CHOOSE, so it takes the
    sentence a tool leads with and stops — never its parameters."""
    registry = _registry()
    entries = {e["key"]: e["description"]
               for e in registry.activation_catalogue(
                   ["shell_exec", "recall_delegation", "web_research"])}
    assert entries["shell_exec"] == (
        "Execute a shell command on the local system and return stdout/stderr.")
    assert entries["recall_delegation"].startswith("Look up what another agent")
    assert entries["recall_delegation"].endswith("chat."), entries
    # Long first sentences are capped on a word boundary, never mid-word.
    long = entries["web_research"]
    assert len(long) <= ToolRegistry._SUMMARY_LIMIT + 1, long
    assert long.endswith("\u2026") and " " in long, long
    assert not long.rstrip("\u2026").endswith(" "), long
    # And the cap is a summary, not the schema: no parameter names leak in.
    assert "query" not in long.lower(), long
    print("ok: flat entries carry one capped sentence of their own description")


def test_bundled_groups_describe_themselves():
    """Every bundled group ships a `group.json`: a catalogue entry that falls
    back to names is legal but poorer, and here it would just be an oversight."""
    registry = _registry()
    cats = sorted({m["category"] for m in registry.get_all_definitions() if m.get("category")})
    missing = [c for c in cats if registry.group_meta(c) is None]
    assert not missing, f"bundled groups without group.json: {missing}"
    print(f"ok: all {len(cats)} bundled groups describe themselves")


def test_empty_grant_and_missing_gate():
    """Two ways the flag must simply not engage, rather than produce a turn
    with no tools at all."""
    ex = _executor(tools=[])
    full, sent = _begin(ex)
    assert full == [] and sent == []
    assert ex._lazy_catalogue == []

    class NoGate(ToolRegistry):
        def get_definition(self, tool_id):
            return None if tool_id == "activate_tools" else super().get_definition(tool_id)

    ex = _executor(registry=NoGate(Path(_TMP) / "tools", bundled_dir=BUNDLED))
    full, sent = _begin(ex)
    assert sent == full, "gate not installed -> send everything, do not strand the agent"
    print("ok: no grant / no gate -> the flag does not engage")


if __name__ == "__main__":
    test_flag_off_changes_nothing()
    test_first_payload_is_the_catalogue_alone()
    test_capabilities_are_untouched()
    asyncio.run(test_activation_cycle())
    asyncio.run(test_activation_is_not_charged_and_stays_for_the_turn())
    test_unloaded_tool_still_runs()
    test_per_agent_enum_does_not_leak()
    test_group_description_falls_back_to_names()
    test_partial_group_grant_drops_the_group_description()
    test_flat_tool_carries_its_first_sentence()
    test_bundled_groups_describe_themselves()
    test_empty_grant_and_missing_gate()
    print("all tests passed")
