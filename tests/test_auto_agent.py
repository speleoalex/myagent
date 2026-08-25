#!/usr/bin/env python3
"""The chat's "Auto" agent option — per-message routing to the best agent.

Run: server/.venv/bin/python tests/test_auto_agent.py
(needs the venv; MYAGENT_* dirs are temporary and no network is ever touched:
the LLM half of pick_agent is only reached with 2+ candidates AND a resolvable
model, and every case below stays on the pure functions and the short-circuit
paths — with these temp dirs any real LLM attempt would fail, so a returned id
proves the call was never made.)

The contract:
  1. parse_pick is forgiving but never invents: exact id, id inside prose,
     think-tags stripped, longest id wins on overlap, "unknown"/garbage/foreign
     ids -> None;
  2. route_candidates honors enabled AND callable (machine selection respects
     the same opt-out as call_agent) — EXCEPT master, a candidate even with
     callable: false (it is the right entrypoint for general questions);
  3. pick_agent short-circuits without an LLM call: empty message, zero
     candidates, single candidate;
  4. the session flag mirrors the model_override contract (set/pop);
  5. source checks — the forgotten-call-site class of bug: both endpoints
     route, the non-streaming one only for the web session (connectors never
     classify), a literal agent named "auto" beats the sentinel, and the
     chat's model_override reaches the classifier;
  6. i18n: both dictionaries carry the new keys.
"""

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app.engine.agent_router import (mark_foreign, parse_pick,     # noqa: E402
                                     pick_agent, route_candidates)
from app.engine.executor import AgentExecutor, Stores               # noqa: E402
from app.models import ChatMessage                                  # noqa: E402
from app.routers.chat import _mark_foreign, _remember_agent_auto    # noqa: E402
from app.storage.store import JsonStore                             # noqa: E402
from app.tools.registry import ToolRegistry                         # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# 1. parse_pick — forgiving, never inventing.
ids = ["master", "sysadmin", "web-researcher", "web"]
check("exact id", parse_pick("sysadmin", ids) == "sysadmin")
check("quoted/punctuated id", parse_pick("'sysadmin'.", ids) == "sysadmin")
check("id inside prose",
      parse_pick("I think sysadmin fits best here.", ids) == "sysadmin")
check("unknown -> None", parse_pick("unknown", ids) is None)
check("think-tags stripped",
      parse_pick("<think>web? no, general...</think>\nmaster", ids) == "master")
check("garbage -> None", parse_pick("no idea, sorry!", ids) is None)
check("longest id wins on overlap",
      parse_pick("web-researcher", ids) == "web-researcher")
check("id outside candidates -> None",
      parse_pick("coder", ["master", "sysadmin"]) is None)
check("empty reply -> None", parse_pick("", ids) is None)

# 2. route_candidates — enabled AND callable, master excepted.
agents.save("normal", {"id": "normal", "name": "N", "system_prompt": "x"})
agents.save("disabled", {"id": "disabled", "name": "D", "system_prompt": "x",
                         "enabled": False})
agents.save("meta", {"id": "meta", "name": "M", "system_prompt": "x",
                     "callable": False})
agents.save("master", {"id": "master", "name": "Master", "system_prompt": "x",
                       "callable": False})
got = {a["id"] for a in route_candidates(stores)}
check("missing flags default to included", "normal" in got)
check("enabled: false excluded", "disabled" not in got)
check("callable: false excluded (deliberate, human pick)", "meta" not in got)
check("master included despite callable: false", "master" in got)


# 3. pick_agent short-circuits (no model registered -> any LLM attempt fails,
#    so a returned id proves no call was made).
async def main():
    check("empty message -> None",
          await pick_agent("", stores, registry) is None)
    check("whitespace message -> None",
          await pick_agent("   ", stores, registry) is None)

    solo_agents = JsonStore(base / "solo" / "agents")
    solo_agents.save("only", {"id": "only", "name": "O", "system_prompt": "x"})
    solo = Stores(agents=solo_agents, models=models)
    check("single candidate short-circuits to it",
          await pick_agent("ciao", solo, registry) == "only")

    empty = Stores(agents=JsonStore(base / "none" / "agents"), models=models)
    check("zero candidates -> None",
          await pick_agent("ciao", empty, registry) is None)


asyncio.run(main())

