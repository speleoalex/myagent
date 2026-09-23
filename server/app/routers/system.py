import getpass
import os
import platform
import re
import secrets as secrets_mod
import socket
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import config
from app.config import save_settings, WORKSPACE_DIR
from app.engine import default_model, embedding, imagegen
from app.models import Settings

router = APIRouter()

# A generated key is 24 random bytes as hex; a hand-typed one only has to clear
# this floor. Short enough to be typeable on a phone, long enough that the port
# it protects is not worth guessing at.
API_KEY_MIN_LEN = 12

# The instance name is shown in a navbar brand and a browser tab, both of which
# truncate; anything longer than this would be cut where the eye reads it.
INSTANCE_NAME_MAX = 40
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class ApiKeyUpdate(BaseModel):
    """Either an explicit key, or generate: true to get a fresh random one."""
    key: str | None = None
    generate: bool = False


@router.get("/health")
async def health():
    return {"status": "ok", "workspace": str(WORKSPACE_DIR)}


def _host_timezone() -> str:
    """The machine's own IANA name, '' when it cannot be told. /etc/localtime is
    a symlink into the zoneinfo tree on Linux and macOS; %Z gives an ABBREVIATION
    (CEST), which is not a name anyone can type back into the field."""
    try:
        target = Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            return "/".join(parts[parts.index("zoneinfo") + 1:])
    except OSError:
        pass
    return ""


@router.get("/info")
async def info():
    """Who and where this server process is: the account it runs as, the host,
    and the directories it actually uses. Read-only; meant for the Settings
    page so a misconfigured unit (wrong User=, state tree under /root, ...)
    is visible at a glance instead of being guessed from missing agents."""
    try:
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid()) if hasattr(os, "getuid") else "?"
    return {
        # How this install introduces itself (Settings -> Server). Reported
        # here and not only in /settings because the navbar needs it on every
        # page load, and /settings carries fields nobody else should read for
        # that. Empty name = the UI shows the hostname below.
        "instance_name": config.settings.instance_name,
        "instance_color": config.settings.instance_color,
        "user": user,
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "is_root": (os.getuid() == 0) if hasattr(os, "getuid") else False,
        "hostname": socket.gethostname(),
        # The zone this process actually reasons in, already resolved through
        # settings -> machine. Reported so the Settings field can show what an
        # empty value falls back to: the BROWSER's zone is the one thing it
        # must not show there, since the clock that ends up in a prompt is the
        # server's and a phone in another country would suggest otherwise.
        "timezone": config.now_in_timezone().strftime("%Z"),
        "timezone_name": config.timezone_name() or _host_timezone(),
        "platform": platform.platform(terse=True),
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "home_dir": str(config.HOME_DIR),
        "config_dir": str(config.CONFIG_DIR),
        "sessions_dir": str(config.SESSIONS_DIR),
        "workspace_dir": str(config.WORKSPACE_DIR),
        "app_dir": str(config.APP_DIR),
    }


@router.get("/ready")
async def ready(request: Request):
    """Can this install actually answer a message right now?

    Separate from /health, which is a liveness probe and must stay instant.
    This one asks the same resolver the executor uses, so it reports exactly
    what the next turn will do — and warms its cache before the first message.
    """
    stores = request.app.state.stores
    try:
        cfg, note = await default_model.resolve_default(
            stores.models, config.settings.default_model_id)
    except ValueError as e:
        return {"ready": False, "model": None, "auto": False, "detail": str(e)}
    return {"ready": True, "model": cfg.name, "auto": bool(note), "detail": note}


@router.get("/settings")
async def get_settings():
    # Read config.settings live: update_settings rebinds it, so importing the
    # `settings` name directly would return a stale, pre-save snapshot.
    return config.settings.model_dump()


