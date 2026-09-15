"""Which model generates the images — the ONE definition.

Sibling of :mod:`app.engine.embedding`, and separate from it on purpose: the two
answer the same shape of question ("a model chosen for something that is not the
chat") under **opposite** policies, and folding them together would mean writing
one rule and living with it in the place where it is wrong.

- The embedder may only ever be LOCAL, because indexing sends the CONTENT of
  every document to it — the corpus, not the question.
- The image generator MAY be remote. What leaves the machine is one sentence the
  user deliberately wrote in order to get a picture back. Refusing a remote
  endpoint would protect nothing and would leave a laptop that cannot run a
  diffusion model with no image generation at all.

So there is no ``rejection_reason`` about api_keys here. What is still refused is
a config that cannot possibly work: no base_url, or the wrong ``kind`` — pointing
the image tool at an LLM produces a confusing 404 from a chat endpoint instead of
a sentence saying what is actually wrong.

**Two wire formats, one transport.** ``provider`` selects between
``POST /v1/images/generations`` (OpenAI's, and copied by stable-diffusion.cpp's
``sd-server`` among others) and ``POST /sdapi/v1/txt2img`` (Automatic1111's, also
spoken by Forge, SwarmUI and again sd-server). Both are plain HTTP, which is why
"local or remote" is a matter of ``base_url`` and nothing else: a generator on
this machine, one on another node of the overlay network, and a paid API are the
same three fields with different values, not three mechanisms.

**One model, two tools.** The same provider also names the EDITING endpoint
(``/v1/images/edits`` and ``/sdapi/v1/img2img``), exported alongside as
``MYAGENT_IMAGE_EDIT_URL`` for the ``edit_image`` tool. A model that can draw
can also redraw: choosing it once in Settings switches both tools on, and there
is no second setting to forget.

**Nothing is chosen automatically.** With ``image_model_id`` unset the tool is
still installed but refuses with an instruction to go and pick one — the same
posture as the embedder, and deliberately unlike ``default_model``, which falls
back because there the alternative is a hard failure on the user's first message.
"""
from __future__ import annotations

import json
import logging

from app import config
from app.models import IMAGE_KIND

log = logging.getLogger(__name__)

#: Wire format per provider, and the path appended to ``base_url`` for
#: text-to-image (``generate_image``).
ENDPOINTS = {
    "openai": "/v1/images/generations",
    "a1111": "/sdapi/v1/txt2img",
}

#: Same providers, the image-to-image path (``edit_image``): the whole picture
#: guided by a source, or only a masked region.
EDIT_ENDPOINTS = {
    "openai": "/v1/images/edits",
    "a1111": "/sdapi/v1/img2img",
}

#: Options forwarded to the backend as generation defaults. Free-form values in
#: ``ModelConfig.options`` that are not in here are passed through too: a
#: backend knob we have never heard of is the user's business, and the tool
#: merges them under whatever it computes per call.
KNOWN_OPTIONS = ("width", "height", "steps", "cfg_scale", "sampler",
                 "negative_prompt", "seed", "size")


def image_model_id() -> str:
    return getattr(config.settings, "image_model_id", None) or ""


def rejection_reason(raw: dict) -> str:
    """Why this model config cannot generate images, or "" if it can."""
    if not raw:
        return "no such model"
    if raw.get("kind") != IMAGE_KIND:
        return "it is a chat model, not an image generator"
    if raw.get("provider") not in ENDPOINTS:
        return (f"provider {raw.get('provider')!r} has no image endpoint "
                f"(expected one of {', '.join(sorted(ENDPOINTS))})")
    if not (raw.get("base_url") or "").strip():
        return "it has no base_url"
    return ""


def endpoint_url(raw: dict, path: str | None = None) -> str:
    """The full URL the tool will POST to; ``path`` defaults to text-to-image.

    A base_url that already ends in the endpoint's path is left alone, so
    pasting the complete URL out of a backend's own documentation works instead
    of producing ``/v1/images/generations/v1/images/generations``. A base_url
    pasted WITH the generation path still yields the right editing URL: the
    known tail is stripped before the other path goes on.
    """
    base = (raw.get("base_url") or "").strip().rstrip("/")
    provider = raw["provider"]
    if path is None:
        path = ENDPOINTS[provider]
    if base.endswith(path):
        return base
    for known in (ENDPOINTS[provider], EDIT_ENDPOINTS[provider]):
        if base.endswith(known):
            base = base[: -len(known)].rstrip("/")
            break
    # OpenAI-compatible base_urls are conventionally written with the /v1 on:
    # the chat side has the same wart (see _fetch_remote_models in the models
    # router), and the two must not disagree about what a base_url means.
    if path.startswith("/v1/") and base.endswith("/v1"):
        return base + path[len("/v1"):]
    return base + path


def resolve_image_env(models_store) -> dict[str, str]:
    """The environment that tells the image tools where to generate/edit, or {}.

    Resolved on every call rather than cached, for the same reason
    :func:`app.engine.embedding.resolve_embed_env` is: the registry's
    process-wide tool environment is fixed at startup, and choosing an image
    model in Settings has to take effect on the next turn.

    The api_key travels in the environment and never in the tool's parameters:
    the parameters are written BY the model and come back to it in the trace, so
    a secret placed there would be one prompt away from being read out loud.
    """
    model_id = image_model_id()
    if not model_id:
        return {}
    try:
        raw = models_store.get(model_id) or {}
    except Exception:
        return {}
    why = rejection_reason(raw)
    if why:
        log.warning("image_model_id %r cannot be used (%s): image generation "
                    "stays off", model_id, why)
        return {}
    env = {
        "MYAGENT_IMAGE_URL": endpoint_url(raw),
        "MYAGENT_IMAGE_EDIT_URL": endpoint_url(raw, EDIT_ENDPOINTS[raw["provider"]]),
        "MYAGENT_IMAGE_FORMAT": raw["provider"],
        "MYAGENT_IMAGE_NAME": raw.get("name") or model_id,
    }
    if (raw.get("model") or "").strip():
        env["MYAGENT_IMAGE_MODEL"] = raw["model"].strip()
    if raw.get("api_key"):
        env["MYAGENT_IMAGE_KEY"] = raw["api_key"]
    opts = raw.get("options")
    if isinstance(opts, dict) and opts:
        env["MYAGENT_IMAGE_OPTIONS"] = json.dumps(opts)
    return env