# 4. the session flag — the model_override contract (set/pop, plain dict).
session = {}
_remember_agent_auto(session, True)
check("flag set", session.get("agent_auto") is True)
_remember_agent_auto(session, False)
check("flag popped, not falsified", "agent_auto" not in session)

# 4b. _mark_foreign + _split_history — foreign turns leave the few-shot
#     channel but stay in the returned history. (Measured rationale: with
#     another agent's direct answers in the message list a 4B made ZERO tool
#     calls in 3/3 runs, 3/3 tool calls without them; a warning note alone
#     changed nothing — see prompts.SECTION_FOREIGN_HISTORY. And filtering
#     the prior itself was a data-loss bug: record_turn stores what the
#     executor hands back, so the foreign turns vanished from the session.)
mixed = {
    "agent_auto": True,
    "messages": [
        {"role": "user", "agent_id": "conversation", "text": "ciao"},
        {"role": "assistant", "text": "Ciao!"},
        {"role": "user", "agent_id": "librarian", "text": "cerca X"},
        {"role": "tool", "tool": "local_search"},
        {"role": "assistant", "text": "Trovato Y."},
    ],
    "conversation": [
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "Ciao!"},
        {"role": "user", "content": "cerca X"},
        {"role": "assistant", "content": "Trovato Y."},
    ],
}


def _prior(sess):
    return [ChatMessage(**m) for m in sess["conversation"]]


prior = _prior(mixed)
_mark_foreign(prior, mixed, "librarian")
check("other agents' turns flagged, own turns not",
      [m.foreign for m in prior] == [True, True, None, None])
history = AgentExecutor._clean_conversation(prior, max_messages=10)
own, transcript = AgentExecutor._split_history(history)
check("own turns are what gets sent",
      [m["content"] for m in own] == ["cerca X", "Trovato Y."])
check("foreign turns become the quoted transcript",
      transcript is not None and "user: ciao" in transcript
      and "assistant: Ciao!" in transcript and "cerca X" not in transcript)
check("the flag never reaches payload or storage",
      all("foreign" not in m for m in history))
check("whole history survives (nothing dropped, order kept)",
      [m["content"] for m in history] == ["ciao", "Ciao!", "cerca X", "Trovato Y."])
p2 = _prior(mixed)
_mark_foreign(p2, dict(mixed, agent_auto=False), "librarian")
check("outside Auto mode nothing is flagged", not any(m.foreign for m in p2))
p3 = _prior(mixed)
_mark_foreign(p3, dict(mixed, messages=mixed["messages"][:1]), "librarian")
check("misalignment flags nothing, never a crash", not any(m.foreign for m in p3))
p3b = _prior(mixed)
_mark_foreign(p3b, dict(mixed, messages=[
    {"role": "user", "agent_id": "conversation", "text": "ciao"},
    {"role": "user", "agent_id": "sysadmin", "text": "stopped before any reply"},
    {"role": "user", "agent_id": "librarian", "text": "cerca X"}]), "librarian")
check("a turn stopped before its reply (in messages only) is skipped by text",
      [m.foreign for m in p3b] == [True, True, None, None])
p3c = _prior(mixed)
_mark_foreign(p3c, dict(mixed, conversation=[
    dict(m, content="[Alessandro via Telegram]\n" + m["content"]) if m["role"] == "user" else m
    for m in mixed["conversation"]]), "librarian")
check("the channel sender prefix does not break the text match",
      [m.foreign for m in p3c] == [True, True, None, None])
p4 = _prior(mixed)
_mark_foreign(p4, dict(mixed, messages=[
    {"role": "user", "agent_id": "master", "text": "older, outside the window"},
    {"role": "assistant", "text": "..."}] + mixed["messages"]), "librarian")
check("conversation[] is a suffix: aligned from the end past older turns",
      [m.foreign for m in p4] == [True, True, None, None])
p5 = _prior(mixed)
mark_foreign(p5, dict(mixed, agent_auto=False), "librarian")
check("agent_router.mark_foreign itself has no Auto gate (channels use it always)",
      [m.foreign for m in p5] == [True, True, None, None])
check("no foreign turns -> no transcript",
      AgentExecutor._split_history([{"role": "user", "content": "x"}])[1] is None)
check("ChatMessage.foreign is dropped from dumps when unset",
      "foreign" not in ChatMessage(role="user", content="x").model_dump(exclude_none=True))

