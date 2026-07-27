"""Minimal MCP client: the ONLY module that knows the MCP/JSON-RPC wire format.

Everything above this file talks to five methods — ``connect()``, ``list_tools()``,
``call_tool()``, ``ping()``, ``aclose()`` — and sees only plain dicts. Swapping in
the official `mcp` SDK later is therefore a single-file change.

Implemented surface (tools only):
  initialize + notifications/initialized, tools/list (paginated), tools/call,
  ping, notifications/cancelled on timeout, notifications/tools/list_changed
  (invalidates the caller's cache), and a -32601 reply to any server->client
  request (we advertise no capabilities, but a sloppy server that asks for
  sampling would otherwise block waiting for an answer).

Deliberately out of scope: OAuth 2.1, the deprecated 2024-11-05 HTTP+SSE
transport, resources/prompts/sampling/roots, JSON-RPC batching (removed from the
spec; still tolerated when *reading*).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "myagent", "version": "0.1.0"}

# Pagination guard for tools/list.
MAX_LIST_PAGES = 20

# asyncio's default StreamReader limit is 64 KiB, and an MCP stdio frame is ONE
# line of JSON: a single tool result bigger than that raises ValueError inside
# readline() and kills the reader task (which looks exactly like "the server
# hung"). 8 MiB is the real ceiling for a tool result we would keep anyway.
STREAM_LIMIT = 8 * 1024 * 1024

# Never handed to an MCP server subprocess: it inherits our environment so that
# `npx`/`uvx` keep working, but not the credentials protecting this API.
_STRIPPED_ENV = {"MYAGENT_API_KEY", "MYAGENT_API_TOKEN"}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class McpError(Exception):
    """Any MCP-level failure: transport, protocol or JSON-RPC error."""


# ----------------------------------------------------------------------
# shared protocol logic
# ----------------------------------------------------------------------

class BaseConnection:
    """Transport-independent MCP session handling."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.protocol_version = ""
        self.server_info: dict = {}
        self.capabilities: dict = {}
        # Optional callback (sync, no args) invoked on notifications/tools/list_changed.
        self.on_tools_changed = None
        self._id = 0
        self._ready = False
        self._closed = False

    # -- lifecycle (implemented by subclasses) -------------------------

    async def connect(self) -> dict:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    @property
    def alive(self) -> bool:
        raise NotImplementedError

    @property
    def stderr_tail(self) -> list[str]:
        return []

    async def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        raise NotImplementedError

    async def _notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    # -- protocol ------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _handshake(self) -> dict:
        timeout = max(5.0, float(self.cfg.connect_timeout or 20))
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # We implement tools only. Advertising nothing keeps a compliant
                # server from asking us for sampling/roots/elicitation.
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
            timeout,
        )
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        caps = result.get("capabilities")
        self.capabilities = caps if isinstance(caps, dict) else {}
        # Set before the notification so it already carries the negotiated
        # MCP-Protocol-Version header (mandatory post-init on HTTP).
        self._ready = True
        await self._notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        """Every tool the server exposes, following nextCursor pagination."""
        timeout = max(5.0, float(self.cfg.connect_timeout or 20))
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params, timeout)
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            log.warning("mcp[%s] stopped paginating tools/list after %d pages",
                        self.cfg.id, MAX_LIST_PAGES)
        return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        """Raw tools/call result. A *tool* error comes back as a result with
        ``isError: true`` (not an exception); only protocol failures raise."""
        return await self._request(
            "tools/call",
            {"name": name, "arguments": arguments if isinstance(arguments, dict) else {}},
            timeout,
        )

    async def ping(self) -> None:
        await self._request("ping", {}, min(10.0, max(5.0, float(self.cfg.connect_timeout or 20))))

    # -- incoming messages ---------------------------------------------

    async def _handle_incoming(self, msg: dict) -> None:
        method = msg.get("method") or ""
        rid = msg.get("id")
        if rid is not None:
            # A server->client request. We advertise no capabilities, so nothing
            # here is supported — but it MUST be answered or a strict server
            # blocks forever waiting for a reply.
            await self._reply_unsupported(rid, method)
            return
        if method == "notifications/tools/list_changed":
            callback = self.on_tools_changed
            if callback is not None:
                try:
                    callback()
                except Exception:  # never let a callback break the read loop
                    log.debug("mcp[%s] tools_changed callback failed", self.cfg.id)
        # Everything else (notifications/message, notifications/progress, ...)
        # is intentionally ignored.

    async def _handle_incoming_safe(self, msg: dict) -> None:
        try:
            await self._handle_incoming(msg)
        except Exception as e:
            log.debug("mcp[%s] incoming message ignored: %s", self.cfg.id, e)

    async def _reply_unsupported(self, request_id, method: str) -> None:
        log.debug("mcp[%s] unanswered server request '%s'", self.cfg.id, method)

    async def _cancel(self, request_id, reason: str) -> None:
        """Tell the server to stop working on a request we gave up on."""
        try:
            await self._notify(
                "notifications/cancelled", {"requestId": request_id, "reason": reason}
            )
        except Exception:
            pass


