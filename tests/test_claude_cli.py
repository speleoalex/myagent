#!/usr/bin/env python3
"""Claude Code CLI provider (app.engine.claude_cli + LLMProvider).

Run: server/.venv/bin/python tests/test_claude_cli.py
(MYAGENT_* dirs are temporary. The real CLI is never run: a stub script that
speaks its stream-json protocol stands in for it, so no account is touched.)

The contract, one case each:
  1. the history is folded into ONE user turn: earlier turns inside
     <conversation_history>, the last user message after it, role:tool
     entries rewritten as text;
  2. a single user message travels untouched (no history block);
  3. the provider is pinned to the text protocol even when the config asks
     for native tools;
  4. the CLI's stream events come out as OpenAI-style chunks, and the CLI got
     the system prompt, the model and the "no tools" flags;
  5. an error result (unknown model, not logged in) raises with the CLI's own
     message;
  6. a missing executable raises a message that names the fix;
  7. the probe reports the executable as (un)reachable without any HTTP.
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.mkdtemp(prefix="myagent-test-")
os.environ["MYAGENT_HOME"] = _tmp

from app.engine import claude_cli, model_probe  # noqa: E402
from app.engine.llm_provider import LLMProvider  # noqa: E402
from app.models import ModelConfig  # noqa: E402

# Speaks the CLI's protocol: records argv + stdin, then streams a reply — or an
# error result when the model is "broken".
STUB = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
record = {"argv": args, "stdin": json.loads(sys.stdin.readline()),
          "system": open(args[args.index("--system-prompt-file") + 1]).read()}
open(__file__ + ".seen", "w").write(json.dumps(record))
def emit(d): print(json.dumps(d), flush=True)
emit({"type": "system", "subtype": "init"})
print("not json", flush=True)
if "broken" in args:
    emit({"type": "result", "is_error": True, "result": "There's an issue with the selected model."})
    sys.exit(1)
for text in ("Hel", "lo"):
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
          "delta": {"type": "text_delta", "text": text}}})
emit({"type": "result", "is_error": False, "result": "Hello"})
'''

stub = Path(_tmp) / "claude-stub"
stub.write_text(STUB)
stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def cfg(**kw) -> ModelConfig:
    base = dict(id="cc", name="cc", provider="claude_code", model="sonnet",
                base_url=str(stub))
    return ModelConfig(**{**base, **kw})


async def collect(provider, messages):
    text = ""
    async for chunk in provider.chat_completion_stream(messages, tools=[{"type": "function"}]):
        text += chunk["choices"][0]["delta"].get("content") or ""
    return text


def seen() -> dict:
    return json.loads(Path(str(stub) + ".seen").read_text())


async def main():
    # 1. history folding
    p = LLMProvider(cfg())
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "clock", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "12:00"},
        {"role": "assistant", "content": "It is noon."},
        {"role": "user", "content": "thanks, and now?"},
    ]
    blocks = claude_cli.single_turn(p._anthropic_messages(p._sanitize_messages(history)))
    flat = "".join(b["text"] for b in blocks)
    assert all(b["type"] == "text" for b in blocks)
    head, _, tail = flat.partition("</conversation_history>")
    assert '<turn role="user">\nfirst' in head and "12:00" in head and "It is noon." in head
    assert tail.strip() == "thanks, and now?", tail
    assert "SYS" not in flat

    # 2. a single user message is not wrapped
    single = claude_cli.single_turn(p._anthropic_messages([{"role": "user", "content": "hi"}]))
    assert single == [{"type": "text", "text": "hi"}], single

    # 3. text protocol, whatever the config says
    assert p.supports_tools is False
    assert LLMProvider(cfg(supports_tools=True)).supports_tools is False

    # 4. a turn through the stub
    p.reasoning = False
    assert await collect(p, history) == "Hello"
    got = seen()
    assert got["system"] == "SYS"
    argv = got["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    assert got["stdin"]["message"]["content"] == blocks
    assert not list((Path(_tmp) / "cache" / "claude_cli").iterdir()), "system file left behind"

    # 5. the CLI's own error message surfaces
    try:
        await collect(LLMProvider(cfg(model="broken")), history)
        raise AssertionError("an error result must raise")
    except RuntimeError as e:
        assert "issue with the selected model" in str(e), e

    # 6. missing executable
    missing = cfg(base_url=str(Path(_tmp) / "nope"))
    try:
        await collect(LLMProvider(missing), history)
        raise AssertionError("a missing executable must raise")
    except RuntimeError as e:
        assert "not found" in str(e), e

    # 7. probe
    info = await model_probe.probe(cfg())
    assert info["reachable"] and info["n_ctx_max"] == claude_cli.CONTEXT_WINDOW
    assert model_probe.resolve(cfg(), info)["context_window"] == claude_cli.CONTEXT_WINDOW
    assert (await model_probe.probe(missing))["reachable"] is False

    print("OK: claude_cli provider")


if __name__ == "__main__":
    asyncio.run(main())
