"""Live introspection of a model server: what it is actually serving, right now.

The stored :class:`~app.models.ModelConfig` is not the whole truth. llama.cpp
ignores the configured ``model`` name (one model per instance), and *nobody*
reports the context window in the OpenAI ``/v1/models`` shape — yet the context
window is what decides how much conversation we may send. Instead of asking the
user to type that number into the model form, we ask the server:

    llama.cpp   GET  /props     -> default_generation_settings.n_ctx: the
                                   PER-SLOT window (``-c`` divided by
                                   ``--parallel``), i.e. the real budget of a
                                   single request.
    Ollama      GET  /api/ps    -> context_length of the LOADED instance, the
                                   only value actually being served.
                POST /api/show  -> capabilities + model_info["<arch>.context_length"],
                                   the model's TRAINED maximum: an upper bound,
                                   not what is being served.
    remote      GET  /v1/models -> context_length / max_model_len, when the
                                   gateway bothers to declare them (OpenRouter,
                                   vLLM, LiteLLM; OpenAI itself does not).
    anthropic   GET  /v1/models/{id} -> max_input_tokens: the context window,
                                   AND max_tokens: the per-request OUTPUT cap
                                   (a hard limit the Messages API 400s on, and
                                   the only provider here that states it).

Results are cached per (provider, base_url, model) with a short TTL: they are
live data — restarting llama.cpp with a different ``-c`` changes them — but a
chat turn must not pay a round-trip on every LLM call.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# Probe answers are live data, so the cache is short-lived; it exists to keep
# the per-turn cost at zero, not to remember forever.
CACHE_TTL = 300.0
PROBE_TIMEOUT = 6.0

# Fallbacks when the server won't say: unreachable, or an API that never
# declares its window.
FALLBACK_REMOTE = 32768  # remote windows are large — this is just a safety net
FALLBACK_LOCAL = 4096

# Ceiling applied to a PROBED output cap (see max_output_budget). Current Claude
# models declare 64K-128K of max output; using all of it as our default
# max_tokens would let one degenerate loop bill 16x what the old hardcoded 8192
# could. 32768 leaves ample room for thinking plus a large file in one tool call
# — the case that was truncating — while keeping the worst case bounded. An
# explicit `max_tokens` in the model options bypasses this entirely.
MAX_OUTPUT_CEILING = int(os.environ.get("MYAGENT_MAX_OUTPUT_CEILING") or 32768)

# What Ollama serves when we don't ask for anything (its own default, settable
# server-side via OLLAMA_CONTEXT_LENGTH — which we cannot read remotely, hence
# the override).
OLLAMA_DEFAULT_CTX = int(os.environ.get("MYAGENT_OLLAMA_DEFAULT_CTX") or 4096)

_CACHE: dict[str, tuple[float, dict]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _as_dict(cfg) -> dict:
    """Accept either a stored dict (routers) or a ModelConfig (engine)."""
    return cfg if isinstance(cfg, dict) else cfg.model_dump()


def _cache_key(c: dict) -> str:
    # Keyed by what determines the answer, not by model id: two configs pointing
    # at the same llama.cpp instance share one probe.
    return "|".join([
        c.get("provider") or "",
        (c.get("base_url") or "").rstrip("/"),
        c.get("model") or "",
    ])


def _first_int(*values) -> int | None:
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int) and v > 0:
            return v
    return None


# ---------------------------------------------------------------- probing


async def probe(cfg, *, client: httpx.AsyncClient | None = None, force: bool = False) -> dict:
    """Ask the model server what it is serving. Never raises.

    Returns ``{reachable, served_model, capabilities, n_ctx, n_ctx_max,
    max_output}`` where ``n_ctx`` is the window actually being served (when
    knowable), ``n_ctx_max`` the model's declared maximum INPUT window, and
    ``max_output`` its declared maximum OUTPUT — a separate, much smaller
    number that only the Anthropic Models API currently states.
    """
    c = _as_dict(cfg)
    key = _cache_key(c)
    now = time.monotonic()
    if not force:
        hit = _CACHE.get(key)
        if hit and (now - hit[0]) < CACHE_TTL:
            return hit[1]

    lock = _LOCKS.get(key)
    if lock is None:  # avoid allocating a Lock on every cache hit
        lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        # A concurrent turn may have filled the cache while we waited on the lock.
        hit = _CACHE.get(key)
        if hit and not force and (time.monotonic() - hit[0]) < CACHE_TTL:
            return hit[1]
        info = await _probe_now(c, client)
        _CACHE[key] = (time.monotonic(), info)
        return info


def invalidate(cfg=None) -> None:
    """Drop cached probes — one config's, or all of them (on save/delete).
    The per-key locks go too, or the dict grows one entry per (provider,
    base_url, model) ever probed; a prober still holding a dropped lock keeps
    its own reference, so the worst case is two concurrent probes racing to
    fill the same cache slot (benign)."""
    if cfg is None:
        _CACHE.clear()
        _LOCKS.clear()
        return
    key = _cache_key(_as_dict(cfg))
    _CACHE.pop(key, None)
    _LOCKS.pop(key, None)


async def _probe_now(c: dict, client: httpx.AsyncClient | None) -> dict:
    out = {
        "reachable": False,
        "served_model": c.get("model") or None,
        "capabilities": [],
        "n_ctx": None,
        "n_ctx_max": None,
        "max_output": None,
    }
    base = (c.get("base_url") or "").rstrip("/")
    if not base:
        return out

    own_client = None
    if client is None:
        if c.get("provider") == "anthropic":
            headers = {"anthropic-version": "2023-06-01"}
            if c.get("api_key"):
                headers["x-api-key"] = c["api_key"]
        else:
            headers = {"Authorization": f"Bearer {c['api_key']}"} if c.get("api_key") else {}
        own_client = httpx.AsyncClient(timeout=PROBE_TIMEOUT, headers=headers)
        client = own_client
    try:
        provider = c.get("provider") or "ollama"
        if provider == "ollama":
            await _probe_ollama(client, base, c, out)
        elif provider == "anthropic":
            await _probe_anthropic(client, base, c, out)
        else:
            await _probe_openai_compatible(client, base, c, out)
    except Exception as e:
        # Unreachable server / odd payload: keep reachable=False and let the
        # caller fall back. A probe must never break a chat turn.
        log.debug("Model probe failed (%s %s): %s", c.get("provider"), base, e)
    finally:
        if own_client is not None:
            await own_client.aclose()
    return out


def _ollama_trained_ctx(model_info: dict) -> int | None:
    """Pull "<arch>.context_length" out of /api/show's model_info blob."""
    arch = model_info.get("general.architecture")
    if arch:
        n = _first_int(model_info.get(f"{arch}.context_length"))
        if n:
            return n
    for k, v in model_info.items():
        if k.endswith(".context_length"):
            n = _first_int(v)
            if n:
                return n
    return None


