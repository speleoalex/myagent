#!/usr/bin/env python3
"""A model's API key survives every save, whatever its provider.

Run: server/.venv/bin/python tests/test_model_api_key_kept.py

The key is write-only: GET masks it, and a PUT that sends the mask back means
"keep what is stored". That much always worked for the remote providers. What
did not: ollama and llamacpp were treated as "local, therefore keyless", so
  - the form hid the api_key field for them, and a hidden field posts "",
  - the router then force-cleared the field on save for those providers.
Either half alone destroys the key. `llama-server --api-key` is a first-class
flag, and a local server published over HTTPS is precisely the one that has a
key to lose: this cost a live node its key, with nothing on screen to show
what had gone, and the model then answered 401 to every turn.

So: no provider is exempt, and the field is always on the form.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from fastapi.testclient import TestClient                 # noqa: E402
from main import app                                      # noqa: E402

failures = []


def check(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


SECRET = "sk-local-" + "a" * 32
OTHER = "sk-local-" + "b" * 32
MASK = "********"

client = TestClient(app)


def stored_key(model_id):
    """Read the key off disk — the API never hands it back in clear."""
    path = Path(_tmp.name) / "config" / "models" / f"{model_id}.json"
    return json.loads(path.read_text()).get("api_key", "")


# Every provider, not just the remote ones: the local pair is the whole point.
for provider, base_url in (("llamacpp", "http://127.0.0.1:8080"),
                           ("ollama", "http://127.0.0.1:11434"),
                           ("openai", "https://api.openai.com/v1")):
    mid = f"m-{provider}"
    body = {"id": mid, "name": provider, "provider": provider,
            "model": "m", "base_url": base_url, "api_key": SECRET}
    r = client.post("/api/models", json=body)
    check(f"{provider}: created", r.status_code == 201)
    check(f"{provider}: the key is stored, not dropped on create",
          stored_key(mid) == SECRET)
    check(f"{provider}: GET masks it", client.get(f"/api/models/{mid}").json()["api_key"] == MASK)

    # The save that used to destroy it: an unrelated edit, key field untouched.
    r = client.put(f"/api/models/{mid}", json={**body, "name": "renamed", "api_key": MASK})
    check(f"{provider}: PUT ok", r.status_code == 200)
    check(f"{provider}: renaming the model KEEPS the key",
          stored_key(mid) == SECRET)

    # Replacing and clearing both still work — the mask is the only sentinel.
    client.put(f"/api/models/{mid}", json={**body, "api_key": OTHER})
    check(f"{provider}: an explicit key replaces the stored one",
          stored_key(mid) == OTHER)
    client.put(f"/api/models/{mid}", json={**body, "api_key": ""})
    check(f"{provider}: an explicit empty string clears it",
          stored_key(mid) == "")

# The form half of the same bug: no provider-conditional hiding is left.
form = (ROOT / "ui" / "js" / "models.js").read_text()
check("the form has no showsKey() gate any more", "showsKey" not in form)
check("the api_key group is rendered unconditionally",
      'id="api-key-group">' in form)

# And the rule has no list of exempt providers hiding on the server either.
check("KEYED_PROVIDERS is gone",
      "KEYED_PROVIDERS" not in (ROOT / "server" / "app" / "models.py").read_text()
      and "KEYED_PROVIDERS" not in (ROOT / "server" / "app" / "routers" / "llm_models.py").read_text())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — the API key survives an unrelated save for every provider, local "
      "ones included, and can still be replaced or cleared on purpose")
