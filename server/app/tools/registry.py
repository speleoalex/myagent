from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Callable

log = logging.getLogger(__name__)

# Shebang interpreter -> label shown as the tool's language badge. An
# interpreter that is not listed falls back to its own name, so an exotic
# shebang still gets a badge instead of a hole.
_LANGUAGE_LABELS = {
    "python": "Python", "python2": "Python", "python3": "Python",
    "sh": "Shell", "dash": "Shell", "bash": "Bash", "zsh": "Zsh", "fish": "Fish",
    "node": "Node.js", "nodejs": "Node.js", "deno": "Deno", "bun": "Bun",
    "ruby": "Ruby", "perl": "Perl", "php": "PHP", "lua": "Lua", "rscript": "R",
}

# Extension -> label, used only to see through a shell launcher (below).
_LANGUAGE_BY_EXT = {
    ".py": "Python", ".js": "Node.js", ".mjs": "Node.js", ".cjs": "Node.js",
    ".ts": "TypeScript", ".rb": "Ruby", ".pl": "Perl", ".php": "PHP",
    ".lua": "Lua", ".sh": "Shell",
}

_SHELL_LABELS = {"Shell", "Bash", "Zsh", "Fish"}


def _detect_language(run_path: Path) -> str | None:
    """The language a tool is written in, read from its ``run`` shebang.

    A tool that needs packages from the app venv is a small shell launcher that
    execs the real script (the pattern documented in docs/TOOLS.md, e.g.
    ``local_search``), so for a shell shebang the exec'd file decides: reporting
    "Shell" for what is plainly a Python tool would be true and useless.
    """
    try:
        with open(run_path, errors="replace") as f:
            head = f.read(2048)
    except OSError:
        return None
    first, _, body = head.partition("\n")
    if not first.startswith("#!"):
        return None
    parts = first[2:].strip().split()
    if not parts:
        return None
    interp = PurePosixPath(parts[0]).name
    if interp == "env":
        # #!/usr/bin/env python3   /   #!/usr/bin/env -S python3 -u
        rest = [p for p in parts[1:] if not p.startswith("-") and "=" not in p]
        if not rest:
            return None
        interp = PurePosixPath(rest[0]).name

    key = interp.lower()
    label = _LANGUAGE_LABELS.get(key)
    if label is None:  # python3.12 -> python, else keep the raw name
        label = _LANGUAGE_LABELS.get(key.rstrip("0123456789."), interp)

    if label in _SHELL_LABELS:
        # Only exec lines: a mention in a comment must not decide the language.
        for line in body.splitlines():
            if not line.strip().startswith("exec "):
                continue
            for ext in re.findall(r"\.[A-Za-z0-9]+", line):
                if ext.lower() in _LANGUAGE_BY_EXT:
                    return _LANGUAGE_BY_EXT[ext.lower()]
    return label