async def _probe_ollama(client, base: str, c: dict, out: dict) -> None:
    model = c.get("model") or ""
    resp = await client.post(base + "/api/show", json={"model": model},
                             timeout=PROBE_TIMEOUT)
    resp.raise_for_status()
    d = resp.json()
    out["capabilities"] = d.get("capabilities") or []
    out["n_ctx_max"] = _ollama_trained_ctx(d.get("model_info") or {})
    out["reachable"] = True

    # /api/ps lists only models currently in memory, so a miss is normal — but
    # when there is a hit it reports the window the instance is really running.
    try:
        ps = (await client.get(base + "/api/ps", timeout=PROBE_TIMEOUT)).json()
        for e in ps.get("models") or []:
            if not isinstance(e, dict):
                continue
            if model in (e.get("model"), e.get("name")):
                out["n_ctx"] = _first_int(e.get("context_length"))
                break
    except Exception:
        pass


async def _probe_anthropic(client, base: str, c: dict, out: dict) -> None:
    """The Anthropic Models API declares both windows outright:
    GET /v1/models/{id} carries max_input_tokens (the context window) and
    max_tokens (the OUTPUT cap the Messages API enforces per request)."""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    model = (c.get("model") or "").strip()
    if not model:
        return
    resp = await client.get(f"{base}/models/{model}", timeout=PROBE_TIMEOUT)
    resp.raise_for_status()
    d = resp.json()
    out["reachable"] = True
    out["served_model"] = d.get("id") or out["served_model"]
    out["n_ctx_max"] = _first_int(d.get("max_input_tokens"))
    # Careful: this key means the OUTPUT cap here, while the same name on an
    # OpenAI-compatible listing (and in ModelConfig.options) means the value WE
    # request. Never read one as the other — that is how a 1M context window
    # becomes a 1M max_tokens and a 400.
    out["max_output"] = _first_int(d.get("max_tokens"))


