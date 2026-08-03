"""Pick a model that actually answers, when the configured default doesn't.

A fresh install seeds ``default_model_id: "llama-cpp"`` pointing at
``localhost:8080``, and all seven seed agents inherit it. The most common
starting point — Ollama installed, a model pulled — therefore failed on the
very first message, with a bare ``All connection attempts failed``. This module
is what turns that into an answer.

Three rules hold the design together:

- **Nothing is ever written.** ``settings.json`` keeps the user's choice, so the
  moment their backend comes back the fallback stops happening on its own. The
  alternative (rewrite the setting on first boot) races the backend at boot —
  ``deploy.sh`` has no ``After=ollama.service`` — and can never self-correct.
- **A remote default is never second-guessed.** ``model_probe`` reports an
  OpenAI-compatible gateway that doesn't serve ``/v1/models`` as unreachable, so
  probing a working remote model would silently divert the turn to a local one.
  Fallback applies only when the default is local (or gone).
- **Never fall back TO a model with an ``api_key``.** Quietly moving a turn off
  a local model and onto a paid API is a cost and privacy surprise nobody asked
  for — the opposite of what an offline-first assistant should do when its
  backend hiccups.

The result is memoised because ``call_agent`` builds one executor per
delegation: without it a single ``master`` turn would re-scan ``/api/tags``
once per sub-agent. A degraded answer is held for ``MISS_TTL`` only, so "start
Ollama, send another message" works instead of waiting out the full window.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app import config
from app.engine import model_probe
from app.models import ModelConfig

log = logging.getLogger(__name__)

# A healthy answer is as stable as the probe behind it; a degraded one must
# expire fast, because the user is probably fixing it right now.
HIT_TTL = model_probe.CACHE_TTL  # 300s
MISS_TTL = 10.0
TAGS_TIMEOUT = 2.0

LOCAL_PROVIDERS = ("ollama", "llamacpp")

# id of the ephemeral config built for a pulled-but-unregistered Ollama model.
# Never saved, so it cannot collide with a stored model of the same name.
AUTO_ID = "auto"

# key -> (expiry, model_id | None, ephemeral dict | None, note, error)
_MEMO: dict[tuple, tuple] = {}
_LOCK = asyncio.Lock()


def invalidate() -> None:
    """Forget every memoised decision (settings or models changed)."""
    _MEMO.clear()


async def _ollama_tags(base_url: str) -> list[dict]:
    """Models actually pulled on the Ollama server. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=TAGS_TIMEOUT) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/tags")
            resp.raise_for_status()
            return resp.json().get("models") or []
    except Exception as e:
        log.debug("Ollama tag list unavailable at %s: %s", base_url, e)
        return []


def _tag_rank(entry: dict) -> tuple:
    """Tool-capable first, then alphabetical.

    This is a tool-calling agent platform: a model without native tool calling
    is driven through the slower text protocol, so it is the worse automatic
    pick. Older Ollama builds omit `capabilities` — assume usable rather than
    disqualify every model on an old server.
    """
    caps = entry.get("capabilities")
    name = entry.get("model") or entry.get("name") or ""
    if caps is None:
        return (1, name)
    return (0 if "tools" in caps else 2, name)


def _auto_config(tag: str, base_url: str) -> ModelConfig:
    return ModelConfig(
        id=AUTO_ID,
        name=f"{tag} (auto-detected)",
        provider="ollama",
        model=tag,
        base_url=base_url,
    )


async def _reachable(cfg: ModelConfig, force: bool) -> bool:
    info = await model_probe.probe(cfg, force=force)
    return bool(info.get("reachable"))


