#!/usr/bin/env python3
"""IndexService — the supervised, on-demand semantic indexer.

Run: server/.venv/bin/python tests/test_index_service.py
(no network, no embeddings: the "indexer" is a stub script this test writes.)

The design under test, and why it is a service rather than a detached process:
local_search cannot index inline (30s timeout, minutes of work) and cannot
reach app.state (it is a subprocess), so it drops a request FILE. Something has
to pick that up. A detached `Popen(start_new_session=True)` would work but owns
nothing — it cannot be stopped, its state is invisible, it outlives the server,
and "only one at a time" needs a cross-process lockfile. Holding the process
handle turns all four into ordinary code, and these are the cases that prove it.

  1. a request starts exactly ONE run, and the request is cleared when it ends;
  2. a second root WAITS: embedding competes with the chat model for the same
     backend, so two indexers would double the interference;
  3. stop() kills a running pass and leaves a STICKY marker — without it the
     next search re-queues what the user just stopped, seconds later;
  4. the marker records the ROOT, so resume works after the cache (explicitly
     safe to delete) has been cleared;
  5. shutdown terminates the child: an indexer must not outlive the server;
  6. no embedding model -> the request is dropped, not retried forever;
  7. a corrupt request file is discarded instead of stalling the queue;
  8. a failing run backs off and is RETRIED — never a terminal state, the
     lesson the autonomy scheduler already paid for.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

_tmp = tempfile.TemporaryDirectory()
os.environ["MYAGENT_HOME"] = _tmp.name

from app import config                                    # noqa: E402
from app.engine import index_service as mod               # noqa: E402
from app.models import Settings                           # noqa: E402

mod.SCAN_INTERVAL = 0.05                                  # keep the test quick
base = Path(_tmp.name)
failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


class FakeStore:
    def __init__(self, models):
        self._m = models

    def get(self, mid):
        return self._m.get(mid)


class FakeRegistry:
    """Stands in for ToolRegistry: all IndexService wants from it is where the
    tool folder is (so a copy-on-write override is honoured) and the resolved
    paths to hand the subprocess."""

    def __init__(self, folder):
        self.folder = folder

    def tool_dir(self, tool_id):
        return self.folder

    def tool_env_for_index(self):
        return {"MYAGENT_HOME": str(base)}


def write_stub(folder: Path, body: str):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "semindex.py").write_text(
        "import os, sys, time\n"
        "root = sys.argv[sys.argv.index('--root') + 1]\n"
        "log = os.environ['STUB_LOG']\n"
        "open(log, 'a').write(root + '\\n')\n" + body)


def request(svc, root, ocr=False):
    svc.dir.mkdir(parents=True, exist_ok=True)
    key = mod._key_of(root, svc.dir)
    (svc.dir / f"{key}.request").write_text(
        json.dumps({"root": root, "ocr": ocr, "requested_at": time.time()}))
    return key


async def until(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def main():
    tool = base / "tool"
    log = base / "runs.log"
    os.environ["STUB_LOG"] = str(log)

    roots = []
    for name in ("alpha", "beta"):
        d = base / name
        d.mkdir(exist_ok=True)
        roots.append(str(d.resolve()))

    models = FakeStore({"local": {"id": "local", "provider": "ollama",
                                  "model": "e", "base_url": "http://x"}})
    config.settings = Settings(embedding_model_id="local")

    # 1 + 2. one at a time, in the background, and cleared when done.
    write_stub(tool, "time.sleep(1.2)\n")
    svc = mod.IndexService(base / "c1", FakeRegistry(tool), models)
    svc.start()
    k0 = request(svc, roots[0])
    await until(lambda: svc._current is not None)        # alpha first: FIFO
    k1 = request(svc, roots[1])

    check("a request starts a run", svc._current is not None)
    check("requests are served oldest-first, not in hash order",
          svc._current == roots[0])
    await asyncio.sleep(0.4)
    check("the second root waits its turn — one indexer at a time",
          log.read_text().count("\n") == 1)
    check("status() reports the run", any(r["state"] == "running"
                                          for r in svc.status()))

    check("the first request is cleared when the run succeeds",
          await until(lambda: not (svc.dir / f"{k0}.request").exists()))
    check("the queued root runs next",
          await until(lambda: roots[1] in log.read_text()))
    check("both roots ran exactly once",
          await until(lambda: sorted(log.read_text().split()) == sorted(roots)))
    await svc.aclose()

    # 3 + 4. stop is sticky and names the root.
    log.write_text("")
    write_stub(tool, "time.sleep(30)\n")
    svc = mod.IndexService(base / "c2", FakeRegistry(tool), models)
    svc.start()
    k = request(svc, roots[0])
    check("a long run starts", await until(lambda: svc._current is not None))
    proc = svc._proc
    await svc.stop(k)
    check("stop() kills the running indexer",
          await until(lambda: proc.returncode is not None))
    check("stop() leaves a sticky marker", (svc.dir / f"{k}.paused").exists())
    check("stop() removes the queued request", not (svc.dir / f"{k}.request").exists())
    check("the marker records the ROOT, not just the fact",
          json.loads((svc.dir / f"{k}.paused").read_text())["root"] == roots[0])
    check("a paused root reports as paused",
          any(r["state"] == "paused" and r["root"] == roots[0]
              for r in svc.status()))

    # the cache is documented as safe to delete: resume must survive that.
    for f in svc.dir.glob("*.db*"):
        f.unlink()
    await svc.resume(k)
    check("resume works with the database gone",
          (svc.dir / f"{k}.request").exists()
          and json.loads((svc.dir / f"{k}.request").read_text())["root"] == roots[0])
    check("resume clears the pause", not (svc.dir / f"{k}.paused").exists())

    # 5. shutdown must not leave a child behind.
    check("a run restarted after resume", await until(lambda: svc._proc is not None))
    child = svc._proc
    await svc.aclose()
    check("shutdown terminates the indexer", child.returncode is not None)

    # 6. nothing configured -> drop the request rather than spin on it.
    config.settings = Settings(embedding_model_id=None)
    log.write_text("")
    svc = mod.IndexService(base / "c3", FakeRegistry(tool), models)
    svc.start()
    k = request(svc, roots[0])
    check("with no embedding model the request is dropped",
          await until(lambda: not (svc.dir / f"{k}.request").exists()))
    check("...and nothing was run", log.read_text().strip() == "")
    await svc.aclose()

    # 7. a corrupt request must not stall the queue.
    config.settings = Settings(embedding_model_id="local")
    write_stub(tool, "")
    log.write_text("")
    svc = mod.IndexService(base / "c4", FakeRegistry(tool), models)
    svc.dir.mkdir(parents=True, exist_ok=True)
    (svc.dir / "zzzz.request").write_text("{not json")
    svc.start()
    k = request(svc, roots[1])
    check("a corrupt request is discarded",
          await until(lambda: not (svc.dir / "zzzz.request").exists()))
    check("...and the good one still runs",
          await until(lambda: roots[1] in log.read_text()))
    await svc.aclose()

    # 8. failure backs off and is retried, never terminal.
    write_stub(tool, "sys.exit(3)\n")
    log.write_text("")
    svc = mod.IndexService(base / "c5", FakeRegistry(tool), models)
    svc.start()
    k = request(svc, roots[0])
    check("a failing run is noticed",
          await until(lambda: svc._failures.get(roots[0], 0) >= 1))
    check("the request STAYS queued — failure is never terminal",
          (svc.dir / f"{k}.request").exists())
    check("the next attempt is delayed, not immediate",
          svc._retry_after.get(roots[0], 0) > time.time())
    check("the error is reported, not swallowed",
          any(r.get("error") for r in svc.status()))
    await svc.aclose()


asyncio.run(main())

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — indexing runs one at a time under supervision, stops stickily, "
      "survives a cleared cache, and never reaches a terminal failure")
