#!/usr/bin/env python3
"""Only a LOCAL model may provide embeddings.

Run: server/.venv/bin/python tests/test_embed_local_only.py

Indexing sends the CONTENT of every document to the embedding endpoint — the
corpus, not just the question. On an app that sells itself on working offline
and keeping your documents yours, a remote embedder is a corpus leak, so it is
refused rather than warned about.

The refusal lives in TWO places on purpose, and this test is mostly about the
second one:
  1. PUT /api/system/settings answers 400 (the friendly half — see
     routers/system.py);
  2. AgentExecutor.tool_env_overrides simply exports nothing for a non-local
     model (the enforcing half). Without it, hand-editing settings.json — or
     a config restored from a backup, or a model later given an api_key —
     would quietly start shipping documents out.

Cases:
  1. a local model -> MYAGENT_EMBED_URL/_MODEL are exported;
  2. a remote (openai/anthropic) model -> nothing, even though settings.json
     names it;
  3. a LOCAL model that carries an api_key -> nothing (it is fronting for
     something remote);
  4. a dangling model id -> nothing, no exception;
  5. unset -> nothing, i.e. today's behaviour;
  6. the folder and the embedder travel in the SAME dict, so an agent can have
     one, the other, or both.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app import config                                    # noqa: E402
from app.engine.executor import AgentExecutor, Stores     # noqa: E402
from app.models import Settings                           # noqa: E402
from app.storage.store import JsonStore                   # noqa: E402
from app.tools.registry import ToolRegistry               # noqa: E402

base = Path(_tmp.name)
agents = JsonStore(base / "config" / "agents")
models = JsonStore(base / "config" / "models")
stores = Stores(agents=agents, models=models)
registry = ToolRegistry(base / "tools")

models.save("local-emb", {"id": "local-emb", "name": "Local", "provider": "ollama",
                          "model": "embeddinggemma:300m",
                          "base_url": "http://localhost:11434"})
models.save("remote-emb", {"id": "remote-emb", "name": "Remote", "provider": "openai",
                           "model": "text-embedding-3-small", "api_key": "sk-x",
                           "base_url": "https://api.openai.com"})
models.save("keyed-local", {"id": "keyed-local", "name": "Keyed", "provider": "ollama",
                            "model": "e", "base_url": "http://localhost:11434",
                            "api_key": "sk-y"})
models.save("chat", {"id": "chat", "name": "C", "provider": "llamacpp",
                     "model": "c", "base_url": "http://localhost:8080"})
agents.save("a", {"id": "a", "name": "A", "model_id": "chat", "system_prompt": "x"})
agents.save("scoped", {"id": "scoped", "name": "S", "model_id": "chat",
                       "system_prompt": "x", "folder": {"path": "/tmp"}})

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


def with_setting(value):
    config.settings = Settings(embedding_model_id=value)


async def main():
    ex = await AgentExecutor.create_for_agent("a", registry, stores)

    with_setting("local-emb")
    env = ex.tool_env_overrides()
    check("a local model is exported as an embeddings endpoint",
          env.get("MYAGENT_EMBED_URL") == "http://localhost:11434/v1/embeddings"
          and env.get("MYAGENT_EMBED_MODEL") == "embeddinggemma:300m")

    with_setting("remote-emb")
    check("a REMOTE model exports nothing, whatever settings.json says",
          "MYAGENT_EMBED_URL" not in ex.tool_env_overrides())

    with_setting("keyed-local")
    check("a local model carrying an api_key exports nothing either",
          "MYAGENT_EMBED_URL" not in ex.tool_env_overrides())

    with_setting("does-not-exist")
    check("a dangling model id exports nothing and does not raise",
          "MYAGENT_EMBED_URL" not in ex.tool_env_overrides())

    with_setting(None)
    check("unset means today's behaviour: an empty environment",
          ex.tool_env_overrides() == {})

    # 6. folder and embedder are independent.
    sc = await AgentExecutor.create_for_agent("scoped", registry, stores)
    check("a folder alone still works with no embedder",
          sc.tool_env_overrides() == {"MYAGENT_AGENT_DIR": "/tmp"})
    with_setting("local-emb")
    env = sc.tool_env_overrides()
    check("folder and embedder travel together",
          env.get("MYAGENT_AGENT_DIR") == "/tmp" and "MYAGENT_EMBED_URL" in env)

    # The router's half: a source check, because spinning up the app just to
    # assert a 400 would test FastAPI, not the rule.
    src = (ROOT / "server" / "app" / "routers" / "system.py").read_text(encoding="utf-8")
    check("the settings route refuses a remote embedding model",
          "embedding_model_id" in src and "embedding.rejection_reason" in src)

    # Both callers must ask the SAME module: vectors written by one model and
    # queried with another are noise, so a second copy of the rule would be a
    # bug waiting for a config change.
    from app.engine import embedding                      # noqa: E402
    check("the rule lives in one module, used by the executor",
          "embedding.resolve_embed_env" in
          (ROOT / "server" / "app" / "engine" / "executor.py").read_text(encoding="utf-8"))
    check("a remote model is rejected with a reason a human can read",
          "api_key" in embedding.rejection_reason({"provider": "ollama",
                                                   "api_key": "x"}))
    check("a good local model is not rejected",
          embedding.rejection_reason({"provider": "ollama", "model": "e",
                                      "base_url": "http://x"}) == "")


asyncio.run(main())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — only a local model ever becomes an embeddings endpoint, and the "
      "rule holds even when settings.json is edited by hand")
