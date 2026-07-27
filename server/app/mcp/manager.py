"""Connection pool, discovery cache and policy for MCP servers.

Design rules that the rest of the app depends on:

* **Nothing here ever raises into a chat turn.** `ensure_for()` swallows every
  per-server failure and `call()` returns the same in-band ``"ERROR: ..."``
  strings a script tool would.
* **Lazy**: a server process is started only when an agent that references its
  tools actually runs a turn.
* **A failed refresh keeps the previous tool list.** Definitions the model has
  already been told about must not vanish because one discovery timed out.
* **Connecting is bounded but not abandoned**: the wait is capped by
  ``connect_timeout`` while the connect itself continues in the background
  (`asyncio.shield`), so a cold `npx -y` download costs one degraded turn
  instead of blocking it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.models import McpServer
from app.storage.store import JsonStore
from app.mcp import client as mcp_client
from app.mcp import naming, result

log = logging.getLogger(__name__)

# After a failed connect, a server is left alone for this long instead of being
# retried on every turn (each retry costs up to its connect_timeout). A manual
# refresh or a config edit clears it immediately.
FAILURE_COOLDOWN = 60.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class McpManager:
    def __init__(self, store: JsonStore, cache_store: JsonStore, workspace: Path):
        self._store = store
        self._cache = cache_store
        self._workspace = workspace

        self._servers: dict[str, McpServer] = {}
        self._dir_mtime: float = -1.0

        self._conns: dict[str, mcp_client.BaseConnection] = {}
        # Config snapshot a live connection was opened with: an edited server
        # must be reconnected, whether the edit came through the API or the file.
        self._conn_cfg: dict[str, dict] = {}
        # In-flight connect+discover task per server. This is the mutual
        # exclusion mechanism: concurrent turns (and channel sessions) awaiting
        # the same server share ONE task, so a connect that outlives its waiter's
        # timeout can never be started twice — i.e. never two subprocesses.
        self._connecting: dict[str, asyncio.Task] = {}
        # Fire-and-forget catalogue warm-ups (strong refs: asyncio only keeps
        # weak ones, so a task without a reference can be garbage collected).
        self._tasks: set[asyncio.Task] = set()
        # refresh() tears a connection down and builds a new one, so it must not
        # run twice concurrently for the same server (a background warm-up and a
        # user-triggered refresh can overlap): the loser would leak a subprocess.
        # asyncio.Lock() binds to a loop lazily, so creating these at import time
        # (before uvicorn starts one) is safe.
        self._refresh_locks: dict[str, asyncio.Lock] = {}

        self._defs: dict[str, list[dict]] = {}   # sid -> usable tool metas
        self._skipped: dict[str, list[dict]] = {}
        self._listed_at: dict[str, float] = {}   # sid -> monotonic
        self._status: dict[str, dict] = {}
        self._failed_at: dict[str, float] = {}   # sid -> monotonic of last failure
        self._by_id: dict[str, dict] = {}        # qualified id -> live meta
        self._cached: dict[str, dict] = {}       # sid -> disk cache payload
        self._owner: dict[str, str] = {}         # qualified id -> sid (live + cached)

        self.reload()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def reload(self) -> None:
        servers: dict[str, McpServer] = {}
        for raw in self._store.list_all():
            try:
                cfg = McpServer(**raw)
            except (ValidationError, TypeError) as e:
                log.warning("invalid MCP server config skipped: %s", e)
                continue
            servers[cfg.id] = cfg
        self._servers = servers
        self._dir_mtime = self._mtime()
        self._cached = {}
        for sid in servers:
            payload = self._cache.get(sid)
            if isinstance(payload, dict):
                self._cached[sid] = payload
        self._reindex()

    def _mtime(self) -> float:
        try:
            return self._store.directory.stat().st_mtime
        except OSError:
            return -1.0

    def _maybe_reload(self) -> None:
        """Pick up config changes (API writes or hand-edited files) cheaply."""
        if self._mtime() != self._dir_mtime:
            self.reload()

    def _reindex(self) -> None:
        by_id: dict[str, dict] = {}
        owner: dict[str, str] = {}
        for sid in sorted(self._defs):
            for meta in self._defs[sid]:
                # Two servers can produce the same qualified id when one id is a
                # prefix of the other (server "a" + tool "b_c" vs server "a_b" +
                # tool "c"). Whoever got there first keeps it: silently routing a
                # call to the wrong server would be far worse than dropping one.
                if meta["id"] in by_id:
                    other = owner.get(meta["id"])
                    if other != sid:
                        log.warning(
                            "mcp[%s] tool '%s' collides with server '%s' on the "
                            "name '%s' and is not offered — rename one server id",
                            sid, meta["mcp"]["tool"], other, meta["id"],
                        )
                    continue
                by_id[meta["id"]] = meta
                owner[meta["id"]] = sid
        # Cached (possibly unavailable) tools still resolve to their server, so a
        # tool id from a server that is currently down routes correctly.
        for sid, payload in self._cached.items():
            for meta in payload.get("mapped") or []:
                if isinstance(meta, dict) and meta.get("id"):
                    owner.setdefault(meta["id"], sid)
        self._by_id = by_id
        self._owner = owner

    def servers(self) -> list[McpServer]:
        self._maybe_reload()
        return [self._servers[sid] for sid in sorted(self._servers)]

    def get(self, server_id: str) -> McpServer | None:
        self._maybe_reload()
        return self._servers.get(server_id)

    def server_ids(self) -> set[str]:
        self._maybe_reload()
        return set(self._servers)

    # ------------------------------------------------------------------
    # tool catalogue
    # ------------------------------------------------------------------

    def servers_for_tool_ids(self, tool_ids: list[str]) -> set[str]:
        """Which configured servers the given Agent.tools entries refer to."""
        self._maybe_reload()
        known = set(self._servers)
        out: set[str] = set()
        for entry in tool_ids or []:
            if not isinstance(entry, str):
                continue
            sid = naming.parse_wildcard(entry)
            if sid:
                if sid in known:
                    out.add(sid)
                continue
            owner = self._owner.get(entry)
            if owner in known:
                out.add(owner)
                continue
            rest = naming.server_of(entry)
            if not rest:
                continue
            # Unknown tool id (never discovered yet): server ids may contain
            # underscores, so accept every configured id that prefixes it.
            for candidate in known:
                if rest == candidate or rest.startswith(candidate + "_"):
                    out.add(candidate)
        return out

    def defs_for_tool_ids(self, tool_ids: list[str]) -> list[dict]:
        """Usable-right-now definitions for the given entries (wildcards expanded)."""
        self._maybe_reload()
        out: list[dict] = []
        seen: set[str] = set()

        def add(meta: dict) -> None:
            if meta["id"] not in seen:
                seen.add(meta["id"])
                out.append(meta)

        for entry in tool_ids or []:
            if not isinstance(entry, str):
                continue
            sid = naming.parse_wildcard(entry)
            if sid:
                for meta in self._defs.get(sid, []):
                    # Only the definition that actually owns the qualified id (see
                    # _reindex): a colliding one would dispatch to another server.
                    if self._by_id.get(meta["id"]) is meta:
                        add(meta)
                continue
            meta = self._by_id.get(entry)
            if meta is not None:
                add(meta)
        return out

    def get_meta(self, tool_id: str) -> dict | None:
        return self._by_id.get(tool_id)

    def catalogue(self, include_unavailable: bool = True) -> list[dict]:
        """Every tool ever discovered, for pickers. No connection is opened.

        Live definitions are marked ``available: true``; ones only known from the
        on-disk cache are included as ``available: false`` so a selection is
        never silently lost while a server is down.
        """
        self._maybe_reload()
        out: list[dict] = []
        for sid in sorted(self._servers):
            live = self._defs.get(sid)
            if live:
                out.extend({**meta, "available": True} for meta in live)
            elif include_unavailable:
                for meta in (self._cached.get(sid) or {}).get("mapped") or []:
                    if isinstance(meta, dict) and meta.get("id"):
                        out.append({**meta, "available": False})
        return out

    def status(self, server_id: str) -> dict:
        cfg = self._servers.get(server_id)
        state = self._status.get(server_id) or {}
        conn = self._conns.get(server_id)
        if cfg is not None and not cfg.enabled:
            current = "disabled"
        elif conn is not None and conn.alive:
            current = "ready"
        elif state.get("last_error"):
            current = "error"
        else:
            current = "idle"
        cached = self._cached.get(server_id) or {}
        live = self._defs.get(server_id)
        return {
            "state": current,
            "tool_count": len(live) if live is not None else len(cached.get("mapped") or []),
            "tools_cached": live is None and bool(cached.get("mapped")),
            "last_error": state.get("last_error") or "",
            "server_info": state.get("server_info") or cached.get("server_info") or {},
            "protocol_version": state.get("protocol_version") or cached.get("protocol_version") or "",
            "connected_at": state.get("connected_at") or "",
            "listed_at": state.get("listed_at") or cached.get("fetched_at") or "",
            "skipped": self._skipped.get(server_id) or cached.get("skipped") or [],
            "stderr_tail": conn.stderr_tail[-10:] if conn is not None else [],
        }

    # ------------------------------------------------------------------
    # connect / discover
    # ------------------------------------------------------------------

    async def ensure_for(self, server_ids: set[str]) -> None:
        """Make sure each server is connected and its tool list is fresh.

        Never raises: a failing server degrades to "no tools from that server".
        """
        self._maybe_reload()
        wanted = [
            sid for sid in sorted(server_ids or ())
            if (cfg := self._servers.get(sid)) is not None and cfg.enabled
        ]
        if not wanted:
            return
        # Concurrently: one unreachable server must not add its connect budget to
        # the next one's before the turn can start.
        await asyncio.gather(
            *(self._ensure_one(sid, self._servers[sid]) for sid in wanted),
            return_exceptions=True,
        )

    async def _ensure_one(self, sid: str, cfg: McpServer) -> None:
        if self._fresh(sid, cfg):
            return
        # A server that just failed is not retried on every single turn: without
        # this, two dead servers add 2x connect_timeout to every message the user
        # sends. The Refresh button and any config edit clear the cooldown.
        failed_at = self._failed_at.get(sid)
        if failed_at is not None and (time.monotonic() - failed_at) < FAILURE_COOLDOWN:
            return
        # No awaits between the check and the task creation, so this is atomic
        # on the event loop: at most one connect task exists per server.
        task = self._connecting.get(sid)
        if task is None:
            task = asyncio.create_task(self._connect_and_list(sid, cfg))
            self._connecting[sid] = task
            task.add_done_callback(lambda t, s=sid: self._on_connect_done(s, t))
        try:
            # shield: give up WAITING after connect_timeout, but let the connect
            # finish in the background so the next turn finds it ready (a cold
            # `npx -y` can take 30s+ the first time).
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=max(5.0, float(cfg.connect_timeout or 20)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            message = "connect timed out" if isinstance(e, asyncio.TimeoutError) else str(e)
            self._mark_error(sid, message)
            log.warning("mcp[%s] unavailable: %s", sid, message)
            # NOTE: self._defs[sid] is deliberately left untouched — a failed
            # refresh must not retract tools already in use.

    def _on_connect_done(self, sid: str, task: asyncio.Task) -> None:
        self._connecting.pop(sid, None)
        if task.cancelled():
            return
        # Retrieve the exception even when every waiter timed out and walked
        # away, otherwise asyncio logs "exception was never retrieved".
        error = task.exception()
        if error is not None:
            self._mark_error(sid, str(error))

    def _fresh(self, sid: str, cfg: McpServer) -> bool:
        if sid not in self._defs:
            return False
        conn = self._conns.get(sid)
        if conn is None or not conn.alive:
            return False
        if self._conn_cfg.get(sid) != cfg.model_dump():
            return False  # the server was edited: reconnect with the new settings
        listed = self._listed_at.get(sid)
        if listed is None:
            return False
        return (time.monotonic() - listed) < max(5, int(cfg.tools_ttl or 300))

    async def _connect_and_list(self, sid: str, cfg: McpServer) -> None:
        conn = self._conns.get(sid)
        if conn is not None and (
            not conn.alive or self._conn_cfg.get(sid) != cfg.model_dump()
        ):
            # Closed directly, NOT through aclose_server(): this coroutine *is*
            # the in-flight connect task, and aclose_server waits for that task
            # to settle — i.e. it would wait on itself.
            self._conns.pop(sid, None)
            self._conn_cfg.pop(sid, None)
            await self._close(conn, sid)
            conn = None
        if conn is None:
            conn = mcp_client.create_connection(cfg)
            # A tools/list_changed notification just marks the list stale; the
            # next turn re-lists over the same connection.
            conn.on_tools_changed = lambda s=sid: self._listed_at.pop(s, None)
            await conn.connect()
            # Defensive: if anything else published a connection while we were
            # connecting, close it instead of leaking its subprocess.
            previous = self._conns.get(sid)
            if previous is not None and previous is not conn:
                await self._close(previous, sid)
            self._conns[sid] = conn
            self._conn_cfg[sid] = cfg.model_dump()
            self._status.setdefault(sid, {})["connected_at"] = _now_iso()

        tools = await conn.list_tools()
        metas, skipped = self._map_tools(cfg, tools)
        self._defs[sid] = metas
        self._skipped[sid] = skipped
        self._listed_at[sid] = time.monotonic()
        self._failed_at.pop(sid, None)
        self._status[sid] = {
            "last_error": "",
            "server_info": conn.server_info,
            "protocol_version": conn.protocol_version,
            "connected_at": (self._status.get(sid) or {}).get("connected_at") or _now_iso(),
            "listed_at": _now_iso(),
        }
        self._reindex()
        self._save_cache(sid, conn, tools, metas, skipped)
        log.info("mcp[%s] ready: %d tool(s) from %s", sid, len(metas),
                 conn.server_info.get("name") or cfg.transport)

    async def refresh(self, server_id: str) -> dict:
        """Force a reconnect + rediscovery. Returns the resulting status."""
        self._maybe_reload()
        cfg = self._servers.get(server_id)
        if cfg is None:
            return {"state": "error", "last_error": "unknown server"}
        lock = self._refresh_locks.setdefault(server_id, asyncio.Lock())
        async with lock:
            # aclose_server settles any in-flight connect first, so tearing the
            # connection down cannot race with it publishing a fresh one.
            await self.aclose_server(server_id)
            self._listed_at.pop(server_id, None)
            self._defs.pop(server_id, None)
            self._status.pop(server_id, None)
            self._failed_at.pop(server_id, None)  # explicit retry: no cooldown
            self._reindex()
            if not cfg.enabled:
                return self.status(server_id)
            try:
                await asyncio.wait_for(
                    self._connect_and_list(server_id, cfg),
                    timeout=max(5.0, float(cfg.connect_timeout or 20)) * 2,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                message = "connect timed out" if isinstance(e, asyncio.TimeoutError) else str(e)
                self._mark_error(server_id, message)
            return self.status(server_id)

    def schedule_refresh(self, server_id: str) -> None:
        """Warm a server's catalogue in the background, without blocking a request.

        Adding or editing a server is an explicit user action, and the very next
        thing the user does is pick its tools for an agent — so the catalogue must
        not stay empty until the first chat turn. Failures are visible as the
        server's status/last_error.
        """
        cfg = self._servers.get(server_id)
        if cfg is None or not cfg.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. import time): the next turn will discover
        task = loop.create_task(self._background_refresh(server_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _background_refresh(self, server_id: str) -> None:
        try:
            await self.refresh(server_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("mcp[%s] background refresh failed: %s", server_id, e)

    async def test(self, cfg: McpServer) -> dict:
        """Connect an ephemeral session, list the tools, tear it down.

        Used by the UI before a config is saved, so it must never leave a
        subprocess behind and never touch the live pool.
        """
        conn = mcp_client.create_connection(cfg)
        started = time.monotonic()
        budget = max(5.0, float(cfg.connect_timeout or 20))
        try:
            try:
                await asyncio.wait_for(conn.connect(), timeout=budget)
                # A round trip after the handshake: proves the session answers,
                # not just that it opened.
                await asyncio.wait_for(conn.ping(), timeout=budget)
                tools = await asyncio.wait_for(conn.list_tools(), timeout=budget)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                message = f"timed out after {budget:g}s" if isinstance(e, asyncio.TimeoutError) else str(e)
                return {
                    "ok": False,
                    "error": message,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "stderr_tail": conn.stderr_tail[-20:],
                }
            metas, skipped = self._map_tools(cfg, tools)
            return {
                "ok": True,
                "error": "",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "server_info": conn.server_info,
                "protocol_version": conn.protocol_version,
                "capabilities": sorted(conn.capabilities or {}),
                "tool_count": len(metas),
                "tools": [
                    {
                        "remote_name": meta["mcp"]["tool"],
                        "id": meta["id"],
                        "description": meta.get("description", ""),
                        "parameters": sorted((meta.get("parameters") or {}).get("properties") or {}),
                    }
                    for meta in metas
                ],
                "skipped": skipped,
                "stderr_tail": conn.stderr_tail[-20:],
            }
        finally:
            await conn.aclose()

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    async def call(self, meta: dict, arguments: dict) -> str:
        """Run one MCP tool. Returns the model-facing string, never raises."""
        info = (meta or {}).get("mcp") or {}
        sid = info.get("server") or ""
        remote = info.get("tool") or ""
        cfg = self.get(sid)
        if cfg is None:
            return f"ERROR: MCP server '{sid}' is not configured"
        if not cfg.enabled:
            return f"ERROR: MCP server '{sid}' is disabled"
        # Defence in depth: discovery already filtered, re-check in case the
        # allow/deny lists changed after the definition was handed to the model.
        if not _tool_allowed(cfg, remote):
            return f"ERROR: tool '{remote}' is not allowed on MCP server '{sid}'"

        timeout = float(info.get("timeout") or cfg.timeout or 60)
        try:
            await self.ensure_for({sid})
            conn = self._conns.get(sid)
            if conn is None or not conn.alive:
                reason = (self._status.get(sid) or {}).get("last_error") or "not connected"
                return f"ERROR: MCP server '{sid}' unavailable: {reason}"
            raw = await conn.call_tool(remote, arguments or {}, timeout)
        except asyncio.CancelledError:
            raise
        except mcp_client.McpError as e:
            # Never retried: MCP tools are not idempotent and the request may
            # well have been executed before the failure.
            self._mark_error(sid, str(e))
            return f"ERROR: MCP tool '{remote}' failed on server '{sid}': {e}"
        except Exception as e:
            log.exception("mcp[%s] call to '%s' failed", sid, remote)
            return f"ERROR: MCP tool '{remote}' failed on server '{sid}': {e}"

        try:
            return result.flatten(
                raw,
                max_output=int(info.get("max_output") or cfg.max_output or 10000),
                workspace=self._workspace,
                label=remote or "mcp",
            )
        except Exception as e:
            # Belt and braces: a malformed result must not raise into the turn.
            log.exception("mcp[%s] cannot render the result of '%s'", sid, remote)
            return f"ERROR: MCP tool '{remote}' returned an unreadable result: {e}"

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    async def aclose_server(self, server_id: str) -> None:
        # Settle any connect still in flight FIRST: it runs detached (a waiter
        # may have timed out and walked away), and it publishes into _conns when
        # it finishes — closing before that would leave its subprocess running.
        await self._settle_connect(server_id)
        conn = self._conns.pop(server_id, None)
        self._conn_cfg.pop(server_id, None)
        if conn is not None:
            await self._close(conn, server_id)

    async def _settle_connect(self, server_id: str, timeout: float = 10.0) -> None:
        """Wait out (or cancel) an in-flight connect for one server."""
        task = self._connecting.get(server_id)
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return  # called from inside the connect itself: never await on self
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Timed out or the connect failed: either way stop it, then give the
            # cancellation a chance to unwind (client.connect() shields its own
            # cleanup, so the subprocess is reaped).
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=8)
            except Exception:
                pass

    @staticmethod
    async def _close(conn, server_id: str) -> None:
        try:
            await conn.aclose()
        except Exception as e:
            log.warning("mcp[%s] close failed: %s", server_id, e)

    async def forget(self, server_id: str) -> None:
        """Drop a deleted/disabled server from the live catalogue."""
        await self.aclose_server(server_id)
        self._defs.pop(server_id, None)
        self._skipped.pop(server_id, None)
        self._listed_at.pop(server_id, None)
        self._status.pop(server_id, None)
        self._failed_at.pop(server_id, None)
        self._reindex()

    async def aclose(self) -> None:
        """Stop every connection (called from the app's lifespan shutdown)."""
        # Cancel pending warm-ups first, or one could spawn a subprocess right
        # after we finish closing the pool.
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Every server with either a live connection OR a connect in flight: an
        # in-flight one would otherwise publish its subprocess after we are done.
        ids = list(dict.fromkeys([*self._conns, *self._connecting]))
        if ids:
            log.info("Closing %d MCP connection(s)", len(ids))
        await asyncio.gather(*(self.aclose_server(sid) for sid in ids),
                             return_exceptions=True)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _mark_error(self, sid: str, message: str) -> None:
        self._failed_at[sid] = time.monotonic()
        state = dict(self._status.get(sid) or {})
        state["last_error"] = message
        self._status[sid] = state

    def _map_tools(self, cfg: McpServer, tools: list[dict]) -> tuple[list[dict], list[dict]]:
        metas: list[dict] = []
        skipped: list[dict] = []
        used: set[str] = set()
        for tool in tools:
            remote = tool.get("name")
            if not isinstance(remote, str) or not remote:
                continue
            if not _tool_allowed(cfg, remote):
                skipped.append({"tool": remote, "reason": "filtered"})
                continue
            if len(metas) >= max(1, int(cfg.max_tools or 32)):
                skipped.append({"tool": remote, "reason": "max_tools reached"})
                continue
            qualified = naming.qualify(cfg.id, remote)
            if qualified in used:
                skipped.append({"tool": remote, "reason": f"name collision on {qualified}"})
                continue
            used.add(qualified)
            metas.append({
                "id": qualified,
                # Humans read the title, the model reads the id.
                "name": tool.get("title") or remote,
                "description": naming.truncate_description(tool.get("description")),
                "parameters": naming.sanitize_schema(tool.get("inputSchema")),
                "enabled": True,
                "source": "mcp",
                "mcp": {
                    "server": cfg.id,
                    "tool": remote,
                    "timeout": int(cfg.timeout or 60),
                    "max_output": int(cfg.max_output or 10000),
                },
            })
        return metas, skipped

    def _save_cache(self, sid: str, conn, tools: list[dict], metas: list[dict],
                    skipped: list[dict]) -> None:
        payload = {
            "server": sid,
            "fetched_at": _now_iso(),
            "server_info": conn.server_info,
            "protocol_version": conn.protocol_version,
            "tool_count": len(metas),
            "mapped": metas,
            "skipped": skipped,
        }
        try:
            self._cache.save(sid, payload)
            self._cached[sid] = payload
        except (OSError, ValueError) as e:
            log.warning("mcp[%s] cannot write tool cache: %s", sid, e)

    def drop_cache(self, sid: str) -> None:
        try:
            self._cache.delete(sid)
        except (OSError, ValueError):
            pass
        self._cached.pop(sid, None)


def _tool_allowed(cfg: McpServer, remote_name: str) -> bool:
    if cfg.deny_tools and remote_name in cfg.deny_tools:
        return False
    if cfg.allow_tools and remote_name not in cfg.allow_tools:
        return False
    return True
