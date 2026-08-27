"""Where the embeddings endpoint comes from — the ONE definition.

Two callers need it and must never disagree: ``AgentExecutor`` exports it to a
search tool so that tool can QUERY the index, and ``IndexService`` exports it to
the indexer so it can BUILD it. Vectors written by one model and queried with
another are noise, so a second copy of this rule would be a bug waiting for a
config change.

**Local providers only, and this is the enforcement point.** Indexing sends the
CONTENT of every document to the endpoint — the corpus, not just the question.
On an app whose premise is working offline and keeping documents yours, a remote
embedder is a corpus leak, so it is refused rather than warned about. The
settings route refuses it too, but that is only the friendly half: a
``settings.json`` restored from a backup, or a model later given an api_key,
must not be able to start shipping documents out. Same posture as
``default_model``, which never falls back onto a model carrying an api_key.
"""
from __future__ import annotations

import logging

from app import config
from app.engine.default_model import LOCAL_PROVIDERS

log = logging.getLogger(__name__)


def embedding_model_id() -> str:
    return getattr(config.settings, "embedding_model_id", None) or ""


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
    """``MYAGENT_EMBED_URL`` / ``_MODEL`` for the configured embedder, or {}.

    Resolved on every call rather than cached: the registry's process-wide tool
    environment is fixed at startup, and choosing an embedding model in Settings
    has to take effect on the next turn — the same reason
    ``default_model.invalidate()`` exists.
    """
    model_id = embedding_model_id()
    if not model_id:
        return {}
    try:
        raw = models_store.get(model_id) or {}
    except Exception:
        return {}
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
