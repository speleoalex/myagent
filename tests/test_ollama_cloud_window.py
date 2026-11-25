#!/usr/bin/env python3
"""An Ollama CLOUD model is budgeted at its real window, not at 4096.

Orin, 2026-11-25: the model `deepseek-v4.1-flash:cloud` carried an explicit
context_window of 9000960 (a typo; the probe capped it to the declared
1048576). Clearing it to "let the probe decide" dropped the budget to 4096:
a cloud model never appears in /api/ps, so no "served" window is known, and
the Ollama branch fell to its local default — chosen to spare local VRAM,
which a model running on ollama.com does not use.

Pinned here (model_probe.resolve is pure, no network):

  * cloud tags (":cloud", ":<size>-cloud") without an explicit value use the
    model's declared maximum;
  * a local Ollama model keeps the small default (the VRAM reason stands);
  * an explicit value still wins, and with no declared maximum the default
    is still the fallback.

Run:  server/.venv/bin/python tests/test_ollama_cloud_window.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["MYAGENT_HOME"] = tempfile.mkdtemp(prefix="myagent-ollamacloud-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from app.engine.model_probe import OLLAMA_DEFAULT_CTX, is_ollama_cloud, resolve  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def window(model, info, explicit=None):
    cfg = {"provider": "ollama", "model": model}
    if explicit:
        cfg["context_window"] = explicit
    r = resolve(cfg, info)
    return r["context_window"], r["source"]


print("naming")
check(is_ollama_cloud("deepseek-v4.1-flash:cloud"), "':cloud' is a cloud model")
check(is_ollama_cloud("gpt-oss:120b-cloud"), "':<size>-cloud' is a cloud model")
check(not is_ollama_cloud("qwen3:4b") and not is_ollama_cloud("my-cloud-thing:latest")
      and not is_ollama_cloud("cloud") and not is_ollama_cloud(None),
      "'cloud' elsewhere in the name does not count")

print("window")
check(window("deepseek-v4.1-flash:cloud", {"n_ctx_max": 1048576}) == (1048576, "model_max"),
      "a cloud model without an explicit value gets its declared window")
check(window("qwen3:4b", {"n_ctx_max": 262144}) == (OLLAMA_DEFAULT_CTX, "default"),
      "a local model keeps the small default (local VRAM)")
check(window("deepseek-v4.1-flash:cloud", {"n_ctx_max": 1048576}, 32768) == (32768, "explicit"),
      "an explicit value still wins")
check(window("deepseek-v4.1-flash:cloud", {"n_ctx_max": 1048576}, 9000960) == (1048576, "model_max"),
      "an explicit value above the model's maximum is capped (the Orin typo)")
check(window("deepseek-v4.1-flash:cloud", {"n_ctx_max": None}) == (OLLAMA_DEFAULT_CTX, "default"),
      "with no declared maximum the default is still the fallback")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
