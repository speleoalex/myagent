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

    # Extract from markdown code blocks first
    code_block = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if code_block:
        text = code_block.group(1).strip()

    json_match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
    if not json_match:
        return None

    try:
        parsed = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict):
        parsed = [parsed]
    elif not isinstance(parsed, list):
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
                msg = arguments.get("message", "") if isinstance(arguments, dict) else ""
                arguments = {"agent_id": name, "message": msg}
                name = "call_agent"
            elif isinstance(arguments, dict) and "agent_id" in arguments:
                # Placeholder name but call_agent-shaped arguments
                name = "call_agent"

        if name not in known_tools:
            continue

        arguments = coerce_arg_keys(arguments, definitions.get(name))
        tool_calls.append(_make_call(name, arguments))

    return tool_calls if tool_calls else None


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
