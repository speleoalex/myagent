import asyncio

from fastapi import APIRouter, HTTPException, Request
import httpx
from pydantic import BaseModel

from app.engine import model_probe
from app.models import ModelConfig
from app import config

router = APIRouter()

# Sentinel returned to the frontend in place of a stored API key. Keys are
# write-only: a PUT that sends the sentinel back keeps the saved key.
API_KEY_MASK = "********"


def _store(request: Request):
    return request.app.state.stores.models


def _masked(data: dict) -> dict:
    """Never expose stored API keys to the frontend."""
    if data.get("api_key"):
        data = {**data, "api_key": API_KEY_MASK}
    return data


def _public(data: dict) -> dict:
    """Stored config as the frontend should see it: normalized through the model
    (so legacy fields like options.num_ctx surface as context_window) and with
    the API key masked."""
    try:
        data = ModelConfig(**data).model_dump()
    except Exception:
        pass  # malformed stored config: hand it back as-is rather than 500
    return _masked(data)


@router.get("")
async def list_models(request: Request):
    return [_public(m) for m in _store(request).list_all()]


@router.get("/ollama/available")
async def list_ollama_models():
    url = config.settings.ollama_base_url.rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("models", [])
    except Exception as e:
        raise HTTPException(502, f"Cannot reach Ollama: {e}")


@router.get("/llamacpp/status")
async def llamacpp_status(request: Request):
    """Report reachability of every registered llama.cpp instance.

    Each llama.cpp model can point at its own base_url, so we ping them all
    (not just the single global settings URL) and return one entry per model.
    """
    models = [m for m in _store(request).list_all()
              if m.get("provider") == "llamacpp"]
    if not models:
        # Nothing registered yet: fall back to the global settings URL.
        models = [{"id": None, "name": "llama.cpp",
                   "base_url": config.settings.llamacpp_base_url}]

    async def check(m):
        base = (m.get("base_url") or config.settings.llamacpp_base_url).rstrip("/")
        entry = {"id": m.get("id"),
                 "name": m.get("name") or m.get("id") or "llama.cpp",
                 "base_url": base}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.get(base + "/health")
            entry["status"] = "ok"
        except Exception:
            entry["status"] = "unreachable"
        return entry

    instances = await asyncio.gather(*(check(m) for m in models))
    return {"instances": list(instances)}


class RemoteModelsRequest(BaseModel):
    """Probe an OpenAI-compatible provider for its available models. The api_key
    may be the mask sentinel when editing an existing config (model_id set), in
    which case the stored key is used."""
    base_url: str
    api_key: str = ""
    model_id: str | None = None


@router.post("/openai/available")
async def list_openai_models(req: RemoteModelsRequest, request: Request):
    api_key = req.api_key
    # Empty or masked key + an existing config: reuse the stored key — but ONLY
    # if the request targets that model's own base_url. Otherwise a caller could
    # exfiltrate the stored secret by pointing base_url at an arbitrary host.
    if api_key in ("", API_KEY_MASK) and req.model_id:
        stored = _store(request).get(req.model_id) or {}
        stored_base = (stored.get("base_url") or "").rstrip("/")
        if stored_base and stored_base == req.base_url.rstrip("/"):
            api_key = stored.get("api_key", "")
        else:
            api_key = ""

    base = req.base_url.rstrip("/")
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Provider error: HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, f"Cannot reach provider: {e}")
    # OpenAI shape: {"data": [{"id": ...}]}; some gateways return a bare array
    # or (llama.cpp) an Ollama-style {"models": [{"name": ...}]}.
    if isinstance(data, dict):
        models = data.get("data") or data.get("models") or []
    else:
        models = data
    if not isinstance(models, list):
        models = []
    ids = [
        (m.get("id") or m.get("name")) for m in models
        if isinstance(m, dict) and (m.get("id") or m.get("name"))
    ]
    return sorted(ids)


@router.get("/{model_id}/probe")
async def probe_model(model_id: str, request: Request, refresh: bool = False):
    """Query a model's server for what it is ACTUALLY serving right now.

    Useful especially for llama.cpp, whose configured `model` field is ignored
    (one model per instance): this reports the real loaded model, its
    capabilities (e.g. vision / audio / tools) and its context window, so the UI
    can show live, accurate info instead of a meaningless stored value.

    On top of the raw probe it returns the resolved `context_window` (the budget
    a request will actually get) and the `context_source` that produced it —
    that is what the model form shows next to the context field.
    """
    cfg = _store(request).get(model_id)
    if cfg is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    cfg = ModelConfig(**cfg).model_dump()  # migrate legacy options.num_ctx

    info = await model_probe.probe(cfg, force=refresh)
    resolved = model_probe.resolve(cfg, info)
    return {**info,
            "context_window": resolved["context_window"],
            "context_source": resolved["source"]}


@router.get("/{model_id}")
async def get_model(model_id: str, request: Request):
    data = _store(request).get(model_id)
    if data is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    return _public(data)


@router.post("", status_code=201)
async def create_model(model: ModelConfig, request: Request):
    store = _store(request)
    if store.exists(model.id):
        raise HTTPException(409, f"Model already exists: {model.id}")
    store.save(model.id, model.model_dump())
    return _masked(model.model_dump())


def _forget_probe(*cfgs: dict | None) -> None:
    """Drop cached probes for configs whose target may have changed, so the next
    request re-asks the server instead of reusing a stale window/capability set."""
    for cfg in cfgs:
        if cfg:
            model_probe.invalidate(cfg)


@router.put("/{model_id}")
async def update_model(model_id: str, model: ModelConfig, request: Request):
    store = _store(request)
    existing = store.get(model_id)
    if existing is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    model.id = model_id
    # API key handling (write-only — the frontend only ever holds the mask):
    #  - a non-remote provider never keeps a key;
    #  - the mask sentinel means "keep the stored key" (field untouched);
    #  - anything else (including an empty string) is taken literally, so the
    #    key can be replaced or explicitly cleared.
    if model.provider != "openai":
        model.api_key = ""
    elif model.api_key == API_KEY_MASK:
        model.api_key = existing.get("api_key", "")
    store.save(model_id, model.model_dump())
    # base_url / model may have changed: the old and new targets are both stale.
    _forget_probe(existing, model.model_dump())
    return _masked(model.model_dump())


@router.delete("/{model_id}")
async def delete_model(model_id: str, request: Request):
    store = _store(request)
    existing = store.get(model_id)
    if not store.delete(model_id):
        raise HTTPException(404, f"Model not found: {model_id}")
    _forget_probe(existing)
    return {"ok": True}