# ----------------------------------------------------------------------
# stdio transport
# ----------------------------------------------------------------------

class StdioConnection(BaseConnection):
    """MCP over a local subprocess: newline-delimited JSON on stdin/stdout."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr = collections.deque(maxlen=40)
        self._write_lock = asyncio.Lock()
        self._error: str | None = None

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._error is None
        )

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    async def connect(self) -> dict:
        cfg = self.cfg
        if not (cfg.command or "").strip():
            raise McpError("no command configured")
        # Inherit the environment and layer the configured vars on top: a
        # replaced environment would lose PATH and break `npx`/`uvx`. Our own
        # secrets are stripped first — a third-party server has no business
        # holding the key that unlocks this API.
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        env.update({str(k): str(v) for k, v in (cfg.env or {}).items()})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                cfg.command,
                *[str(a) for a in (cfg.args or [])],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=(cfg.cwd or "").strip() or None,
                env=env,
                limit=STREAM_LIMIT,
            )
        except (OSError, ValueError) as e:
            raise McpError(f"cannot start '{cfg.command}': {e}") from e

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            return await self._handshake()
        except BaseException:
            # shield: if the handshake failed because WE were cancelled, the
            # cleanup must still finish or the subprocess is orphaned.
            await asyncio.shield(asyncio.ensure_future(self.aclose()))
            raise

    # -- io ------------------------------------------------------------

    async def _read_loop(self) -> None:
        stdout = self._proc.stdout if self._proc else None
        reason = "server closed the connection"
        try:
            while stdout is not None:
                try:
                    line = await stdout.readline()
                except ValueError:
                    # Frame past STREAM_LIMIT: the data is gone and the stream is
                    # no longer frame-aligned, so the session is unusable.
                    reason = f"a response exceeded the {STREAM_LIMIT} byte frame limit"
                    break
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except ValueError:
                    # Servers that print to stdout violate the spec; log the
                    # noise instead of tearing the session down.
                    log.debug("mcp[%s] non-JSON stdout: %.200s", self.cfg.id, stripped)
                    continue
                for one in msg if isinstance(msg, list) else [msg]:
                    if isinstance(one, dict):
                        await self._dispatch(one)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = f"reader failed: {e}"
            log.warning("mcp[%s] %s", self.cfg.id, reason)
        finally:
            self._fail_all(reason)

    async def _dispatch(self, msg: dict) -> None:
        rid = msg.get("id")
        if rid is not None and ("result" in msg or "error" in msg):
            future = self._pending.pop(rid, None)
            if future is not None and not future.done():
                future.set_result(msg)
            # No pending entry: the caller timed out or was cancelled. Dropping
            # the late response silently is the correct behaviour.
            return
        await self._handle_incoming_safe(msg)

    async def _drain_stderr(self) -> None:
        """Concurrently drain stderr: a full 64 KB pipe deadlocks chatty servers."""
        stderr = self._proc.stderr if self._proc else None
        try:
            while stderr is not None:
                try:
                    line = await stderr.readline()
                except ValueError:
                    continue  # over-long stderr line: skip, keep draining
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    log.debug("mcp[%s] stderr: %s", self.cfg.id, text)
        except asyncio.CancelledError:
            raise
        except OSError as e:
            log.debug("mcp[%s] stderr drain stopped: %s", self.cfg.id, e)

    async def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise McpError(self._error or "MCP server is not running")
        # json.dumps escapes newlines, so one message is always one line.
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (OSError, RuntimeError, ConnectionResetError) as e:
                raise McpError(f"write failed: {e}") from e

    async def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        if self._error:
            raise McpError(self._error)
        rid = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            payload["params"] = params
        try:
            await self._write(payload)
            msg = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self._cancel(rid, "timeout")
            raise McpError(f"'{method}' timed out after {timeout:g}s") from None
        finally:
            # Also runs when the caller is cancelled (chat Stop): the pending
            # entry goes away and the shared connection stays usable.
            self._pending.pop(rid, None)
        return _unwrap(msg, method)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        await self._write(payload)

    async def _reply_unsupported(self, request_id, method: str) -> None:
        try:
            await self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
        except McpError:
            pass

    def _fail_all(self, reason: str) -> None:
        if self._error is None:
            self._error = reason
        for rid in list(self._pending):
            future = self._pending.pop(rid, None)
            if future is not None and not future.done():
                future.set_exception(McpError(reason))

    async def aclose(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.returncode is None:
                # Closing stdin is the spec's graceful stdio shutdown signal.
                try:
                    if proc.stdin is not None and not proc.stdin.is_closing():
                        proc.stdin.close()
                except (OSError, RuntimeError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    for step in ("terminate", "kill"):
                        try:
                            getattr(proc, step)()
                            await asyncio.wait_for(proc.wait(), timeout=3)
                            break
                        except (asyncio.TimeoutError, OSError, ProcessLookupError):
                            continue
            # Always reap, even a child that was already dead (a command that
            # exits immediately): this is what lets asyncio close the subprocess
            # transport while the loop is still running. Skipping it leaves the
            # transport to __del__, which prints "Event loop is closed" at exit.
            try:
                await proc.wait()
            except (OSError, ProcessLookupError):
                pass
        tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        for task in tasks:
            task.cancel()
        self._reader_task = self._stderr_task = None
        # Await the cancellations: a cancelled task needs one more loop tick to
        # unwind, and leaving them pending makes teardown non-deterministic (they
        # would run their cleanup against an already-closed loop).
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_all("connection closed")


# ----------------------------------------------------------------------
# Streamable HTTP transport
# ----------------------------------------------------------------------

class HttpConnection(BaseConnection):
    """MCP over Streamable HTTP (spec revision 2025-03-26 and later).

    Each request is a POST whose reply is either a JSON object or an SSE stream;
    there is no background listen stream, so ``notifications/tools/list_changed``
    only arrives if a server interleaves it into a reply (the TTL and the manual
    refresh cover the rest).
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    @property
    def alive(self) -> bool:
        return self._client is not None and not self._closed

    async def connect(self) -> dict:
        cfg = self.cfg
        headers = {str(k): str(v) for k, v in (cfg.headers or {}).items()}
        if (cfg.bearer or "").strip():
            headers["authorization"] = f"Bearer {cfg.bearer.strip()}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                connect=10.0, read=float(cfg.timeout or 60), write=10.0, pool=10.0
            ),
            follow_redirects=True,
        )
        try:
            return await self._handshake()
        except BaseException:
            await asyncio.shield(asyncio.ensure_future(self.aclose()))
            raise

    async def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            payload["params"] = params
        try:
            msg = await self._post(
                payload, timeout, want_id=rid, allow_reinit=(method != "initialize")
            )
        except httpx.TimeoutException:
            await self._cancel(rid, "timeout")
            raise McpError(f"'{method}' timed out after {timeout:g}s") from None
        except httpx.HTTPError as e:
            raise McpError(f"{type(e).__name__}: {e}") from e
        if msg is None:
            raise McpError(f"no response to '{method}'")
        return _unwrap(msg, method)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        try:
            await self._post(payload, 10.0, want_id=None, allow_reinit=False)
        except (httpx.HTTPError, McpError) as e:
            log.debug("mcp[%s] notification '%s' failed: %s", self.cfg.id, method, e)

    async def _post(self, payload: dict, timeout: float, *, want_id,
                    allow_reinit: bool) -> dict | None:
        client = self._client
        if client is None:
            raise McpError("connection is closed")
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if self._ready and self.protocol_version:
            # Mandatory on every post-initialize request from 2025-06-18 on.
            headers["mcp-protocol-version"] = self.protocol_version

        stale_session = False
        async with client.stream(
            "POST", self.cfg.url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code == 202:
                return None  # accepted notification: no body to parse
            elif resp.status_code >= 400:
                body = _clean_error_body(await resp.aread())
                # The spec says an expired session answers 404, but the reference
                # TypeScript server answers 400 "No valid session ID provided".
                # Accept both — only when a session was actually in play, so a
                # genuine bad request still surfaces its own message.
                if (
                    self._session_id
                    and allow_reinit
                    and (resp.status_code == 404 or "session" in body.lower())
                ):
                    stale_session = True
                else:
                    raise McpError(f"HTTP {resp.status_code}: {body or resp.reason_phrase}")
            else:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid
                ctype = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" in ctype:
                    return await self._read_sse(resp, want_id)
                raw = await resp.aread()
                if not raw.strip():
                    return None
                try:
                    return _match(json.loads(raw), want_id, sole=True)
                except ValueError as e:
                    raise McpError(f"malformed JSON response: {e}") from e

        # The server dropped our session: re-initialize once and replay.
        if stale_session:
            self._session_id = None
            self._ready = False
            await self._handshake()
            return await self._post(payload, timeout, want_id=want_id, allow_reinit=False)
        return None

    async def _read_sse(self, resp: httpx.Response, want_id) -> dict | None:
        """Read the reply out of an SSE body, dispatching interleaved messages."""
        data_lines: list[str] = []
        async for raw in resp.aiter_lines():
            line = raw.rstrip("\r\n")
            if line.startswith(":"):
                continue  # comment / keep-alive
            if line:
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # event: / id: / retry: carry nothing we need
                continue
            # Blank line: the accumulated data lines are one complete event.
            msg = _decode_event(data_lines)
            data_lines = []
            if msg is None:
                continue
            matched = _match(msg, want_id, sole=False)
            if matched is not None:
                return matched
            if isinstance(msg, dict):
                await self._handle_incoming_safe(msg)
        msg = _decode_event(data_lines)
        if msg is not None:
            matched = _match(msg, want_id, sole=False)
            if matched is not None:
                return matched
        raise McpError("SSE stream ended without a response")

    async def aclose(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            if self._session_id:
                # Best-effort explicit session termination (spec: DELETE).
                try:
                    await client.delete(
                        self.cfg.url,
                        headers={"mcp-session-id": self._session_id},
                        timeout=5.0,
                    )
                except (httpx.HTTPError, RuntimeError):
                    pass
            try:
                await client.aclose()
            except (httpx.HTTPError, RuntimeError):
                pass
        self._session_id = None
        self._ready = False


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def create_connection(cfg) -> BaseConnection:
    if cfg.transport == "stdio":
        return StdioConnection(cfg)
    if cfg.transport == "http":
        return HttpConnection(cfg)
    raise McpError(f"unsupported transport '{cfg.transport}'")


def _clean_error_body(raw: bytes) -> str:
    """Shorten and de-fang a remote error body before it becomes an error string.

    This text ends up in the server's ``last_error``, which the UI renders (and
    the model may see): it is third-party content, so strip control characters,
    collapse whitespace and keep it short.
    """
    text = raw.decode("utf-8", "replace")
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text[:200].strip()


def _unwrap(msg: dict, method: str) -> dict:
    error = msg.get("error")
    if isinstance(error, dict):
        raise McpError(
            f"{method} failed (code {error.get('code')}): "
            f"{error.get('message') or 'unknown error'}"
        )
    result = msg.get("result")
    return result if isinstance(result, dict) else {}


def _decode_event(data_lines: list[str]) -> dict | list | None:
    if not data_lines:
        return None
    try:
        decoded = json.loads("\n".join(data_lines))
    except ValueError:
        return None
    return decoded if isinstance(decoded, (dict, list)) else None


def _match(data: object, want_id, *, sole: bool) -> dict | None:
    """Find our response in a decoded body.

    *sole* means the body can only be the answer to our single POST, so an
    id mismatch (some servers echo none) is tolerated. Inside an SSE stream the
    match must be exact, since other messages are interleaved.
    """
    if isinstance(data, list):  # batching is gone from the spec, still tolerated
        for item in data:
            if isinstance(item, dict) and item.get("id") == want_id:
                return item
        return None
    if not isinstance(data, dict):
        return None
    if data.get("id") == want_id:
        return data
    if sole and ("result" in data or "error" in data):
        return data
    return None
