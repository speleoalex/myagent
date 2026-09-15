#!/usr/bin/env python3
"""Model KINDS (chat / embedding / image) and the instance identity.

Run: server/.venv/bin/python tests/test_model_kinds_identity.py

Two changes made together in Settings (2026-09-15):

  1. every model select lists ONLY its own kind. The store used to know two
     kinds, chat and image, and the embedder select listed "any local chat
     model" — so an image generator or a 30B chat model could be picked as an
     embedder and fail at index time. Now `kind = "embedding"` exists, the
     embedder policy refuses an image generator by kind, and an unknown kind
     still degrades to chat (older stores);
  2. `instance_name` / `instance_color` on Settings, validated by the PUT and
     exposed by GET /system/info, so the navbar can say WHICH server this is.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from fastapi import HTTPException                          # noqa: E402

from app import config                                     # noqa: E402
from app.engine import embedding, imagegen                 # noqa: E402
from app.models import (CHAT_KIND, EMBED_KIND, IMAGE_KIND, MODEL_KINDS,  # noqa: E402
                        ModelConfig, Settings)
from app.routers import system                             # noqa: E402

failures = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


# ---- 1. kinds -------------------------------------------------------------
check("three kinds", set(MODEL_KINDS) == {CHAT_KIND, EMBED_KIND, IMAGE_KIND})
check("embedding kind is kept",
      ModelConfig(id="e", name="e", provider="ollama", model="x", kind="embedding").kind == EMBED_KIND)
check("kind is case-insensitive",
      ModelConfig(id="e", name="e", provider="ollama", model="x", kind="Image").kind == IMAGE_KIND)
check("unknown kind degrades to chat",
      ModelConfig(id="e", name="e", provider="ollama", model="x", kind="banana").kind == CHAT_KIND)
check("missing kind is chat",
      ModelConfig(id="e", name="e", provider="ollama", model="x").kind == CHAT_KIND)

local = {"id": "emb", "name": "emb", "provider": "ollama", "model": "embeddinggemma:300m",
         "base_url": "http://localhost:11434"}
check("embedder: local embedding-kind model accepted",
      embedding.rejection_reason({**local, "kind": EMBED_KIND}) == "")
check("embedder: local chat-kind model still accepted (older stores)",
      embedding.rejection_reason({**local, "kind": CHAT_KIND}) == "")
check("embedder: image generator refused by kind",
      "image" in embedding.rejection_reason({**local, "kind": IMAGE_KIND}))
check("embedder: remote still refused",
      embedding.rejection_reason({**local, "kind": EMBED_KIND, "provider": "openai"}) != "")
check("image: embedding model refused",
      imagegen.rejection_reason({**local, "kind": EMBED_KIND}) != "")

# ---- 2. identity ----------------------------------------------------------
check("identity defaults empty",
      Settings().instance_name == "" and Settings().instance_color == "")
check("older settings.json without the fields still loads",
      Settings(**{"default_model_id": None, "debug": False}).instance_name == "")


class _Models:
    def __init__(self, rows): self.rows = rows
    def get(self, key): return self.rows.get(key)


def put(**fields):
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        stores=SimpleNamespace(models=_Models({})))))
    return asyncio.run(system.update_settings(Settings(**fields), req))


def put_fails(**fields):
    try:
        put(**fields)
    except HTTPException as e:
        return e.status_code == 400
    return False


put(instance_name="  Casa  ", instance_color="#AABBCC")
check("PUT trims the name", config.settings.instance_name == "Casa")
check("PUT lowercases the color", config.settings.instance_color == "#aabbcc")
check("PUT persisted", Settings(**config.load_settings().model_dump()).instance_name == "Casa")
check("GET /system/info carries both",
      asyncio.run(system.info())["instance_name"] == "Casa"
      and asyncio.run(system.info())["instance_color"] == "#aabbcc")
put(instance_name="", instance_color="")
check("PUT clears both", config.settings.instance_name == "" and config.settings.instance_color == "")
check("name longer than the max is refused", put_fails(instance_name="x" * (system.INSTANCE_NAME_MAX + 1)))
check("name at the max is accepted", not put_fails(instance_name="x" * system.INSTANCE_NAME_MAX))
check("short hex color refused", put_fails(instance_color="#abc"))
check("named color refused", put_fails(instance_color="red"))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all ok")
