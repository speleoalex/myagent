#!/usr/bin/env python3
"""The in-process (fastembed) embedding backend.

Run: server/.venv/bin/python tests/test_local_embedder.py

Semantic search over an OpenAI-compatible endpoint needs a model pulled,
registered and selected. The in-process backend removes all three, and this
test pins the four properties that make it safe to reach for. Every one of them
is a thing that would otherwise fail SILENTLY.

  1. Nothing is chosen automatically. With `embedding_model_id` unset there is
     no semantic search even when fastembed is installed — deliberately unlike
     `default_model.resolve_default`, which falls back because the alternative
     there is a hard failure on the user's first message. Here the alternative
     is a perfectly good state, and an installed package is not consent to
     spend CPU and 241 MB on somebody's folders.
  2. A REGISTERED model called `local` beats the sentinel — the same rule an
     agent actually named `auto` already gets.
  3. The two backends never both apply: an endpoint wins, and `MYAGENT_EMBED_*`
     inherited from the service environment must not survive next to
     MYAGENT_EMBED_LOCAL (that pair would index with one model and query with
     the other, silently).
  4. Constructing the local embedder touches NOTHING — no fastembed import, no
     ONNX session, no network. `open_index()` builds one just to answer "is
     semantic search on?", and it must not pay for a model to do it.
  5. A search may never download the model. Only an index run may, because that
     runs under the IndexService, which throttles, nices, times out, retries
     and can be stopped from the UI.

The embedding QUALITY is not tested here: it needs the 241 MB model, so it
lives in the manual check documented in library/README.md.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "tools" / "library" / "local_search"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

import semindex                                            # noqa: E402
from app import config                                     # noqa: E402
from app.engine import embedding                           # noqa: E402
from app.models import Settings                            # noqa: E402
from app.storage.store import JsonStore                    # noqa: E402

base = Path(_tmp.name)
models = JsonStore(base / "config" / "models")
models.save("emb", {"id": "emb", "name": "E", "provider": "ollama",
                    "model": "embeddinggemma:300m",
                    "base_url": "http://localhost:11434"})

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


def clean_env():
    for k in ("MYAGENT_EMBED_URL", "MYAGENT_EMBED_MODEL", "MYAGENT_EMBED_LOCAL",
              "HF_HUB_OFFLINE"):
        os.environ.pop(k, None)


# --- 1. nothing is automatic ---------------------------------------------
clean_env()
config.settings = Settings(embedding_model_id=None)
check("unset means no embedder, even with fastembed installed",
      embedding.resolve_embed_env(models) == {})

# --- 2. the sentinel, and a real model that claims the id ----------------
config.settings = Settings(embedding_model_id=embedding.LOCAL_ID)
env = embedding.resolve_embed_env(models)
if embedding.local_available():
    check("the 'local' sentinel exports MYAGENT_EMBED_LOCAL",
          env == {"MYAGENT_EMBED_LOCAL": "1"})
else:
    check("without fastembed the sentinel exports nothing rather than a "
          "setting that can only fail", env == {})

models.save("local", {"id": "local", "name": "Real one", "provider": "llamacpp",
                      "model": "bge-m3", "base_url": "http://localhost:8080"})
env = embedding.resolve_embed_env(models)
check("a REGISTERED model named 'local' wins over the sentinel",
      env.get("MYAGENT_EMBED_MODEL") == "bge-m3"
      and "MYAGENT_EMBED_LOCAL" not in env)
models.delete("local")

# --- 3. the two backends never both apply --------------------------------
clean_env()
os.environ["MYAGENT_EMBED_URL"] = "http://localhost:11434/v1/embeddings"
os.environ["MYAGENT_EMBED_MODEL"] = "endpoint-model"
os.environ["MYAGENT_EMBED_LOCAL"] = "1"
emb = semindex.embedder_from_env()
check("an endpoint wins over the local backend when both are in the env",
      isinstance(emb, semindex.Embedder) and emb.model == "endpoint-model")

src = (ROOT / "server" / "app" / "engine" / "index_service.py").read_text(encoding="utf-8")
check("the indexer strips an inherited MYAGENT_EMBED_URL when it was told to "
      "embed locally", 'env.pop("MYAGENT_EMBED_URL", None)' in src)

# --- 4. constructing costs nothing --------------------------------------
clean_env()
os.environ["MYAGENT_EMBED_LOCAL"] = "1"
check("fastembed is not imported yet", "fastembed" not in sys.modules)
t0 = time.monotonic()
emb = semindex.embedder_from_env()
elapsed = time.monotonic() - t0
check("MYAGENT_EMBED_LOCAL yields the local backend",
      isinstance(emb, semindex.LocalEmbedder))
check("constructing it is instant (no model, no session, no network)",
      elapsed < 0.5)
check("constructing it did NOT import fastembed",
      "fastembed" not in sys.modules)
check("the default model is multilingual — an Italian question over English "
      "manuals is the case this index exists for",
      "multilingual" in semindex.DEFAULT_LOCAL_MODEL)
check("the invalidation key names the backend, so the same weights served over "
      "HTTP and run locally do not silently share an index",
      emb.model.startswith("fastembed:"))
check("the model cache lives under the cache dir: derived, large, deletable",
      "cache" in semindex.embed_cache_dir())

os.environ["MYAGENT_EMBED_LOCAL"] = "some/other-model"
check("a model name in the env is honoured",
      semindex.embedder_from_env().name == "some/other-model")

# --- 5. only an index run may download ----------------------------------
clean_env()
os.environ["MYAGENT_EMBED_LOCAL"] = "1"
check("the search path builds an embedder that refuses to download",
      semindex.embedder_from_env().allow_download is False)
check("an index run is allowed to",
      semindex.embedder_from_env(allow_download=True).allow_download is True)

sem_src = (ROOT / "server" / "tools" / "library" / "local_search"
           / "semindex.py").read_text(encoding="utf-8")
check("the download gate is fastembed's own offline switch, not a guess at its "
      "cache layout", 'HF_HUB_OFFLINE' in sem_src)
check("open_index does not pass allow_download",
      "embedder_from_env()" in sem_src)
check("--index is what turns downloading on",
      "allow_download=bool(args.index)" in sem_src)

# The IndexService sends --ocr for a request that asks for it; argparse exits 2
# on an unknown flag, so a missing --ocr meant a run that could only ever fail,
# retried with backoff forever.
check("the CLI accepts every flag the IndexService sends",
      '"--ocr"' in sem_src and '"--throttle-ms"' in sem_src)

clean_env()

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — the in-process embedder is opt-in, never automatic, never both "
      "backends at once, free to construct, and can only download from an "
      "index run")