# 5. source checks — a forgotten call site fails silently.
chat_src = (ROOT / "server" / "app" / "routers" / "chat.py").read_text(encoding="utf-8")
router_src = (ROOT / "server" / "app" / "engine" / "agent_router.py").read_text(encoding="utf-8")
check("every entry point routes auto (web chat, web stream, named session)",
      chat_src.count("await _route_auto(") == 3)
check("non-streaming routes only the web session (connectors never classify)",
      "if not req.session_id:" in chat_src)
check("the shared resolver is what the web router calls",
      "await resolve_auto(" in chat_src)
check("a literal agent named 'auto' beats the sentinel",
      "stores.agents.get(AUTO) is not None" in router_src)
core_src = (ROOT / "connectors" / "plugin" / "myagent_connectors" / "core.py").read_text(encoding="utf-8")
check("a connector binding on 'auto' resolves through the same resolver",
      "await resolve_auto(" in core_src)
ct_src = (ROOT / "server" / "app" / "engine" / "channel_turn.py").read_text(encoding="utf-8")
check("channel turns quote other agents' history, gated on the Auto flag",
      "if req.agent_auto:" in ct_src and "mark_foreign(prior, session, req.agent_id)" in ct_src)
check("the connector sets the Auto flag on the request",
      "req.agent_auto = True" in core_src)
check("live.is_active is re-checked after the classification (twin-run race)",
      chat_src.count("if live.is_active(sid):") == 2)
check("the id 'auto' is reserved at both agent creation paths",
      "RESERVED_AGENT_ID" in (ROOT / "server" / "app" / "routers" / "agents.py").read_text(encoding="utf-8")
      and "reserved for automatic agent selection" in
      (ROOT / "server" / "tools" / "manage_agents" / "run").read_text(encoding="utf-8"))
check("the bindings form offers Auto",
      'value="auto"' in (ROOT / "ui" / "js" / "connectors.js").read_text(encoding="utf-8"))
check("the chat's model pick reaches the classifier",
      "model_override=req.model_override)" in chat_src.split("resolve_auto(", 1)[1][:200]
      and "model_override=model_override" in router_src)
check("the classifier receives the last-used agent as a hint",
      re.search(r"pick_agent\([^)]*last_agent_id=", router_src, re.S) is not None)
check("both endpoints mark the foreign turns",
      chat_src.count("_mark_foreign(prior, session, req.agent_id)") == 2)
check("pick_agent strips reasoning before parsing",
      "strip_reasoning" in router_src)
check("the last-agent hint only names a valid candidate",
      "AUTO_ROUTE_LAST_AGENT.format" in router_src)
exec_src = (ROOT / "server" / "app" / "engine" / "executor.py").read_text(encoding="utf-8")
check("the returned conversation is rebuilt from the WHOLE history + this turn",
      "messages[:1] + history + messages[history_end:]" in exec_src
      and "history_end = len(messages)" in exec_src)


# 5b. functional: the foreign transcript lands in the system prompt
#     (_prepare_turn, no network needed).
async def check_prepare():
    agents.save("plain", {"id": "plain", "name": "P", "system_prompt": "base",
                          "model_id": "m1"})
    models.save("m1", {"id": "m1", "name": "M", "provider": "llamacpp",
                       "model": "m", "base_url": "http://localhost:8080"})
    ex = await AgentExecutor.create_for_agent("plain", registry, stores)
    sys_content, _, _ = ex._prepare_turn([], None, None,
                                         foreign_context="user: meteo?")
    check("foreign transcript quoted in the system prompt",
          "Conversation context" in sys_content and "user: meteo?" in sys_content)
    sys_plain, _, _ = ex._prepare_turn([], None, None)
    check("no transcript, no section", "Conversation context" not in sys_plain)

asyncio.run(check_prepare())
check("the directory line has ONE definition (executor.directory_entry)",
      "directory_entry" in router_src and
      "directory_entry" in
      (ROOT / "server" / "app" / "engine" / "executor.py").read_text(encoding="utf-8"))

# 6. i18n — both dictionaries, always.
for lang in ("en", "it"):
    src = (ROOT / "ui" / "js" / "i18n" / f"{lang}.js").read_text(encoding="utf-8")
    check(f"{lang}.js has chat.agentAuto", "chat.agentAuto" in src)
    check(f"{lang}.js has chat.viaAgent", "chat.viaAgent" in src)

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — Auto routes per message through enabled+callable agents (plus"
      " master), parses forgivingly without inventing ids, short-circuits"
      " without a model, and both endpoints resolve the sentinel")
