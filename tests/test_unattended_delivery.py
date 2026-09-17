#!/usr/bin/env python3
"""A file produced during an autonomous wake reached NOBODY — say so.

Orin, 2026-09-17. A scheduled task ("send Alessandro a picture of a heart")
woke master, which drew the heart, cut its background out, and then called
notify_user with text, subject and recipient — and no ``attachments``. The
message arrived; the picture did not. Nothing failed: every tool told the
model the file was ALREADY displayed to the user ("do not paste the path"),
which is true of a web chat and of a messaging channel (the connector sends a
turn's resources as photos) and false of a wake, where the reply is only
logged. The run of the previous evening got it right, so the wording is not a
hard failure — it is a coin flip, and this pins which way the coin is loaded.

What is pinned here:

  * ``resources.extract`` returns the SAME resources either way (they ride the
    trace, which the session file and the UI read) and only the model-facing
    note changes: delivered vs saved-and-seen-by-nobody, naming the path to
    pass to notify_user;
  * an executor is attended by default, and ``tool_env_overrides`` exports
    MYAGENT_UNATTENDED only when it is not — that env var is how the three
    image tools, which phrase their own delivery line in prose, learn it;
  * those three lines really do change with it;
  * a wake sets the flag (AutonomyService._wake), which is the only place that
    knows.

Run:  server/.venv/bin/python tests/test_unattended_delivery.py
"""

import importlib.machinery
import importlib.util
import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-unattended-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine.autonomy import AutonomyService  # noqa: E402
from app.engine.executor import AgentExecutor, Stores  # noqa: E402
from app.models import Agent, ModelConfig  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402
from app.tools import resources  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

BUNDLED = _ROOT / "server" / "tools"
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# ------------------------------------------------- the note the model reads
WS = Path(_TMP) / "workspace"
WS.mkdir(parents=True, exist_ok=True)
(WS / "heart-image.png").write_bytes(b"\x89PNG fake")

RAW = ("Image generated in 4s and saved as heart-image.png (672 KB).\n"
       + resources.marker("heart-image.png", "image/png", "A glowing heart"))

print("resource note")
chat_text, chat_res = resources.extract(RAW, WS)
wake_text, wake_res = resources.extract(RAW, WS, unattended=True)

check(chat_res == wake_res, "the resources themselves do not depend on the turn")
check(len(wake_res) == 1 and wake_res[0]["path"] == "heart-image.png",
      "the file is collected for the trace either way")
check("already displayed in the chat" in chat_text,
      "attended: the file is said to be displayed")
check("already displayed" not in wake_text and "delivered to the user" not in wake_text,
      "unattended: nothing claims the user got it")
check("notify_user" in wake_text and "heart-image.png" in wake_text,
      "unattended: the note names the file and the way to send it")
check("notify_user" not in chat_text, "attended: the note stays as it was")
check("[[resource:" not in wake_text, "the marker never survives into the prompt")

# ------------------------------------------------------ the executor's flag
print("\nexecutor")
agent = Agent(id="master", name="master", tools=["notify_user"],
              memory_enabled=False)
model = ModelConfig(id="m", name="m", provider="llamacpp",
                    base_url="http://127.0.0.1:9")  # never contacted
registry = ToolRegistry(Path(_TMP) / "tools", bundled_dir=BUNDLED)
stores = Stores(agents=JsonStore(Path(_TMP) / "agents"),
                models=JsonStore(Path(_TMP) / "models"))
ex = AgentExecutor(agent, model, registry, stores)

check(ex.unattended is False, "a turn is attended unless somebody says otherwise")
check("MYAGENT_UNATTENDED" not in ex.tool_env_overrides(),
      "attended: the tools see the environment they see today")
ex.unattended = True
check(ex.tool_env_overrides().get("MYAGENT_UNATTENDED") == "1",
      "unattended: the tools are told")

# ------------------------------------------- the tools that phrase it in prose
print("\nimage tools")


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


TOOLS = {
    "generate_image": BUNDLED / "images/generate_image/run",
    "edit_image": BUNDLED / "images/edit_image/run",
    "remove_background": BUNDLED / "images/remove_background/remove_background.py",
}
for name, path in TOOLS.items():
    mod = _load(path, f"_tool_{name}")
    os.environ.pop("MYAGENT_UNATTENDED", None)
    attended = mod._delivery_line()
    os.environ["MYAGENT_UNATTENDED"] = "1"
    unattended = mod._delivery_line()
    os.environ.pop("MYAGENT_UNATTENDED", None)
    check("already displayed to the user" in attended,
          f"{name}: attended wording unchanged")
    check("Nobody has seen" in unattended and "notify_user" in unattended,
          f"{name}: unattended wording sends it through notify_user")

# ------------------------------------------------------------ who sets it
print("\nwiring")
src = inspect.getsource(AutonomyService._wake)
check("executor.unattended = True" in src, "a wake marks its executor unattended")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all ok")