async def _find_fallback(
    models_store, force: bool
) -> tuple[ModelConfig | None, list[str]]:
    """First reachable local model, and the URLs we tried (for the error)."""
    ollama_url = config.settings.ollama_base_url
    llamacpp_url = config.settings.llamacpp_base_url
    tried: list[str] = []

    # 1. A registered local model that answers. Sorted for determinism: the same
    #    machine must not pick a different model on every restart.
    candidates = [
        ModelConfig(**d)
        for d in sorted(models_store.list_all(), key=lambda d: d.get("id") or "")
        if d.get("provider") in LOCAL_PROVIDERS and not d.get("api_key")
    ]
    if candidates:
        results = await asyncio.gather(
            *(_reachable(c, force) for c in candidates), return_exceptions=True
        )
        for cfg, ok in zip(candidates, results):
            if ok is True:
                return cfg, tried
        tried += sorted({(c.base_url or "").rstrip("/") for c in candidates})

    # 2. A model pulled into Ollama that nobody registered. This is the case
    #    that makes a fresh install work: the seeds name gemma4/qwen3, and the
    #    user has whatever they happened to pull.
    tags = await _ollama_tags(ollama_url)
    if tags:
        best = sorted(tags, key=_tag_rank)[0]
        tag = best.get("model") or best.get("name")
        if tag:
            return _auto_config(tag, ollama_url), tried
    if ollama_url.rstrip("/") not in tried:
        tried.append(ollama_url.rstrip("/"))

    # 3. A llama.cpp server with no registered model pointing at it. llama.cpp
    #    ignores the model name (one model per instance), so any name works.
    fallback = ModelConfig(id=AUTO_ID, name="llama.cpp (auto-detected)",
                           provider="llamacpp", model="default",
                           base_url=llamacpp_url)
    if await _reachable(fallback, force):
        return fallback, tried
    if llamacpp_url.rstrip("/") not in tried:
        tried.append(llamacpp_url.rstrip("/"))

    return None, tried


def _no_backend_error(tried: list[str]) -> str:
    return (
        "No LLM backend is answering (tried: "
        + ", ".join(tried or ["nothing configured"])
        + "). Start Ollama ('ollama serve' then 'ollama pull qwen3') or a "
          "llama.cpp server, or add a remote model under Models."
    )


async def resolve_default(
    models_store, configured_id: str | None
) -> tuple[ModelConfig, str | None]:
    """The model to run this turn on, plus a note when it is not the configured one.

    Raises ``ValueError`` when nothing at all is reachable; both chat routers
    already turn that into a 404 whose body the UI renders as a sentence.
    """
    key = (configured_id or "", config.settings.ollama_base_url,
           config.settings.llamacpp_base_url)

    async with _LOCK:
        hit = _MEMO.get(key)
        now = time.monotonic()
        force = False
        if hit:
            expiry, model_id, ephemeral, note, error = hit
            if now < expiry:
                if error:
                    raise ValueError(error)
                if ephemeral:
                    return ModelConfig(**ephemeral), note
                data = models_store.get(model_id)
                if data:
                    return ModelConfig(**data), note
                # The model was deleted while memoised: fall through and redo.
            # A stale degraded answer means the user may have just fixed things,
            # so bypass model_probe's own 300s cache on the retry.
            force = bool(note or error)

        cfg, note, error = await _decide(models_store, configured_id, force)
        ttl = HIT_TTL if (cfg is not None and not note) else MISS_TTL
        _MEMO[key] = (
            time.monotonic() + ttl,
            cfg.id if cfg is not None else None,
            cfg.model_dump() if (cfg is not None and cfg.id == AUTO_ID) else None,
            note,
            error,
        )
        if error:
            raise ValueError(error)
        return cfg, note


async def _decide(
    models_store, configured_id: str | None, force: bool
) -> tuple[ModelConfig | None, str | None, str | None]:
    """(config, note, error) — the uncached decision."""
    configured: ModelConfig | None = None
    if configured_id:
        data = models_store.get(configured_id)
        if data:
            configured = ModelConfig(**data)

    if configured is not None:
        # A remote default is taken at its word: see the module docstring.
        if configured.provider not in LOCAL_PROVIDERS:
            return configured, None, None
        if await _reachable(configured, force):
            return configured, None, None

    fallback, tried = await _find_fallback(models_store, force)
    if fallback is None:
        if configured is not None:
            return None, None, (
                f"'{configured.name}' is not answering at {configured.base_url}, "
                f"and no other backend is reachable. Start it, or choose a "
                f"different default model in Settings."
            )
        return None, None, _no_backend_error(tried)

    if configured is not None:
        note = (f"{configured.name} is not answering at {configured.base_url} — "
                f"answering with {fallback.name} instead. "
                f"Pick a default in Settings to make this permanent.")
    elif configured_id:
        note = (f"The default model '{configured_id}' no longer exists — "
                f"answering with {fallback.name} instead. "
                f"Choose a default in Settings.")
    else:
        note = (f"No default model is configured — answering with "
                f"{fallback.name}. Choose one in Settings to make it permanent.")
    log.warning("Default model fallback: %s", note)
    return fallback, note, None
