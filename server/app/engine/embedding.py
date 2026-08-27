"""Where the embeddings come from — the ONE definition.

Two callers need it and must never disagree: ``AgentExecutor`` exports it to a
search tool so that tool can QUERY the index, and ``IndexService`` exports it to
the indexer so it can BUILD it. Vectors written by one model and queried with
another are noise, so a second copy of this rule would be a bug waiting for a
config change.

There are two backends, chosen by ``Settings.embedding_model_id``:

- the id of a **registered model**, queried over its OpenAI-compatible
  ``/v1/embeddings`` endpoint;
- the reserved id ``local`` (:data:`LOCAL_ID`), meaning **in this process**, via
  fastembed — no endpoint, no registered model, no second server.

**Only local providers, and this is the enforcement point.** Indexing sends the
CONTENT of every document to the embedder — the corpus, not just the question.
On an app whose premise is working offline and keeping documents yours, a remote
embedder is a corpus leak, so it is refused rather than warned about. The
settings route refuses it too, but that is only the friendly half: a
``settings.json`` restored from a backup, or a model later given an api_key,
must not be able to start shipping documents out. Same posture as
``default_model``, which never falls back onto a model carrying an api_key. The
``local`` backend needs none of this policing — it cannot reach a network — but
it goes through the same function so there is still one answer to "which
embedder is in use".

**Nothing is chosen automatically.** With ``embedding_model_id`` unset there is
no semantic search and ``local_search`` does exactly what it always did, even
when fastembed happens to be installed. Deliberately unlike
``default_model.resolve_default``, which falls back because the alternative
there is a hard failure on the user's first message: here the alternative is a
perfectly good state, and an installed package is not consent to spend CPU and
241 MB of disk embedding somebody's folders.
"""
from __future__ import annotations

import logging

from app import config
from app.engine.default_model import LOCAL_PROVIDERS

log = logging.getLogger(__name__)

#: Reserved ``embedding_model_id`` meaning "in this process, via fastembed".
#: A registered model that really is called ``local`` wins over the sentinel —
#: same rule as an agent actually named ``auto``.
LOCAL_ID = "local"


def embedding_model_id() -> str:
    return getattr(config.settings, "embedding_model_id", None) or ""


def local_available() -> bool:
    """True when the in-process backend could be used.

    ``find_spec`` and not an import: this is called on every settings page load
    and every index status poll, and importing fastembed pulls onnxruntime and
    tokenizers to answer a yes/no question. The result is memoised because the
    answer cannot change without restarting the process that would use it.

    Note what this does NOT do: it does not import anything from the tools
    layer. The tool is a copy-on-write overlay the user may have overridden, so
    the core may never import it (that is why IndexService runs semindex.py as
    a subprocess). Whether ``fastembed`` is importable is a fact about the venv
    the subprocess will run in, which the core is entitled to check directly.
    """
    global _local_ok
    if _local_ok is None:
        try:
            import importlib.util
            _local_ok = importlib.util.find_spec("fastembed") is not None
        except Exception:
            _local_ok = False
    return _local_ok


_local_ok: bool | None = None


def rejection_reason(raw: dict) -> str:
    """Why this model config cannot provide embeddings, or "" if it can."""
    if not raw:
        return "no such model"
    if raw.get("api_key"):
        return "it carries an api_key"
    if raw.get("provider") not in LOCAL_PROVIDERS:
        return f"provider {raw.get('provider')!r} is not local"
    if not (raw.get("base_url") or "").strip():
        return "it has no base_url"
    if not (raw.get("model") or "").strip():
        return "it names no model"
    return ""


def resolve_embed_env(models_store) -> dict[str, str]:
    """The environment that tells a tool which embedder to use, or {}.

    ``MYAGENT_EMBED_URL`` / ``_MODEL`` for an endpoint, ``MYAGENT_EMBED_LOCAL``
    for the in-process one. Resolved on every call rather than cached: the
    registry's process-wide tool environment is fixed at startup, and choosing
    an embedding model in Settings has to take effect on the next turn — the
    same reason ``default_model.invalidate()`` exists.
    """
    model_id = embedding_model_id()
    if not model_id:
        return {}
    try:
        raw = models_store.get(model_id) or {}
    except Exception:
        return {}
    # The sentinel, but only if no real model claimed that id.
    if model_id == LOCAL_ID and not raw:
        if not local_available():
            log.warning("embedding_model_id is %r but fastembed is not "
                        "installed: semantic search stays off (install it with "
                        "`server/.venv/bin/pip install fastembed`)", LOCAL_ID)
            return {}
        return {"MYAGENT_EMBED_LOCAL": "1"}
    why = rejection_reason(raw)
    if why:
        log.warning("embedding_model_id %r cannot be used (%s): indexing would "
                    "send your documents there, so semantic search stays off",
                    model_id, why)
        return {}
    return {
        "MYAGENT_EMBED_URL": f"{raw['base_url'].rstrip('/')}/v1/embeddings",
        "MYAGENT_EMBED_MODEL": raw["model"],
    }