@router.put("/settings")
async def update_settings(new_settings: Settings, request: Request):
    # A remote embedder would send the CONTENT of every indexed document to a
    # third party, so it is refused rather than warned about. This is the
    # friendly half of the rule — the enforcing half is in
    # AgentExecutor.tool_env_overrides, which simply exports nothing for a
    # non-local model, so hand-editing settings.json cannot get around it.
    emb = new_settings.embedding_model_id
    if emb:
        raw = request.app.state.stores.models.get(emb) or {}
        if emb == embedding.LOCAL_ID and not raw:
            # The in-process backend: nothing to validate about a model that
            # does not exist as a config, but refuse if the package is missing
            # rather than accept a setting that can only ever do nothing.
            if not embedding.local_available():
                raise HTTPException(
                    status_code=400,
                    detail=("In-process embeddings need the optional "
                            "`fastembed` package. Install it with "
                            "`server/.venv/bin/pip install fastembed` (or "
                            "re-run install.sh and accept the optional "
                            "dependencies), then choose it again."))
            raw = None
        elif not raw:
            raise HTTPException(status_code=400,
                                detail=f"Unknown model '{emb}'")
        why = embedding.rejection_reason(raw) if raw else ""
        if why:
            raise HTTPException(
                status_code=400,
                detail=(f"'{raw.get('name') or emb}' cannot provide embeddings "
                        f"({why}). Register the embedder under Models with Kind = "
                        "Embedding; it must be local (Ollama or llama.cpp), because "
                        "indexing sends the CONTENTS of your documents to the "
                        "embedding endpoint, not just your question."))
    # The image generator has no such rule and that asymmetry is deliberate:
    # what leaves the machine is a sentence written to be turned into a picture,
    # not the user's documents, so remote is allowed (see app.engine.imagegen).
    # What is still refused is a setting that can only ever do nothing.
    img = new_settings.image_model_id
    if img:
        raw = request.app.state.stores.models.get(img) or {}
        if not raw:
            raise HTTPException(status_code=400, detail=f"Unknown model '{img}'")
        why = imagegen.rejection_reason(raw)
        if why:
            raise HTTPException(
                status_code=400,
                detail=(f"'{raw.get('name') or img}' cannot generate images "
                        f"({why}). Register the generator under Models with "
                        "Kind = Image."))
    # The demotion threshold is a fraction of the usable window. Below 0.5 the
    # payload is compressed on turns that fit comfortably, which throws away
    # context for nothing; above 0.95 there is no room left to land in and the
    # overflow it exists to prevent happens anyway.
    at = new_settings.context_compact_at
    if not (0.5 <= at <= 0.95):
        raise HTTPException(
            status_code=400,
            detail=(f"context_compact_at must be between 0.5 and 0.95 (got {at}): "
                    "below 0.5 it compresses turns that fit, above 0.95 there is "
                    "no room left to land in."))
    # Identity: trimmed, bounded, and the colour either a full #rrggbb or
    # empty. Normalised here rather than in the model so a hand-edited
    # settings.json with an odd value still loads — it just shows as-is until
    # the next save through the UI.
    name = (new_settings.instance_name or "").strip()
    if len(name) > INSTANCE_NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=(f"instance_name is too long ({len(name)} characters, max "
                    f"{INSTANCE_NAME_MAX}): it has to fit a navbar brand and a "
                    "browser tab."))
    color = (new_settings.instance_color or "").strip().lower()
    if color and not _HEX_COLOR.match(color):
        raise HTTPException(
            status_code=400,
            detail=(f"instance_color must be a #rrggbb colour or empty (got "
                    f"{color!r})."))
    # The working zone is checked HERE and not on the model: a name that this
    # machine does not know must not stop settings.json from loading, but it
    # must never get in through the UI either — a silent fallback to the host
    # zone is exactly the wrong date, delivered confidently.
    tz = (new_settings.timezone or "").strip()
    if not config.is_valid_timezone(tz):
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown timezone {tz!r}. Use an IANA name such as "
                    "'Europe/Rome', or leave it empty to follow this machine."))
    new_settings.timezone = tz
    new_settings.instance_name = name
    new_settings.instance_color = color
    save_settings(new_settings)
    config.settings = new_settings
    # The default model and the backend URLs are exactly what the fallback
    # resolver keys on: choosing one here must take effect on the next turn.
    default_model.invalidate()
    return new_settings.model_dump()


