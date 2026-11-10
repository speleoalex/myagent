"""Claude Code CLI as a model backend (provider ``claude_code``).

Runs ``claude -p`` as a subprocess and uses it as a plain model: the point is
to answer with the account the CLI is logged into (a Claude subscription)
instead of an API key. Everything that makes the CLI an agent of its own is
switched off — its tools, MCP servers, settings, hooks, CLAUDE.md discovery,
slash commands, session files — and its system prompt is replaced with the
agent's. MyAgent's tools therefore travel in the text protocol
(``supports_tools`` is pinned to False in ``LLMProvider``).

Two properties of the CLI shape this module:

* It takes ONE user turn, not a message list, so the history is folded into
  that turn (``single_turn``).
* With ``--include-partial-messages`` it wraps the Anthropic SSE events
  verbatim, so ``LLMProvider._anthropic_chunks`` translates them unchanged.

``ModelConfig.base_url`` holds the command or the path of the executable: a
CLI's address is where it is installed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator

from app import config

PROVIDER = "claude_code"
DEFAULT_COMMAND = "claude"

# The CLI declares no window. Every model it can select has at least this much.
CONTEXT_WINDOW = 200_000

# One stdout line carries a whole message (the final `assistant` and `result`
# events repeat the full reply); asyncio's 64 KiB default would raise on it.
_LINE_LIMIT = 32 * 1024 * 1024

_HISTORY_NOTE = ("Earlier turns of this conversation are inside "
                 "<conversation_history>. Continue from them and reply only to "
                 "what follows the block.")


def find_executable(command: str | None) -> str | None:
    """Resolve the configured command to a path, or None if it is not installed.

    The second lookup is the native installer's directory: a systemd unit does
    not inherit the login shell's PATH, so `claude` works in a terminal and is
    "not found" from the service.
    """
    command = (command or "").strip() or DEFAULT_COMMAND
    return (shutil.which(command)
            or shutil.which(command, path=str(Path.home() / ".local" / "bin")))


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def single_turn(turns: list[dict]) -> list[dict]:
    """Anthropic-style turns -> the content blocks of ONE user message.

    The last user turn stays as it is; whatever precedes it is wrapped in tags
    so the model can tell the history from the message it has to answer.
    """
    if not turns:
        return []
    last = turns[-1]["content"] if turns[-1]["role"] == "user" else []
    history = turns[:-1] if last else turns
    if not history:
        return last
    blocks = [_text(_HISTORY_NOTE + "\n<conversation_history>\n")]
    for turn in history:
        blocks.append(_text(f'<turn role="{turn["role"]}">\n'))
        blocks.extend(turn["content"])
        blocks.append(_text("\n</turn>\n"))
    blocks.append(_text("</conversation_history>\n\n"))
    return blocks + last


def _environment(api_key: str, thinking: bool | None) -> dict[str, str]:
    env = dict(os.environ)
    if api_key:
        # A `claude setup-token` token is an OAuth credential and has its own
        # variable; anything else is an ordinary Anthropic API key.
        name = ("CLAUDE_CODE_OAUTH_TOKEN" if api_key.startswith("sk-ant-oat")
                else "ANTHROPIC_API_KEY")
        env[name] = api_key
    if thinking is False:
        env["MAX_THINKING_TOKENS"] = "0"
    return env


async def stream_events(
    command: str | None,
    model: str,
    system: str,
    blocks: list[dict],
    *,
    api_key: str = "",
    thinking: bool | None = None,
    idle_timeout: float = 600,
) -> AsyncIterator[dict]:
    """Run one CLI turn and yield its Anthropic stream events.

    Raises RuntimeError with a message fit for the chat when the CLI is
    missing, stalls or reports an error (not logged in, unknown model, usage
    limit reached).
    """
    executable = find_executable(command)
    if not executable:
        raise RuntimeError(
            f"Claude Code CLI not found ('{command or DEFAULT_COMMAND}'). "
            f"Install it on the server, or set its full path in the model's "
            f"executable field.")

    # A fixed, empty working directory: the CLI keys its per-project state on
    # the cwd, and it must not find anyone's CLAUDE.md there.
    workdir = config.CACHE_DIR / "claude_cli"
    workdir.mkdir(parents=True, exist_ok=True)
    # A file, not an argument: one argv entry is capped at 128 KiB on Linux.
    handle, system_file = tempfile.mkstemp(dir=workdir, suffix=".txt")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(system)

    argv = [executable, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose",
            "--system-prompt-file", system_file,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--no-session-persistence"]
    if model:
        argv += ["--model", model]

    proc = None
    stderr = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, env=_environment(api_key, thinking),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=_LINE_LIMIT)
        # Drained concurrently: a full stderr pipe would block the CLI.
        stderr = asyncio.create_task(proc.stderr.read())
        turn = {"type": "user", "message": {"role": "user", "content": blocks}}
        proc.stdin.write(json.dumps(turn).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()

        result = None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), idle_timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Claude Code CLI did not answer within {idle_timeout:g}s. "
                    f"Raise request_timeout in the model's options.") from None
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "stream_event":
                yield message.get("event") or {}
            elif kind == "result":
                result = message

        await proc.wait()
        if result is None or result.get("is_error"):
            detail = ((result or {}).get("result")
                      or (await stderr).decode(errors="replace").strip()[-500:]
                      or f"exit code {proc.returncode}")
            raise RuntimeError(f"Claude Code CLI error: {detail}")
    finally:
        # Also the stop button: closing the generator lands here mid-stream.
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if stderr is not None:
            stderr.cancel()
        os.unlink(system_file)
