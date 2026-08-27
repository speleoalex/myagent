"""The debug trace — ONE writer, shared by everything that talks to a model.

It used to live as a method on ``AgentExecutor``, which meant the trace could
only ever show the executor's own iterations. Everything else that calls an LLM
— the auto-routing classifier that picks which agent answers, the memory
compactor — was invisible, and "why did it choose that agent" is exactly the
question a trace is opened for.

So the writer moved down here and ``LLMProvider`` uses it too: every model call
is traced by construction, whoever makes it, with a label saying who. The
executor keeps only the annotations the provider cannot know (iteration
bookkeeping, dedup decisions, tool results).

Whether tracing is on is asked on EVERY write (``config.debug_enabled``), never
cached: the switch lives in Settings and must take effect on the next call, not
after a restart.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from app import config

# base64 data URIs (attached images/audio) are the one thing that must NOT go
# verbatim into the trace: a single photo is megabytes of noise.
_DATA_URI = re.compile(r"data:([\w/+.-]+);base64,([A-Za-z0-9+/=]{64,})")


def enabled() -> bool:
    return config.debug_enabled()


def _write(path, msg: str) -> None:
    """Append one line. Never raises: a trace that can break a chat turn is
    worse than no trace."""
    if not config.debug_enabled():
        return
    try:
        with open(path, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def log(msg: str) -> None:
    """One line of the TURN narrative (debug.log)."""
    _write(config.DEBUG_LOG_FILE, msg)


def api(msg: str) -> None:
    """One line of the RAW model-call log (api.log)."""
    _write(config.API_LOG_FILE, msg)


def content(value) -> str:
    """Render a message content IN FULL — text as-is, multimodal part lists as
    JSON — with only base64 payloads replaced by a size placeholder."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return _DATA_URI.sub(
        lambda m: f"data:{m.group(1)};base64,<{len(m.group(2))} chars omitted>",
        value,
    )


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def begin_turn(header: str, first_line: str = "") -> None:
    """Open a new TURN block. Appends — the file is a history, and rotation is
    what bounds it (see config.rotate_debug_log)."""
    if not config.debug_enabled():
        return
    config.ensure_debug_dir()
    config.rotate_debug_log()
    try:
        with open(config.DEBUG_LOG_FILE, "a") as f:
            f.write(f"\n\n{'=' * 78}\n=== {stamp()}  {header}\n{'=' * 78}\n")
            if first_line:
                f.write(first_line + "\n")
    except Exception:
        pass


def call(label: str, endpoint: str, payload: dict) -> None:
    """One outgoing model call, in full: who asked, where it went, and the
    exact payload. Written BEFORE the request, so a call that hangs or crashes
    still leaves its question in the trace."""
    if not config.debug_enabled():
        return
    config.ensure_debug_dir()
    config.rotate_debug_log(config.API_LOG_FILE)
    msgs = payload.get("messages") or []
    tools = payload.get("tools") or []
    params = {k: v for k, v in payload.items() if k not in ("messages", "tools", "system")}
    api(f"\n>>> {stamp()}  LLM CALL  [{label}]  -> {endpoint}")
    api(f"    params: {json.dumps(params, ensure_ascii=False)}")
    # Anthropic carries the system prompt outside `messages`; without this the
    # trace of an Anthropic call would be missing the whole system prompt.
    if payload.get("system"):
        api(f"    system: {content(payload['system'])}")
    if tools:
        # The SCHEMAS, not just the names: the enums the executor narrows at
        # runtime (delegation targets, notify contacts) exist only here, and
        # "why did it call that" is usually answered by them.
        api(f"    tools ({len(tools)}):")
        for t in tools:
            api("      " + json.dumps(t, ensure_ascii=False))
    api(f"    messages ({len(msgs)}):")
    for i, m in enumerate(msgs):
        api(f"      [{i}] {m.get('role', '?')}: {content(m.get('content'))}")
        if m.get("tool_calls"):
            api(f"           tool_calls: {json.dumps(m['tool_calls'], ensure_ascii=False)}")


def reply(label: str, text: str, tool_calls=None, reasoning: str = "",
          error: str = "") -> None:
    """What that call came back with."""
    if not config.debug_enabled():
        return
    if error:
        api(f"<<< {stamp()}  LLM ERROR [{label}]: {error}")
        return
    api(f"<<< {stamp()}  LLM REPLY [{label}]")
    if reasoning:
        api(f"    reasoning ({len(reasoning)} chars): {content(reasoning)}")
    api(f"    content: {content(text)}")
    if tool_calls:
        api(f"    tool_calls: {json.dumps(tool_calls, ensure_ascii=False)}")
