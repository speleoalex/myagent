"""Every string the engine injects into a model's context, in one place.

Two different kinds of thing live here, for two different reasons.

**Section headers** (``## Available Tools``, ``## Memory``, …). The system prompt
is assembled from the agent's own text plus up to four generated blocks, built by
different methods in different files. Keeping the headers together is what makes
the resulting prompt reviewable as a whole (the assembly order lives in the
executor: _system_prompt_with_tools, then _prepare_turn).

**Scaffolding markers** — and these are load-bearing, not cosmetic. Text-protocol
turns are stitched with literal markers (``TOOL RESULTS:``, ``(used tool: …)``)
that are WRITTEN in one place and MATCHED in two others: history injection drops
them so the model stops mimicking the format, and the session-rewind endpoint
counts real user turns around them. They used to be duplicated string literals at
six sites; editing one and missing a matcher silently poisons every later turn's
history and mis-cuts a rewind, with no error anywhere. So: one definition, and the
matchers import it.

``LEGACY_*`` entries are wordings no longer produced. They stay because sessions
stored before the change are still on disk and must keep filtering cleanly —
append here when you reword a marker, never edit in place.
"""

# --- Scaffolding markers -------------------------------------------------
# Written by the executor's text-protocol path, matched by
# is_scaffolding_message() and by _is_marker_only().

TOOL_RESULTS_PREFIX = "TOOL RESULTS:"
USED_TOOL_PREFIX = "(used tool:"
UNPARSED_CALL_PREFIX = "(tool call:"
MALFORMED_PREFIX = "MALFORMED TOOL CALL"

#: Assistant-turn prefixes that mark plumbing rather than a reply.
ASSISTANT_MARKER_PREFIXES = (USED_TOOL_PREFIX, UNPARSED_CALL_PREFIX)

#: Written by LLMProvider._sanitize_messages when an endpoint rejects `tools`
#: and the in-flight payload must be flattened to plain text. Not "legacy":
#: still produced today — but only inside a single request's payload, so the
#: history matcher treats it like the legacy wordings below.
SANITIZED_TOOL_PREFIX = "[Called tool"

#: Earlier assistant wordings, kept so already-stored sessions stay clean
#: (SANITIZED_TOOL_PREFIX doubles as one: old versions did store it).
LEGACY_ASSISTANT_PREFIXES = (SANITIZED_TOOL_PREFIX,)

#: Earlier user-turn wordings, matched as substrings rather than prefixes.
LEGACY_USER_SUBSTRINGS = (
    "Do NOT call any more tools",
    "reply with ONLY the next tool-call JSON",
)


# --- Turn stitching (text protocol only) ---------------------------------

#: Handed back as a user turn after tools ran, when the provider has no native
#: tool calling. Ends with a decision, because the alternative — a bare results
#: dump — makes small models restate the results as their answer.
TOOL_RESULTS_NUDGE = (
    "\n\nNow decide: if these results do NOT fully cover the user's request, "
    "reply with ONLY the next tool call's JSON; if they do, write the final "
    "answer in the user's language using them. Never repeat these lines."
)

#: Forced final-answer pass: some local models keep re-emitting the same tool
#: call after a result instead of answering, which would leave the turn with the
#: bare "(used tool: …)" marker as its reply.
FORCE_ANSWER = (
    "Now write the final answer to the user's request using ONLY the information "
    "from the tool results above. Reply in the user's language as normal prose. "
    "Do NOT output JSON and do NOT call any tool."
)

#: Text-protocol recovery: the reply meant to be a tool call but did not parse.
MALFORMED_CALL = (
    "MALFORMED TOOL CALL: that JSON did not parse, so nothing ran. Send the call "
    "again as ONE JSON object and nothing else, and do NOT use any double quote "
    "inside a value — drop them or use 'single quotes': "
    "{\"name\": \"<tool>\", \"arguments\": {...}}. "
    "If you meant to answer the user, write plain prose with no JSON at all."
)

#: Shown as the reply when the loop ran out of iterations without answering.
NO_FINAL_RESPONSE = "[Agent reached maximum iterations without a final response]"

#: Marks a turn cut short by the autonomy wake timeout (also matched by the UI).
INTERRUPTED = "_[interrotto]_"


# --- System-prompt sections ----------------------------------------------
# Header text only; each block's body is built by its own method, because the
# bodies depend on live state (tool schemas, the agent registry, attachments,
# the memory store) that does not belong in a string module.

# The model sees them in this order: agents, tools, memory, attachments.
# `agents`/`tools` are part of the base prompt (fixed capabilities, built in
# _system_prompt_with_tools); `memory`/`attachments` are turn-scoped and are
# re-appended by _prepare_turn after any mid-loop rebuild of the base —
# dropping them there was a real bug once. The order lives in those two
# methods; a constant here pretending to drive it would just go stale.
SECTION_TOOLS = "\n\n## Available Tools\n"
SECTION_AGENTS = "\n\n## Available Agents\n"
SECTION_ATTACHMENTS = "\n\n## Attachments (this turn)"
SECTION_MEMORY = "\n\n## Memory\n"

#: Preamble of the text-protocol tool block. Only sent when the provider has no
#: native tool calling: in native mode the `tools` payload IS the documentation,
#: and sending both made models emit JSON as prose at ~650 tokens per turn.
TOOLS_PROTOCOL = (
    "When you need to use a tool, respond ONLY with a JSON object (no other text):\n"
    "```json\n"
    '{"name": "TOOL_NAME", "arguments": {"PARAM_NAME": "value"}}\n'
    "```\n\n"
    "Use the tool's EXACT name and EXACT parameter names shown below "
    "(do NOT use the literal words TOOL_NAME or PARAM_NAME).\n"
    "Available tools:"
)

AGENTS_PREAMBLE = ("Delegate with call_agent; an agent can only act through the "
                   "tools listed for it.")