class ToolRegistry:
    """Folder-based tool registry: the user dir overlaid on the bundled catalog.

    Each tool is a folder containing:
      - tool.json  -- metadata (name, description, parameters in OpenAI JSON Schema)
      - run        -- executable script (any language, chmod +x)

    Tools come from two layers. The *bundled* catalog (``bundled_dir``, the
    app's own ``server/tools/``) is always present — native tools need no
    install step. The *user* dir (``tools_dir``, ``~/myagent/tools``) holds
    user/AI-created tools and **overrides**: editing a native tool copies its
    folder here first (copy-on-write, done by the callers via
    :meth:`ensure_override`), and the copy wins the id collision from then on.
    Deleting the override reveals the bundled original again — that is what
    "reset" means. Nothing is ever seeded or duplicated up front.

    A subfolder WITHOUT a tool.json is a *group* (category): its own
    subfolders are scanned as tools, one level deep. The group folder name
    becomes the tools' ``category`` (folder layout is the single source of
    truth; a ``category`` key written in tool.json is ignored). For native
    tools the BUNDLED layout decides the category, wherever the override
    happens to live — a legacy flat copy of a grouped tool keeps its group.
    Tool ids stay globally unique — the leaf folder name — so moving a tool
    into a group changes nothing for the agents that reference it. An agent's
    ``tools`` list may grant a whole group at once with ``<category>/*``, the
    analogue of the ``mcp:<server>/*`` wildcard.

    Tools with ``"internal": true`` in tool.json are handled by Python
    handlers registered via :meth:`register_internal`; they are served from
    the bundle and are not editable, so they never have overrides.
    """

    def __init__(self, tools_dir: Path, workdir: Path | None = None,
                 app_dir: Path | None = None, bundled_dir: Path | None = None):
        self._tools_dir = tools_dir
        # The read-only native catalog underlay. None (or pointing at the same
        # folder as tools_dir) disables the overlay and scans tools_dir alone.
        if bundled_dir is not None:
            try:
                if bundled_dir.resolve() == tools_dir.resolve():
                    bundled_dir = None
            except OSError:
                bundled_dir = None
        self._bundled_dir = bundled_dir
        # Working directory that external tools run in. Relative paths passed to
        # file/shell tools resolve here; absolute paths are unaffected. When
        # None, tools run in their own folder (legacy behaviour).
        self._workdir = workdir
        # App install dir, exported to tools as MYAGENT_APP_DIR so launchers can
        # find the app venv (tools live outside the app tree at runtime).
        self._app_dir = app_dir
        self._cache: dict[str, dict] = {}
        self._mtimes: dict[str, float] = {}
        # Scan debounce: every query method calls _scan(), and one GET
        # /api/tools annotates each tool with 3 more queries — dozens of full
        # directory walks per request without this. Within the TTL the cache
        # answers; manual folder edits are still picked up within a second
        # (hot reload preserved), and every write path calls mark_dirty().
        self._last_scan = 0.0
        # tool_id -> RESOLVED folder (the user override when there is one, the
        # bundled folder otherwise); execution and the CRUD routes go through
        # this instead of assuming a location.
        self._dirs: dict[str, Path] = {}
        # Per-layer locations, for override management (copy-on-write, reset).
        self._native_dirs: dict[str, Path] = {}
        self._user_dirs: dict[str, Path] = {}
        self._internal_handlers: dict[str, Callable] = {}
        # Second tool source: external MCP servers (app.mcp.manager.McpManager),
        # assigned after construction because it needs no event loop here. Their
        # definitions are kept OUT of self._cache: _scan() deletes every id it
        # doesn't see on disk, which would evict them on the next rescan.
        self.mcp_manager = None
        # The address book behind notify_user's schema: a zero-argument callable
        # returning {"contacts": [...], "channels": [...], "broadcast": "all"}.
        # Assigned after construction for the same reason as mcp_manager, plus
        # one of its own: it belongs to a PLUGIN, which registers itself into app
        # state later, so this has to be a call and not a value.
        self.notify_targets = None

    @property
    def tools_dir(self) -> Path:
        return self._tools_dir

    # ------------------------------------------------------------------
    # Internal (Python) handlers
    # ------------------------------------------------------------------

    def register_internal(self, tool_id: str, handler: Callable) -> None:
        """Register a Python async handler for an internal tool."""
        self._internal_handlers[tool_id] = handler

    # ------------------------------------------------------------------
    # Filesystem scanning with mtime cache
    # ------------------------------------------------------------------

    @staticmethod
    def _walk_layer(root: Path | None) -> dict[str, tuple[Path, str | None]]:
        """One layer's tools as ``id -> (folder, category)``.

        Flat tools are collected before group folders, so on an id collision
        within the layer the flat copy deterministically wins."""
        out: dict[str, tuple[Path, str | None]] = {}
        if root is None or not root.exists():
            return out

        def add(entry: Path, category: str | None) -> None:
            tool_id = entry.name
            if "/" in tool_id or ".." in tool_id:
                return
            if tool_id in out:
                log.warning("Duplicate tool id %r: ignoring %s", tool_id, entry)
                return
            out[tool_id] = (entry, category)

        entries = sorted(e for e in root.iterdir() if e.is_dir())
        groups = []
        for entry in entries:
            if (entry / "tool.json").exists():
                add(entry, None)
            elif "/" not in entry.name and ".." not in entry.name:
                groups.append(entry)
        for entry in groups:
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and (sub / "tool.json").exists():
                    add(sub, entry.name)
        return out

    _SCAN_TTL = 1.0  # seconds a scan result stays authoritative

    def mark_dirty(self) -> None:
        """Make the next query rescan immediately. Every write path (the tools
        router, ensure_override) calls this so its own follow-up reads never
        see a stale catalog."""
        self._last_scan = 0.0

    def _scan(self, force: bool = False) -> None:
        """Rescan both layers.  Only re-reads tool.json files whose mtime (or
        resolution: which layer wins, what category applies) changed.
        Debounced by _SCAN_TTL unless ``force``."""
        now = time.monotonic()
        if not force and now - self._last_scan < self._SCAN_TTL:
            return
        self._last_scan = now
        native = self._walk_layer(self._bundled_dir)
        user = self._walk_layer(self._tools_dir)
        self._native_dirs = {tid: entry for tid, (entry, _cat) in native.items()}
        self._user_dirs = {tid: entry for tid, (entry, _cat) in user.items()}

        current_ids = set(native) | set(user)
        for tool_id in current_ids:
            user_entry, user_cat = user.get(tool_id, (None, None))
            native_entry, native_cat = native.get(tool_id, (None, None))
            entry = user_entry if user_entry is not None else native_entry
            # For native tools the bundled layout owns the grouping: a legacy
            # flat override of a grouped tool must keep its category, or a
            # `<group>/*` grant would silently stop covering it.
            category = native_cat if native_entry is not None else user_cat
            origin = "native" if native_entry is not None else "custom"

            tool_json = entry / "tool.json"
            try:
                mtime = tool_json.stat().st_mtime
            except OSError:
                continue
            # mtime alone is not enough: a folder move or an override appearing
            # or disappearing changes behaviour without touching the file.
            cached = self._cache.get(tool_id)
            if (
                cached is not None
                and self._mtimes.get(tool_id) == mtime
                and self._dirs.get(tool_id) == entry
                and cached.get("category") == category
                and cached.get("origin") == origin
            ):
                continue

            try:
                with open(tool_json) as f:
                    meta = json.load(f)
                meta["id"] = tool_id
                if category is not None:
                    meta["category"] = category
                else:
                    meta.pop("category", None)
                meta["origin"] = origin
                # Detected, never declared — like `category`, the files are the
                # truth. Cached with tool.json's mtime: every write path
                # (API, manage_tools) rewrites tool.json too, so the badge
                # follows a script edit; a shebang changed by hand alone is
                # picked up at the next tool.json touch or restart.
                if meta.get("internal"):
                    # In-process handlers are Python callables by construction.
                    meta["language"] = "Python"
                else:
                    lang = _detect_language(entry / "run")
                    if lang:
                        meta["language"] = lang
                    else:
                        meta.pop("language", None)
                self._cache[tool_id] = meta
                self._mtimes[tool_id] = mtime
                self._dirs[tool_id] = entry
                log.info(
                    "Loaded tool: %s%s%s", tool_id,
                    f" (group {category})" if category else "",
                    " [override]" if origin == "native" and user_entry is not None else "",
                )
            except Exception as e:
                log.warning("Failed to load tool %s: %s", tool_id, e)

        # Remove deleted tools
        for old_id in list(self._cache.keys()):
            if old_id not in current_ids:
                del self._cache[old_id]
                self._mtimes.pop(old_id, None)
                self._dirs.pop(old_id, None)
                log.info("Removed tool: %s", old_id)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    @staticmethod
    def parse_category_wildcard(entry: str) -> str | None:
        """``<category>/*`` -> category name, else None.

        Category names share the tool-id charset (no ``:``), so this can never
        match an ``mcp:<server>/*`` entry."""
        if entry.endswith("/*"):
            cat = entry[:-2]
            if cat and "/" not in cat and ":" not in cat and ".." not in cat:
                return cat
        return None

    def get_definition(self, tool_id: str) -> dict | None:
        self._scan()
        found = self._cache.get(tool_id)
        if found is not None or self.mcp_manager is None:
            return found
        return self.mcp_manager.get_meta(tool_id)

    def tool_dir(self, tool_id: str) -> Path | None:
        """RESOLVED folder of a filesystem tool (the override when present)."""
        self._scan()
        return self._dirs.get(tool_id)

    def native_dir(self, tool_id: str) -> Path | None:
        """Bundled folder of a native tool, None for custom tools."""
        self._scan()
        return self._native_dirs.get(tool_id)

    def user_dir(self, tool_id: str) -> Path | None:
        """User folder of a tool: the override of a native, or the custom
        tool itself. None when the tool is served straight from the bundle."""
        self._scan()
        return self._user_dirs.get(tool_id)

    def is_modified(self, tool_id: str) -> bool:
        """True when a native tool's override actually differs from the
        bundled original (parsed tool.json + run script bytes — vendored
        subfolders are deliberately not compared). A byte-identical override
        (e.g. left behind by the old first-run seeding) is NOT a modification;
        it just shadows the bundle harmlessly."""
        self._scan()
        native, user = self._native_dirs.get(tool_id), self._user_dirs.get(tool_id)
        if native is None or user is None:
            return False

        def load(p: Path):
            try:
                return json.loads((p / "tool.json").read_text())
            except Exception:
                return None

        if load(native) != load(user):
            return True
        nr, ur = native / "run", user / "run"
        if nr.exists() != ur.exists():
            return True
        if nr.exists():
            try:
                return nr.read_bytes() != ur.read_bytes()
            except OSError:
                return True
        return False

    def ensure_override(self, tool_id: str) -> Path | None:
        """Copy-on-write: make sure the tool has a user folder and return it.

        A native tool served from the bundle gets its whole folder copied into
        the user dir first (same group layout, vendored deps included — a Node
        tool without its node_modules would not run). Editing then happens on
        the copy; the bundled original is never written to. Returns None for
        unknown ids."""
        self._scan()
        user = self._user_dirs.get(tool_id)
        if user is not None:
            return user
        native = self._native_dirs.get(tool_id)
        if native is None:
            return None
        meta = self._cache.get(tool_id) or {}
        rel = Path(meta["category"]) / tool_id if meta.get("category") else Path(tool_id)
        dst = self._tools_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            native, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        self.mark_dirty()
        log.info("Created override for native tool %s at %s", tool_id, dst)
        return dst

    def expand_tool_ids(self, tool_ids: list[str]) -> list[str]:
        """Expand ``<category>/*`` grants into the enabled tool ids of that
        group; every other entry (plain ids, ``mcp:`` entries) passes through
        verbatim. For code that asks "does this agent hold tool X?" — the
        wildcard-aware replacement for ``x in agent.tools``."""
        self._scan()
        out: list[str] = []
        seen: set[str] = set()
        for entry in tool_ids:
            cat = self.parse_category_wildcard(entry)
            if cat is not None:
                expanded = [
                    m["id"] for m in self._cache.values()
                    if m.get("category") == cat and m.get("enabled", True)
                ]
            else:
                expanded = [entry]
            for tid in expanded:
                if tid not in seen:
                    seen.add(tid)
                    out.append(tid)
        return out

    def get_all_definitions(self, include_mcp: bool = False) -> list[dict]:
        """Every enabled tool. MCP tools are opt-in: the tool CRUD surface deals
        in folders, so they must not leak into it."""
        self._scan()
        out = [m for m in self._cache.values() if m.get("enabled", True)]
        if include_mcp and self.mcp_manager is not None:
            known = set(self._cache)
            out += [m for m in self.mcp_manager.catalogue() if m["id"] not in known]
        return out

    def get_definitions_for_agent(self, tool_ids: list[str]) -> list[dict]:
        """Definitions for an agent's tool list, MCP entries included.

        Filesystem entries may be a tool id or a group wildcard
        (``<category>/*``); MCP entries may be a qualified tool id or a
        per-server wildcard (``mcp:<server>/*``). All are expanded here, so
        nothing downstream ever sees a wildcard. A filesystem tool always wins
        an id collision."""
        out = [
            self._cache[tid]
            for tid in self.expand_tool_ids(tool_ids)  # _scan() happens inside
            if tid in self._cache and self._cache[tid].get("enabled", True)
        ]
        if self.mcp_manager is not None:
            out += [
                meta
                for meta in self.mcp_manager.defs_for_tool_ids(tool_ids)
                if meta["id"] not in self._cache
            ]
        return out

    async def ensure_mcp(self, tool_ids: list[str]) -> None:
        """Connect (lazily) the MCP servers this agent's tools live on.

        Called once per turn before the definitions are built, since discovery is
        async while the query methods above are sync. Cheap and side-effect free
        when the agent references no MCP tool, and never raises: a server that is
        down simply contributes no tools.
        """
        manager = self.mcp_manager
        if manager is None or not tool_ids:
            return
        try:
            server_ids = manager.servers_for_tool_ids(tool_ids)
            if server_ids:
                await manager.ensure_for(server_ids)
        except Exception as e:  # belt and braces: the turn must go on
            log.warning("MCP preparation failed: %s", e)

    # ------------------------------------------------------------------
    # OpenAI function-calling format
    # ------------------------------------------------------------------

    @staticmethod
    def to_openai_format(tool_metas: list[dict]) -> list[dict]:
        """Convert tool metadata dicts to OpenAI function calling format."""
        result = []
        for meta in tool_metas:
            result.append({
                "type": "function",
                "function": {
                    "name": meta["id"],
                    "description": meta.get("description", ""),
                    "parameters": meta.get("parameters", {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    }),
                },
            })
        return result

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, tool_id: str, arguments: dict, **extra) -> str:
        """Run a tool and ALWAYS return a string. The executor feeds this back
        to the model with no try/except of its own, so an exception escaping
        here (a stat() race in _execute_external, a handler bug) would abort
        the whole chat turn instead of coming back as an in-band tool error —
        this outer guard is the one enforcement point of that contract."""
        try:
            return await self._execute(tool_id, arguments, **extra)
        except Exception as e:
            log.exception("Tool '%s' failed", tool_id)
            return f"ERROR: Tool '{tool_id}' failed: {e}"

    async def _execute(self, tool_id: str, arguments: dict, **extra) -> str:
        # MCP first: their ids live outside the filesystem cache, and looking
        # there first keeps every MCP call from triggering a directory rescan.
        # `tool_id not in self._cache` preserves filesystem precedence without
        # paying for a rescan (the cache was just refreshed when this turn's
        # definitions were built).
        meta = None
        if self.mcp_manager is not None and tool_id not in self._cache:
            meta = self.mcp_manager.get_meta(tool_id)
        if meta is None:
            if tool_id not in self._cache:
                # force: a tool created seconds ago (manage_tools) must be
                # callable in the same turn, debounce notwithstanding.
                self._scan(force=True)
            meta = self._cache.get(tool_id)
        if meta is None:
            return f"ERROR: Unknown tool '{tool_id}'"
        if not meta.get("enabled", True):
            return f"ERROR: Tool '{tool_id}' is disabled"

        # External MCP server. `extra` (the executor handle) is deliberately not
        # forwarded: a remote server has no business with executor context.
        if meta.get("mcp"):
            if self.mcp_manager is None:
                return "ERROR: MCP support is not initialized"
            return await self.mcp_manager.call(meta, arguments)

        # Internal tool (e.g. call_agent). Pass only the kwargs the handler
        # accepts — never retry on TypeError: a handler with side effects
        # (call_agent runs a whole sub-agent) must not be executed twice.
        if meta.get("internal"):
            handler = self._internal_handlers.get(tool_id)
            if handler is None:
                return f"ERROR: No internal handler for '{tool_id}'"
            params = inspect.signature(handler).parameters
            accepts_any = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            kwargs = {**arguments, **extra}
            if not accepts_any:
                kwargs = {k: v for k, v in kwargs.items() if k in params}
            return await handler(**kwargs)

        # External tool: run executable via subprocess
        return await self._execute_external(tool_id, arguments, meta)

    async def _execute_external(
        self, tool_id: str, arguments: dict, meta: dict
    ) -> str:
        tool_dir = self._dirs.get(tool_id, self._tools_dir / tool_id)
        run_path = tool_dir / "run"

        if not run_path.exists():
            return f"ERROR: Tool '{tool_id}' has no 'run' executable"
        if not run_path.stat().st_mode & 0o111:
            return f"ERROR: Tool '{tool_id}'/run is not executable (chmod +x needed)"

        timeout = meta.get("timeout", 30)
        max_output = meta.get("max_output", 10000)

        input_json = json.dumps(arguments)

        # Run the tool in the shared working directory so that relative file
        # paths (and shell commands) resolve there. Fall back to the tool's own
        # folder if the workspace can't be created. The `run` executable is
        # launched by absolute path, so cwd doesn't affect finding it; Node
        # tools resolve node_modules via __dirname, not cwd.
        cwd = tool_dir
        env = {**os.environ}
        if self._app_dir is not None:
            env["MYAGENT_APP_DIR"] = str(self._app_dir)
        if self._workdir is not None:
            try:
                self._workdir.mkdir(parents=True, exist_ok=True)
                cwd = self._workdir
            except OSError as e:
                log.warning("Cannot use workspace %s: %s", self._workdir, e)
            env["MYAGENT_WORKDIR"] = str(self._workdir)

        # A broken shebang / missing interpreter must come back to the model as
        # an in-band tool error, not blow up the whole chat turn with a 500.
        try:
            proc = await asyncio.create_subprocess_exec(
                str(run_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )
        except OSError as e:
            return f"ERROR: Cannot execute tool '{tool_id}': {e}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json.encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"ERROR: Tool '{tool_id}' timed out after {timeout}s"

        result = stdout.decode(errors="replace")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if err:
                result = f"ERROR (exit {proc.returncode}): {err}\n{result}".strip()
            elif not result.strip():
                result = f"ERROR: Tool '{tool_id}' exited with code {proc.returncode}"

        if len(result) > max_output:
            result = result[:max_output] + "\n... [truncated]"

        return result.strip()
