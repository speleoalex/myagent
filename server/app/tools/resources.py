"""Tool -> chat resource channel: the ONE definition of the resource marker.

A tool that wants the user to SEE a file (an image, a generated HTML page, any
binary) writes the file under the workspace and prints a one-line marker on
stdout:

    [[resource:<workspace-relative-path>|<mime>|<title>]]

The marker never reaches the model or the user as-is:

- ``ToolRegistry._execute_external`` truncates long outputs with
  :func:`truncate_keep_markers` (the prose is cut, marker lines survive — the
  same rule ``mcp/result.py`` has always applied to its file notes);
- the executor calls :func:`extract` right after ``execute()`` returns: each
  marker becomes structured metadata on the trace step
  (``{path, mime, title, size}``) that rides the existing pipeline to the UI
  (SSE ``tool_result``, ``done`` trace, persisted session tool message), and is
  replaced in the model-facing string by a short prose note — so the bytes
  never enter the conversation.

Double square brackets on purpose: they cannot collide with the single-bracket
prose notes ``mcp/result.py`` emits (``[image saved: ...]``), and stay cheap to
match. The files themselves are served by ``GET /api/files/{path}``
(``app/routers/files.py``).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# One marker per line. Pipes are safe separators: paths and mimes come from our
# own store helpers ([A-Za-z0-9_./-]); marker() strips them from titles.
MARKER_RE = re.compile(
    r"^\[\[resource:([^|\]\n]+)\|([^|\]\n]*)\|([^\]\n]*)\]\][ \t]*$", re.M
)

# Defensive cap: a runaway tool must not turn one turn into a wall of files.
MAX_RESOURCES = 20


def marker(path: str, mime: str, title: str = "") -> str:
    """The marker line for *path* — the only sanctioned way to emit one from
    Python code (mcp/result.py, internal tools). Subprocess tools print the
    same shape by hand (documented in docs/TOOLS.md)."""
    clean = " ".join(str(title or "").split())
    clean = clean.replace("|", "/").replace("]]", ")")
    return f"[[resource:{path}|{mime or 'application/octet-stream'}|{clean}]]"


def extract(text: str, workspace: Path) -> tuple[str, list[dict]]:
    """Pull the resource markers out of a tool result.

    Returns ``(clean_text, resources)`` where every marker line has been
    replaced by a short model-facing note and ``resources`` is the structured
    list for the UI. A marker whose file is missing, escapes the workspace or
    exceeds :data:`MAX_RESOURCES` is dropped with a warning — the tool said
    something that isn't true, and a broken reference must not reach the UI.
    """
    if "[[resource:" not in (text or ""):
        return text, []

    root = workspace.resolve()
    resources: list[dict] = []
    seen: set[str] = set()

    def _replace(m: re.Match) -> str:
        path, mime, title = (g.strip() for g in m.groups())
        # Lexical guard first (the path comes from tool output, which can be
        # model-influenced), then realpath containment under the workspace.
        if not path or path.startswith("/") or ".." in path.split("/"):
            log.warning("resource marker rejected (bad path): %s", path)
            return ""
        try:
            target = (root / path).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise OSError("outside workspace or not a file")
            size = target.stat().st_size
        except OSError as e:
            log.warning("resource marker dropped (%s): %s", e, path)
            return ""
        if path not in seen:
            if len(resources) >= MAX_RESOURCES:
                log.warning("resource cap reached, dropping: %s", path)
                return ""
            seen.add(path)
            resources.append({
                "path": path,
                "mime": mime or "application/octet-stream",
                "title": title,
                "size": size,
            })
        return _note(path, mime, title)

    return MARKER_RE.sub(_replace, text).strip(), resources


def truncate_keep_markers(text: str, limit: int) -> str:
    """Truncate *text* to ~limit chars, but never lose a marker line: the
    markers are the only way the resource reaches the UI, and a tool that
    prints prose first and the marker last must still deliver its file."""
    if len(text) <= limit:
        return text
    all_markers = [m.group(0) for m in MARKER_RE.finditer(text)]
    head = text[:limit]
    # A marker cut in half at the boundary is garbage: drop the partial line
    # unconditionally — if it was complete it comes back via `missing`.
    tail_line = head.rsplit("\n", 1)[-1]
    if "[[resource:" in tail_line:
        head = head[: len(head) - len(tail_line)]
    kept = {m.group(0) for m in MARKER_RE.finditer(head)}
    out = head + "\n... [truncated]"
    missing = [mk for mk in dict.fromkeys(all_markers) if mk not in kept]
    if missing:
        out += "\n" + "\n".join(missing)
    return out


def _note(path: str, mime: str, title: str) -> str:
    """What the model reads in place of the marker: citable (title first),
    actionable (the path still works with document_extract), and explicit that
    the user has already received the file — or the model re-sends it in prose."""
    label = f'"{title}" ' if title else ""
    return (
        f"[file delivered to the user: {label}({path}, {mime}) — already "
        f"displayed in the chat; refer to it by name, and use document_extract "
        f"on the path only if you need to read its contents]"
    )