# --- Debug trace --------------------------------------------------------------
# The switch lives in Settings (a plain bool on the Settings model, so PUT
# /settings already saves it); these three routes are what makes it USABLE:
# without a way to see the file's size, read it and clear it, "debug is on"
# tells the user nothing and leaves full chat content growing on disk unseen.


@router.get("/debug")
async def debug_status():
    """The switch, and both trace files. TWO of them because they answer
    different questions: debug.log is the turn's narrative, api.log is every
    model call verbatim — including the ones made outside a turn, like the
    classifier that picks which agent answers."""
    files = []
    for key, path in config.debug_files().items():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        files.append({"key": key, "path": str(path), "size": size})
    return {"enabled": config.debug_enabled(),
            "max_bytes": config.DEBUG_MAX_BYTES, "files": files}


def _debug_file(key: str):
    path = config.debug_files().get(key)
    if path is None:
        raise HTTPException(status_code=404, detail=f"Unknown log '{key}'")
    return path


@router.get("/debug/log/{key}")
async def debug_log(key: str, tail: int = 400):
    """The last *tail* lines, newest last. A tail and not the whole file: these
    hold every prompt and every reply verbatim, and a 20 MB response would hang
    the browser that asked for it."""
    try:
        with open(_debug_file(key), "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {"lines": [], "truncated": False, "total": 0}
    tail = max(1, min(int(tail or 400), 5000))
    return {"lines": [ln.rstrip("\n") for ln in lines[-tail:]],
            "truncated": len(lines) > tail, "total": len(lines)}


@router.delete("/debug/log/{key}")
async def debug_clear(key: str):
    """Empty one trace, rotated generation included. The point of the button is
    that these files hold full conversations: having turned tracing on to look
    at something, you need one click to not keep it."""
    path = _debug_file(key)
    for p in (path, path.with_suffix(path.suffix + ".1")):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Could not clear: {e}")
    return {"ok": True}


# --- API key -----------------------------------------------------------------
# The key is returned IN CLEAR, unlike every other stored secret (model API
# keys, MCP bearers, bot tokens — those are masked by routers/secrets.py). It is
# the opposite kind of secret: not a credential for a third party that the UI
# merely carries, but the credential of THIS server, which the caller has just
# presented to get here. Masking it would only stop the user from copying it to
# their phone, which is the whole reason to look at it. When no key is set there
# is nothing to leak and the API is open by definition.

@router.get("/api-key")
async def get_api_key():
    return config.api_key_status()


@router.put("/api-key")
async def set_api_key(req: ApiKeyUpdate):
    _require_editable()
    key = secrets_mod.token_hex(24) if req.generate else (req.key or "").strip()
    if not key:
        raise HTTPException(400, "Provide a key, or generate: true "
                                "(use DELETE to turn authentication off)")
    if len(key) < API_KEY_MIN_LEN:
        raise HTTPException(400, f"Key too short (minimum {API_KEY_MIN_LEN} characters)")
    if any(c.isspace() for c in key) or not key.isprintable():
        # It travels in an HTTP header and in a URL query parameter: whitespace
        # would be silently mangled by one or the other, so the key would work
        # in one client and 401 in the next.
        raise HTTPException(400, "Key must not contain spaces or control characters")
    config.set_api_key(key)
    return config.api_key_status()


@router.delete("/api-key")
async def delete_api_key():
    """Turn authentication off. The caller has the current key, so this is
    theirs to decide — but it leaves the whole API (agents, shell tools) open to
    anyone who can reach the port, hence the confirmation in the UI."""
    _require_editable()
    config.set_api_key("")
    return config.api_key_status()


def _require_editable() -> None:
    if not config.api_key_status()["editable"]:
        raise HTTPException(
            409,
            "The API key is pinned by the MYAGENT_API_KEY environment variable; "
            "unset it (e.g. in the systemd drop-in) to manage the key from here",
        )
