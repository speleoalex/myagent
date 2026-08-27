"""IndexService — builds the semantic indexes the search tools ask for.

ON-DEMAND, never on its own initiative. It scans one directory
(``<cache>/index/``) for request files and does nothing when there are none, so
an install that never uses semantic search pays nothing for this and never
starts an embedding run the user did not cause.

**Why a supervised service and not a detached process.** The obvious
alternative was for ``local_search`` to spawn the indexer itself with
``start_new_session=True`` and walk away. That works, and it was the first
design — but nobody owns the result: it cannot be stopped, its state is
invisible, it outlives the server, and preventing two of them needs a
cross-process lockfile. Here the service holds the ``Process`` handle, so
stopping is ``terminate()``, status is a dict, shutdown kills what is running,
and "one at a time" is a variable.

**Why still a subprocess and not inline.** The indexing code belongs to the
tool (``server/tools/library/local_search/semindex.py``), which is a
copy-on-write overlay the user may have overridden. Importing it into the core
would freeze one copy and break the overlay. So the service asks the registry
where the tool actually lives and runs its CLI — the core imports nothing from
the tools layer.

**Why the queue is a file.** A tool runs as a subprocess with no route back
into ``app.state``. It drops ``<hash>.request`` next to where the database will
go, and that is the whole protocol (``semindex.request_index``).

**One indexer at a time, globally.** Embedding competes with the chat model for
the same backend; on a single-slot server every batch is a request the user's
turn queues behind. Two concurrent indexers would double that, so requests wait
their turn and each run is throttled and niced.

**Stopping is sticky.** ``stop()`` writes ``<hash>.paused``, which the tool
checks before queueing: without it the next search would restart what the user
just stopped.

**Failure is never terminal.** A run that fails is retried with a growing
delay, and only the user (via ``stop``) ends it — the same lesson the autonomy
scheduler already paid for, where a terminal error state kept an agent down for
20 hours after its cause had been fixed elsewhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from app.engine.embedding import resolve_embed_env

log = logging.getLogger(__name__)

SCAN_INTERVAL = 5.0            # matches the autonomy scheduler's resolution
THROTTLE_MS = 200              # pause between embedding batches
RETRY_BACKOFF_BASE = 60        # seconds after the first failure
RETRY_BACKOFF_MAX = 1800
# A run this long has stopped being a background chore. It is cut off and
# retried; the index is consistent at every file boundary, so the next run
# resumes rather than restarts.
MAX_RUN_SECONDS = 3600


class IndexService:
    def __init__(self, cache_dir: Path, registry, models_store,
                 tool_id: str = "local_search"):
        self.dir = Path(cache_dir) / "index"
        self.registry = registry
        self.models_store = models_store
        self.tool_id = tool_id
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._current: str | None = None          # root being indexed
        self._started_at: float = 0.0
        self._failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc, self._current = self._proc, None, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
        except Exception:
            pass

    # -- the script -------------------------------------------------------
    def _script(self) -> Path | None:
        """Where semindex.py actually is — asked of the registry, which already
        resolves a copy-on-write override to the user's copy."""
        try:
            folder = self.registry.tool_dir(self.tool_id)
        except Exception:
            folder = None
        if folder is None:
            return None
        script = Path(folder) / "semindex.py"
        return script if script.exists() else None

    # -- the loop ---------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("index scan failed")
            await asyncio.sleep(SCAN_INTERVAL)

    def _requests(self) -> list[tuple[Path, dict]]:
        if not self.dir.is_dir():
            return []
        out = []
        # Oldest request first. The filename is a hash of the root, so sorting
        # by name would serve them in an order nobody chose and could starve
        # the folder someone just searched behind one they searched yesterday.
        def age(f):
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0
        for f in sorted(self.dir.glob("*.request"), key=age):
            if f.with_suffix(".paused").exists():
                continue
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # An unreadable request is not a reason to stall the queue.
                f.unlink(missing_ok=True)
                continue
            if body.get("root"):
                out.append((f, body))
        return out

    async def _tick(self) -> None:
        if self._proc is not None:
            if self._proc.returncode is None:
                if time.time() - self._started_at > MAX_RUN_SECONDS:
                    log.warning("index run for %s exceeded %ss; stopping "
                                "(it resumes on the next pass)",
                                self._current, MAX_RUN_SECONDS)
                    await self._kill()
                return
            await self._finish()
            return

        now = time.time()
        for req, body in self._requests():
            root = body["root"]
            if self._retry_after.get(root, 0) > now:
                continue
            await self._spawn(req, root, bool(body.get("ocr")))
            return

    async def _spawn(self, req: Path, root: str, ocr: bool) -> None:
        script = self._script()
        if script is None:
            log.warning("semindex.py not found; dropping index request for %s", root)
            req.unlink(missing_ok=True)
            return
        if not os.path.isdir(root):
            log.info("index request for a folder that is gone: %s", root)
            req.unlink(missing_ok=True)
            return

        cmd = [sys.executable, str(script), "--root", root, "--index",
               "--throttle-ms", str(THROTTLE_MS)]
        if ocr:
            cmd.append("--ocr")
        # The SAME embedder the search tools query with (app.engine.embedding
        # owns that rule, local-only included): building an index with one
        # model and querying it with another produces noise, silently.
        embed_env = resolve_embed_env(self.models_store)
        env = {**os.environ, **self.registry.tool_env_for_index(), **embed_env}
        if not embed_env:
            log.info("no embedding model configured; dropping index request "
                     "for %s", root)
            req.unlink(missing_ok=True)
            return
        # Which backend was chosen is resolve_embed_env's business, but ONE
        # thing has to be undone here: this process may itself have been
        # started with MYAGENT_EMBED_URL in its environment (a drop-in, a
        # container), and `{**os.environ, **embed_env}` would leave it in place
        # next to MYAGENT_EMBED_LOCAL — where embedder_from_env prefers the
        # endpoint and would silently index with the wrong model.
        if "MYAGENT_EMBED_LOCAL" in embed_env:
            env.pop("MYAGENT_EMBED_URL", None)
            env.pop("MYAGENT_EMBED_MODEL", None)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
                preexec_fn=_nice if hasattr(os, "nice") else None)
        except Exception as e:
            log.warning("could not start the indexer for %s: %s", root, e)
            self._fail(root, str(e))
            return
        self._current, self._started_at, self._request = root, time.time(), req
        log.info("indexing %s", root)

    async def _finish(self) -> None:
        proc, root, req = self._proc, self._current, getattr(self, "_request", None)
        self._proc, self._current = None, None
        try:
            out, err = await proc.communicate()
        except Exception:
            out, err = b"", b""
        if proc.returncode == 0:
            self._failures.pop(root, None)
            self._retry_after.pop(root, None)
            self._last_error.pop(root, None)
            if req is not None:
                req.unlink(missing_ok=True)
            log.info("indexed %s: %s", root,
                     (out or b"").decode(errors="replace").strip()[:200])
        else:
            msg = (err or b"").decode(errors="replace").strip()[:300]
            self._fail(root, msg or f"exit {proc.returncode}")

    def _fail(self, root: str, msg: str) -> None:
        """Back off, never give up. Only stop() ends a root — that decision is
        the user's, and most failures (a backend not up yet, a model still
        loading) fix themselves."""
        n = self._failures.get(root, 0) + 1
        self._failures[root] = n
        self._last_error[root] = msg
        delay = min(RETRY_BACKOFF_BASE * (2 ** (n - 1)), RETRY_BACKOFF_MAX)
        self._retry_after[root] = time.time() + delay
        log.warning("indexing %s failed (%d in a row), retrying in %ds: %s",
                    root, n, delay, msg)

    # -- API surface ------------------------------------------------------
    def status(self) -> list[dict]:
        """One row per root the service knows about: queued, running or paused.

        Progress comes from the index database itself (semindex writes it into
        `meta` as it goes), so a run's progress survives a restart of either
        side.
        """
        rows: dict[str, dict] = {}

        def row(root: str) -> dict:
            return rows.setdefault(root, {
                "root": root, "key": _key_of(root, self.dir),
                "state": "idle", "indexed": 0, "total": 0, "chunks": 0,
                "error": self._last_error.get(root, ""),
            })

        if not self.dir.is_dir():
            return []
        for f in self.dir.glob("*.request"):
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if body.get("root"):
                row(body["root"])["state"] = "queued"
        for f in self.dir.glob("*.paused"):
            root = self._root_for(f.name[:-len(".paused")])
            if root:
                row(root)["state"] = "paused"
        for f in self.dir.glob("*.db"):
            root = _root_of_db(f)
            if not root:
                continue
            r = row(root)
            r.update(_progress_of(f))
        if self._current:
            r = row(self._current)
            r["state"] = "running"
            r["since"] = self._started_at
        for root, when in self._retry_after.items():
            if when > time.time() and root in rows:
                rows[root]["state"] = "retrying"
                rows[root]["retry_at"] = when
        return sorted(rows.values(), key=lambda r: r["root"])

    def _root_for(self, key: str) -> str:
        """The folder behind a key, from whichever artefact still exists.

        Tried in order of reliability, and the marker files are checked BEFORE
        the database because the cache is deletable by design: clearing it must
        not turn a paused root into one that can never be resumed.
        """
        if self._current and _key_of(self._current, self.dir) == key:
            return self._current
        for name in (f"{key}.request", f"{key}.paused"):
            try:
                body = json.loads((self.dir / name).read_text(encoding="utf-8"))
                if body.get("root"):
                    return body["root"]
            except (OSError, ValueError):
                pass
        return _root_of_db(self.dir / f"{key}.db")

    async def stop(self, key: str) -> bool:
        """Pause a root. Sticky: the marker is what stops the next search from
        queueing it again five seconds later.

        The marker RECORDS THE ROOT, so resuming never depends on the database
        still being there — the cache is explicitly safe to delete, and a
        cleared cache must not leave a folder paused forever with nothing able
        to name it again.
        """
        root = self._root_for(key)
        req = self.dir / f"{key}.request"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{key}.paused").write_text(
                json.dumps({"root": root, "paused_at": time.time()}))
            req.unlink(missing_ok=True)
        except OSError:
            return False
        if self._current and _key_of(self._current, self.dir) == key:
            await self._kill()
        return True

    async def resume(self, key: str) -> bool:
        root = self._root_for(key)
        try:
            (self.dir / f"{key}.paused").unlink(missing_ok=True)
        except OSError:
            return False
        if not root:
            # Nothing left to name the folder. Not an error: the next search
            # over it queues a fresh request by itself.
            return True
        self._retry_after.pop(root, None)
        self._failures.pop(root, None)
        try:
            (self.dir / f"{key}.request").write_text(
                json.dumps({"root": root, "ocr": False,
                            "requested_at": time.time()}))
        except OSError:
            return False
        return True


def _nice() -> None:                                   # pragma: no cover
    try:
        os.nice(10)
    except Exception:
        pass


def _key_of(root: str, _dir: Path) -> str:
    import hashlib
    return hashlib.sha1(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()[:16]


def _root_of_db(db: Path) -> str:
    """The root a database belongs to, read from its own meta table. Reading
    sqlite here rather than caching a map keeps ONE source of truth, and the
    file is the thing that survives restarts."""
    if not db.exists():
        return ""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            r = con.execute("SELECT value FROM meta WHERE key='root'").fetchone()
            return r[0] if r else ""
        finally:
            con.close()
    except Exception:
        return ""


def _progress_of(db: Path) -> dict:
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            def meta(k, d=""):
                r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
                return r[0] if r else d
            indexed = con.execute(
                "SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0]
            chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            total = int(meta("total", indexed) or indexed)
            return {"indexed": indexed, "total": max(total, indexed),
                    "chunks": chunks, "model": meta("embed_model")}
        finally:
            con.close()
    except Exception:
        return {}