async def _probe_openai_compatible(client, base: str, c: dict, out: dict) -> None:
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    resp = await client.get(url, timeout=PROBE_TIMEOUT)
    resp.raise_for_status()
    d = resp.json()
    # llama.cpp returns BOTH an Ollama-style "models" array (which carries
    # capabilities) and the OpenAI-standard "data" array (which does not).
    # Prefer "models" so capabilities survive.
    if isinstance(d, dict):
        entries = (d.get("models") or []) + (d.get("data") or [])
    elif isinstance(d, list):
        entries = d
    else:
        entries = []
    entries = [e for e in entries if isinstance(e, dict)]
    out["reachable"] = True

    single = c.get("provider") == "llamacpp" or len(entries) == 1
    if single:
        # One model per instance: its entry is authoritative (llama.cpp ignores
        # the configured name, so this is the only way to know what is loaded).
        for e in entries:
            out["served_model"] = e.get("name") or e.get("id") or out["served_model"]
            if e.get("capabilities"):
                out["capabilities"] = e["capabilities"]
                break

    # A remote gateway lists hundreds of models: only the entry matching the
    # configured one says anything about ITS context window.
    wanted = (c.get("model") or "").strip()
    src = next((e for e in entries if (e.get("id") or e.get("name")) == wanted), None)
    if src is None and single and entries:
        src = entries[0]
    if src:
        top = src.get("top_provider") if isinstance(src.get("top_provider"), dict) else {}
        out["n_ctx_max"] = _first_int(
            src.get("context_length"),      # OpenRouter
            src.get("max_model_len"),       # vLLM
            top.get("context_length"),      # OpenRouter, per-provider detail
        )

    if c.get("provider") == "llamacpp":
        # /props reports the PER-SLOT window (-c / --parallel), which is exactly
        # what a single request can use.
        try:
            props = (await client.get(base + "/props", timeout=PROBE_TIMEOUT)).json()
            gen = props.get("default_generation_settings") or {}
            out["n_ctx"] = _first_int(gen.get("n_ctx"))
        except Exception:
            pass


# ---------------------------------------------------------------- resolving


def resolve(cfg, info: dict) -> dict:
    """Decide the token budget for one request, and report where it came from.

    ``source`` is one of ``explicit`` (the config's context_window),
    ``served`` (what the server reports it is running), ``model_max`` (the
    model's declared maximum) or ``default`` (per-provider fallback).
    """
    c = _as_dict(cfg)
    provider = c.get("provider") or "ollama"
    explicit = _first_int(c.get("context_window")) or 0
    served = _first_int(info.get("n_ctx")) or 0
    maximum = _first_int(info.get("n_ctx_max")) or 0

    if provider == "llamacpp":
        # The window is fixed when the server starts (-c) and the payload can't
        # change it: the probe is authoritative, and an explicit value can only
        # LOWER our budget — asking for more than the server allocated just
        # overflows the slot.
        if served and explicit:
            return _out(min(explicit, served), "explicit" if explicit <= served else "served")
        if served:
            return _out(served, "served")
        return _out(explicit or FALLBACK_LOCAL, "explicit" if explicit else "default")

    if provider == "ollama":
        # Ollama allocates the KV cache for whatever num_ctx we request (capped
        # by the trained window), so an explicit value IS the served window.
        # Without one we deliberately do NOT request the full trained window —
        # that would multiply VRAM use on a machine that was fine before — and
        # stay at whatever Ollama serves on its own.
        if explicit:
            return _out(min(explicit, maximum) if maximum else explicit,
                        "model_max" if maximum and explicit > maximum else "explicit")
        if served:
            return _out(served, "served")
        return _out(min(OLLAMA_DEFAULT_CTX, maximum) if maximum else OLLAMA_DEFAULT_CTX,
                    "default")

    # Remote OpenAI-compatible: nothing is allocated on our behalf, the window
    # is whatever the model has. Most APIs never declare it.
    if explicit:
        return _out(explicit, "explicit")
    if served:
        return _out(served, "served")
    if maximum:
        return _out(maximum, "model_max")
    return _out(FALLBACK_REMOTE, "default")


def _out(n: int, source: str) -> dict:
    return {"context_window": int(n), "source": source}


async def max_output_budget(cfg, *, client: httpx.AsyncClient | None = None) -> int | None:
    """The per-request OUTPUT cap, asked for rather than guessed. Never raises.

    Returns None when nothing declares one, so the caller keeps its own
    conservative fallback: a value that is too HIGH is a 400 from the Messages
    API, not a degraded answer, so silence must not become optimism.

    An explicit ``options["max_tokens"]`` always wins and skips the probe — it
    is the escape hatch for asking a model for its full declared output, above
    MAX_OUTPUT_CEILING.
    """
    c = _as_dict(cfg)
    explicit = _first_int((c.get("options") or {}).get("max_tokens"))
    if explicit:
        return explicit
    probed = _first_int((await probe(cfg, client=client)).get("max_output"))
    return min(probed, MAX_OUTPUT_CEILING) if probed else None


async def context_budget(cfg, *, client: httpx.AsyncClient | None = None) -> int:
    """Token budget for one request. Never raises: a failed probe falls back."""
    c = _as_dict(cfg)
    # An explicit value already settles it for a remote provider, so don't spend
    # a round-trip on someone else's API. Local servers are on localhost and
    # llama.cpp has to be asked anyway (its fixed window clamps the value).
    if c.get("provider") in ("openai", "anthropic"):
        explicit = _first_int(c.get("context_window"))
        if explicit:
            return explicit
    info = await probe(cfg, client=client)
    return resolve(cfg, info)["context_window"]
