"""Text-based tool-call parsing.

Local models often lack native (structured) tool calling: they emit the call as
JSON or a Python-style function call inside their text output. This module
extracts those calls and normalizes them to the OpenAI tool_calls format.
Pure functions — no executor/registry state.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Callable


# Keys a model may use for the text it wants to hand to a sub-agent, when it
# writes the call as `{"name": "<agent_id>", ...}` instead of a call_agent call.
# Ordered by how explicit they are.
_MESSAGE_KEYS = ("message", "text", "query", "prompt", "input", "question", "task", "content")


def _extract_message(arguments) -> str:
    """Best-effort recovery of the message for a call_agent call the model wrote
    with the agent id as the tool name. Such a call carries no `message` key
    (that shape only exists on call_agent), so look under the usual aliases and,
    failing that, accept a lone free-form string argument. Returns "" when there
    is nothing to send — call_agent then rejects the call with a usable error
    instead of waking a sub-agent up with an empty prompt."""
    if not isinstance(arguments, dict):
        return ""
    for key in _MESSAGE_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    free = [v for k, v in arguments.items()
            if k != "agent_id" and isinstance(v, str) and v.strip()]
    return free[0] if len(free) == 1 else ""


def _make_call(name: str, arguments) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments) if isinstance(arguments, dict) else str(arguments),
        },
    }


def coerce_arg_keys(arguments: dict, definition: dict | None) -> dict:
    """Remap wrong/placeholder argument keys to the tool's real parameter
    names. Small models sometimes emit the template key 'param' or put the
    value under the wrong key (e.g. web_search({"param": "..."}) instead of
    {"query": "..."})."""
    if not isinstance(arguments, dict) or not arguments or not definition:
        return arguments
    schema = definition.get("parameters", {})
    prop_names = list(schema.get("properties", {}).keys())
    if not prop_names:
        return arguments
    unknown = [k for k in arguments if k not in prop_names]
    if not unknown:
        return arguments  # all keys already valid
    # Single-parameter tool: map the lone provided value to the lone param
    if len(prop_names) == 1 and len(arguments) == 1:
        arguments[prop_names[0]] = arguments.pop(unknown[0])
        return arguments
    # One wrong key + exactly one missing required param: rename it
    missing_required = [p for p in schema.get("required", []) if p not in arguments]
    if len(unknown) == 1 and len(missing_required) == 1:
        arguments[missing_required[0]] = arguments.pop(unknown[0])
    return arguments


def parse_tool_calls_from_text(
    content: str,
    definitions: dict[str, dict],
    agent_ids_provider: Callable[[], set[str]] | None = None,
) -> list[dict] | None:
    """Parse tool calls embedded as JSON or Python-style function calls in the
    assistant's text content.

    ``definitions`` maps tool id -> tool metadata (for name matching and
    argument-key coercion). ``agent_ids_provider``, when given, enables
    recovering call_agent calls whose tool name is a bare agent id or a
    placeholder (only meaningful for agents that can delegate)."""
    if not content or not content.strip():
        return None

    text = content.strip()
    known_tools = set(definitions)

    # Try Python-style function call: tool_name(key="value", key2="value2")
    func_calls = _parse_python_style_calls(text, known_tools)
    if func_calls:
        return func_calls

    # Try "tool_name {json_args}" format (tool name followed by JSON object)
    if known_tools:
        tool_names_re = "|".join(re.escape(t) for t in known_tools)
        inline_match = re.search(rf'(?<!\w)({tool_names_re})\s+(\{{[\s\S]*\}})', text)
        if inline_match:
            name = inline_match.group(1)
            try:
                arguments = json.loads(inline_match.group(2))
                if isinstance(arguments, dict):
                    return [_make_call(name, arguments)]
            except json.JSONDecodeError:
                pass

    # Extract from markdown code blocks first (all of them: a model may fence
    # each call separately).
    blocks = re.findall(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if blocks:
        text = "\n".join(b.strip() for b in blocks)

    # A model may emit SEVERAL tool-call objects back to back (one per line or
    # concatenated). Scan for every balanced top-level JSON value: a single
    # greedy first-{-to-last-} regex captures the whole run and fails to parse
    # on exactly that shape, silently dropping ALL the calls.
    parsed = []
    for value in _extract_json_values(text):
        if isinstance(value, dict):
            parsed.append(value)
        elif isinstance(value, list):
            parsed.extend(v for v in value if isinstance(v, dict))
    if not parsed:
        return None

    tool_calls = []

    for item in parsed:
        if not isinstance(item, dict):
            continue

        name = item.get("name") or item.get("function", {}).get("name")
        arguments = item.get("arguments") or item.get("parameters") or item.get("function", {}).get("arguments")

        if not name:
            continue

        # Normalize arguments to a dict up front so we can inspect them
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if arguments is None:
            arguments = {}

        # Recover from wrong/placeholder tool names emitted by small models
        # (e.g. "tool_call", "tool_name", "function"). The arguments are
        # usually correct even when the name is not.
        if name not in known_tools and agent_ids_provider is not None:
            agent_ids = agent_ids_provider()
            if name in agent_ids:
                # Model used the agent id directly as the tool name
                arguments = {"agent_id": name, "message": _extract_message(arguments)}
                name = "call_agent"
            elif isinstance(arguments, dict) and "agent_id" in arguments:
                # Placeholder name but call_agent-shaped arguments
                name = "call_agent"

        if name not in known_tools:
            continue

        arguments = coerce_arg_keys(arguments, definitions.get(name))
        tool_calls.append(_make_call(name, arguments))

    return tool_calls if tool_calls else None


def _extract_json_values(text: str) -> list:
    """Every balanced top-level ``{...}`` / ``[...]`` block in ``text`` that
    parses as JSON, in order of appearance. Non-JSON braces (prose, code) are
    skipped by advancing one character and rescanning."""
    values = []
    i, n = 0, len(text)
    while i < n:
        if text[i] not in "{[":
            i += 1
            continue
        end = _scan_balanced_json(text, i)
        if end is not None:
            try:
                values.append(json.loads(text[i:end]))
                i = end
                continue
            except json.JSONDecodeError:
                pass
        i += 1
    return values


def _scan_balanced_json(text: str, start: int) -> int | None:
    """Index just past the brace/bracket balancing ``text[start]``, honoring
    JSON string literals and escapes. None when unbalanced."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _parse_python_style_calls(text: str, known_tools: set[str]) -> list[dict] | None:
    """Parse Python-style function calls like: tool_name(key="value", key2="value2")
    Also handles nested parentheses in quoted values."""
    tool_names_re = "|".join(re.escape(t) for t in known_tools)
    if not tool_names_re:
        return None
    # Match known tool name followed by opening paren, then capture until balanced close
    pattern = re.compile(rf'(?<!\w)({tool_names_re})\s*\(')
    tool_calls = []

    for m in pattern.finditer(text):
        name = m.group(1)
        # Find balanced closing paren (respecting quotes)
        start = m.end()
        args_str = _extract_balanced_parens(text, start)
        if args_str is None:
            continue

        arguments = {}
        if args_str.strip():
            # Parse key=value pairs: key="value" or key='value' or key=value
            arg_pattern = r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([^,\s)]+))'
            for am in re.finditer(arg_pattern, args_str):
                key = am.group(1)
                val = am.group(2) if am.group(2) is not None else (am.group(3) if am.group(3) is not None else am.group(4))
                if val is not None:
                    val = val.replace('\\"', '"').replace("\\'", "'")
                arguments[key] = val

        # The parentheses carried something, but nothing parsed as key=value:
        # a positional arg or plain prose — the shape of a reply MENTIONING
        # the tool («check it with memory_search("ora")»), not calling it.
        # Executing with {} would both do the wrong thing and eat the model's
        # actual answer, so it is not a call.
        if args_str.strip() and not arguments:
            continue

        tool_calls.append(_make_call(name, arguments))

    return tool_calls if tool_calls else None


def _extract_balanced_parens(text: str, start: int) -> str | None:
    """Extract content between balanced parentheses, respecting quotes."""
    depth = 1
    in_quote = None
    i = start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == '\\' and in_quote:
            i += 2
            continue
        if ch in ('"', "'"):
            if in_quote == ch:
                in_quote = None
            elif in_quote is None:
                in_quote = ch
        elif not in_quote:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        i += 1
    return text[start:i - 1] if depth == 0 else None
