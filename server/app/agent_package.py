"""One agent as a single file: export it, import it on another instance.

A working agent is four things in four places: its JSON, the user tools it
calls, its scheduled tasks and those tools' settings. Moving "cerca-lavoro"
off the Orin meant remembering all four, and its user tools were versioned
nowhere. The package is a .tar.gz with a fixed layout::

    manifest.json              format, agent id, what is inside
    agent.json
    tasks/<task_id>.json       definition only, no run state
    tools/<path>/...           user-layer tool folders (custom tools and edited
                               native ones), at their path under tools/
    tool_settings/<id>.json    the tools' settings WITHOUT secrets

What it never carries: secrets (a backup that leaks a password is worse than
no backup), sessions and memory (they are conversations, not configuration),
native tools nobody edited (the other instance has them), MCP servers (their
own configuration, named in the manifest so the gap is visible).

Import is all-or-nothing on conflicts: every collision is found BEFORE the
first write, and without ``overwrite`` nothing is written. The agent arrives
with ``live`` off, whatever it had: an agent that starts running tasks the
moment it lands on another machine is a surprise nobody asked for.
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import time
from pathlib import Path, PurePosixPath

from app.ids import is_valid_id
from app.mcp import naming as mcp_naming
from app.models import Agent, Task
from app.tools import tool_config

FORMAT = 1
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
# Rebuilt on the other side (install.sh / the tool's own setup), and large.
_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".git"}
_TASK_DEFINITION = ("id", "agent_id", "prompt", "cron", "at", "enabled", "source")


class PackageError(ValueError):
    """The package cannot be imported as it is; the message says why."""


class Conflict(PackageError):
    """Something in the package already exists here (import without overwrite).
    ``items`` lists what, as "agent x" / "task y" / "tool z"."""

    def __init__(self, items: list[str]):
        super().__init__("already exists: " + ", ".join(items))
        self.items = items


# ------------------------------------------------------------------ export
def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _add_json(tar: tarfile.TarFile, name: str, data) -> None:
    _add_bytes(tar, name, json.dumps(data, ensure_ascii=False, indent=2).encode())


def export_agent(agent_id: str, stores, tasks, registry) -> bytes:
    """The package of one agent, as bytes. KeyError when it does not exist."""
    data = stores.agents.get(agent_id)
    if data is None:
        raise KeyError(agent_id)
    agent = Agent(**data)
    tools_root = Path(registry.tools_dir)
    exported_tools, external = [], []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_json(tar, "agent.json", agent.model_dump())
        own_tasks = tasks.list_all(agent_id)
        for t in own_tasks:
            _add_json(tar, f"tasks/{t['id']}.json",
                      {k: t[k] for k in _TASK_DEFINITION if k in t})
        for tool_id in registry.expand_tool_ids(agent.tools):
            # By NAME, not by asking the MCP manager: with the server down the
            # definition is gone, and the tool would drop out of the manifest
            # without a word. A folder tool that happens to start with "mcp_"
            # has a folder, which settles it.
            if mcp_naming.parse_wildcard(tool_id) or (
                    mcp_naming.after_prefix(tool_id) and registry.tool_dir(tool_id) is None):
                external.append(tool_id)
                continue
            meta = registry.get_definition(tool_id) or {}
            folder = registry.user_dir(tool_id)
            if folder is not None:
                rel = folder.relative_to(tools_root).as_posix()
                for path in sorted(folder.rglob("*")):
                    if set(path.relative_to(folder).parts) & _SKIP_DIRS:
                        continue
                    if path.is_file() and not path.is_symlink():
                        mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
                        _add_bytes(tar, f"tools/{rel}/{path.relative_to(folder).as_posix()}",
                                   path.read_bytes(), mode)
                exported_tools.append(tool_id)
            current = tool_config.values(tool_id, meta)
            settings = {f["key"]: current[f["key"]] for f in tool_config.fields(meta)
                        if not f["secret"] and current[f["key"]]}
            if settings:
                _add_json(tar, f"tool_settings/{tool_id}.json", settings)
        _add_json(tar, "manifest.json", {
            "format": FORMAT,
            "agent_id": agent_id,
            "tools": exported_tools,
            "tasks": [t["id"] for t in own_tasks],
            # Granted, but configured on an MCP server this package cannot carry.
            "external_tools": external,
        })
    return buf.getvalue()


# ------------------------------------------------------------------ import
def _read(blob: bytes) -> dict[str, tuple[bytes, int]]:
    """Every regular file of the package: ``{path: (bytes, mode)}``.

    By hand rather than ``extractall`` for the plugin installer's reasons:
    regular files only (a link is how an archive writes outside its target),
    no ``..``, no absolute paths, a size cap — and nothing reaches the disk
    here at all, so a bad package is refused before any write."""
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise PackageError(f"not a regular file: {member.name}")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise PackageError(f"unsafe path: {member.name}")
                total += member.size
                if total > MAX_PACKAGE_BYTES:
                    raise PackageError("the package is too large")
                source = tar.extractfile(member)
                files[path.as_posix()] = (source.read() if source else b"", member.mode)
    except tarfile.TarError as e:
        raise PackageError(f"not a valid package: {e}") from e
    return files


def _json(files: dict, name: str):
    try:
        return json.loads(files[name][0])
    except KeyError:
        raise PackageError(f"missing {name}") from None
    except ValueError as e:
        raise PackageError(f"{name} is not valid JSON: {e}") from None


def import_agent(blob: bytes, stores, tasks, registry, *, overwrite: bool = False) -> dict:
    """Install a package. Returns what was installed and what is still
    missing here (a model, MCP servers, secrets to fill in)."""
    files = _read(blob)
    manifest = _json(files, "manifest.json")
    if manifest.get("format") != FORMAT:
        raise PackageError(f"unsupported package format: {manifest.get('format')}")
    try:
        agent = Agent(**_json(files, "agent.json"))
    except PackageError:
        raise
    except Exception as e:
        raise PackageError(f"agent.json is not a valid agent: {e}") from None
    agent.live = False

    task_list = []
    for name in sorted(n for n in files if n.startswith("tasks/")):
        try:
            task = Task(**_json(files, name))
        except PackageError:
            raise
        except Exception as e:
            raise PackageError(f"{name} is not a valid task: {e}") from None
        if task.agent_id != agent.id:
            raise PackageError(f"{name} belongs to another agent ({task.agent_id})")
        task_list.append(task)

    tools_root = Path(registry.tools_dir)
    tool_files: dict[str, dict[str, tuple[bytes, int]]] = {}  # tools/<path> -> files
    for name, content in files.items():
        if not name.startswith("tools/"):
            continue
        parts = PurePosixPath(name).parts[1:]
        # tools/<id>/... or tools/<group>/<id>/...: the folder is the one that
        # holds tool.json, found once every file has been seen.
        tool_files.setdefault("/".join(parts[:-1]), {})[parts[-1]] = content
    folders = {}
    for folder in [f for f in tool_files if "tool.json" in tool_files[f]]:
        parts = folder.split("/")
        if len(parts) > 2 or not all(is_valid_id(p) for p in parts):
            raise PackageError(f"not a tool folder: tools/{folder}")
        folders[folder] = parts[-1]

    # Every conflict first: without overwrite, nothing is written.
    conflicts = []
    if stores.agents.get(agent.id) is not None:
        conflicts.append(f"agent {agent.id}")
    conflicts += [f"task {t.id}" for t in task_list if tasks.get(t.id) is not None]
    conflicts += [f"tool {tid}" for tid in folders.values()
                  if registry.user_dir(tid) is not None]
    if conflicts and not overwrite:
        raise Conflict(conflicts)

    for folder, tool_id in folders.items():
        target = tools_root / folder
        # Replaced, not merged: a file the new version dropped must not linger.
        existing = registry.user_dir(tool_id)
        if existing is not None:
            shutil.rmtree(existing)
        for name, (data, mode) in files.items():
            prefix = f"tools/{folder}/"
            if not name.startswith(prefix):
                continue
            path = target / name[len(prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if mode & 0o100 else 0o644)
    registry.mark_dirty()

    stores.agents.save(agent.id, agent.model_dump())
    for task in task_list:
        tasks.save(task)

    missing_settings = {}
    for name in [n for n in files if n.startswith("tool_settings/")]:
        tool_id = PurePosixPath(name).stem
        meta = registry.get_definition(tool_id)
        if meta is None:
            continue
        current = tool_config.values(tool_id, meta)
        incoming = {k: str(v) for k, v in _json(files, name).items() if k in current}
        # What is configured HERE wins: the package fills gaps, it does not
        # repoint a tool that already works on this machine.
        tool_config.save(tool_id, meta, {k: v for k, v in incoming.items() if not current[k]})
    for tool_id in registry.expand_tool_ids(agent.tools):
        meta = registry.get_definition(tool_id) or {}
        empty = [f["key"] for f in tool_config.fields(meta)
                 if f["secret"] and not tool_config.values(tool_id, meta)[f["key"]]]
        if empty:
            missing_settings[tool_id] = empty

    model_ok = (agent.model_id in ("", "default")
                or stores.models.get(agent.model_id) is not None)
    return {
        "agent_id": agent.id,
        "tools": sorted(folders.values()),
        "tasks": [t.id for t in task_list],
        "overwritten": conflicts,
        "missing_model": "" if model_ok else agent.model_id,
        "external_tools": manifest.get("external_tools", []),
        "missing_settings": missing_settings,
    }
