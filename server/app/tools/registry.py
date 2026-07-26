from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class ToolRegistry:
    """Folder-based tool registry.

    Each tool is a subfolder of *tools_dir* containing:
      - tool.json  -- metadata (name, description, parameters in OpenAI JSON Schema)
      - run        -- executable script (any language, chmod +x)

    Tools with ``"internal": true`` in tool.json are handled by Python
    handlers registered via :meth:`register_internal`.
    """

    def __init__(self, tools_dir: Path, workdir: Path | None = None,
                 app_dir: Path | None = None):
        self._tools_dir = tools_dir
        # Working directory that external tools run in. Relative paths passed to
        # file/shell tools resolve here; absolute paths are unaffected. When
        # None, tools run in their own folder (legacy behaviour).
        self._workdir = workdir
        # App install dir, exported to tools as MYAGENT_APP_DIR so launchers can
        # find the app venv (tools live outside the app tree at runtime).
        self._app_dir = app_dir
        self._cache: dict[str, dict] = {}
        self._mtimes: dict[str, float] = {}
        self._internal_handlers: dict[str, Callable] = {}

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

    def _scan(self) -> None:
        """Rescan tools_dir.  Only re-reads tool.json files whose mtime changed."""
        if not self._tools_dir.exists():
            self._cache.clear()
            self._mtimes.clear()
            return

        current_ids: set[str] = set()

        for entry in self._tools_dir.iterdir():
            if not entry.is_dir():
                continue
            tool_json = entry / "tool.json"
            if not tool_json.exists():
                continue

            tool_id = entry.name
            if "/" in tool_id or ".." in tool_id:
                continue

            current_ids.add(tool_id)

            mtime = tool_json.stat().st_mtime
            if tool_id in self._mtimes and self._mtimes[tool_id] == mtime:
                continue

            try:
                with open(tool_json) as f:
                    meta = json.load(f)
                meta["id"] = tool_id
                self._cache[tool_id] = meta
                self._mtimes[tool_id] = mtime
                log.info("Loaded tool: %s", tool_id)
            except Exception as e:
                log.warning("Failed to load tool %s: %s", tool_id, e)

        # Remove deleted tools
        for old_id in list(self._cache.keys()):
            if old_id not in current_ids:
                del self._cache[old_id]
                self._mtimes.pop(old_id, None)
                log.info("Removed tool: %s", old_id)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_definition(self, tool_id: str) -> dict | None:
        self._scan()
        return self._cache.get(tool_id)

    def get_all_definitions(self) -> list[dict]:
        self._scan()
        return [m for m in self._cache.values() if m.get("enabled", True)]

    def get_definitions_for_agent(self, tool_ids: list[str]) -> list[dict]:
        self._scan()
        return [
            self._cache[tid]
            for tid in tool_ids
            if tid in self._cache and self._cache[tid].get("enabled", True)
        ]

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
        if tool_id not in self._cache:
            self._scan()
        meta = self._cache.get(tool_id)
        if meta is None:
            return f"ERROR: Unknown tool '{tool_id}'"
        if not meta.get("enabled", True):
            return f"ERROR: Tool '{tool_id}' is disabled"

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
            try:
                return await handler(**kwargs)
            except Exception as e:
                log.exception("Internal tool '%s' failed", tool_id)
                return f"ERROR: Tool '{tool_id}' failed: {e}"

        # External tool: run executable via subprocess
        return await self._execute_external(tool_id, arguments, meta)

    async def _execute_external(
        self, tool_id: str, arguments: dict, meta: dict
    ) -> str:
        tool_dir = self._tools_dir / tool_id
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
