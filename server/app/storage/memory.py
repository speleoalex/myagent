"""Per-agent long-term memory: one hand-editable memory.md plus flat Markdown chunks.

Layout (one directory per agent under ``config.MEMORY_DIR``):

    <memory>/<agent_id>/
    ├── memory.md                    # injected whole into the prompt (small!)
    └── chunks/
        └── 260730143012.md          # one archived item (summary or note)

``memory.md`` has three parts, split by two load-bearing markers:

    # Memory — <agent_id>
    ## Profile        <- everything BEFORE "## Notes" is user prose, never touched
    ## Notes          <- index lines for explicit memory_note facts (cap NOTES_CAP)
    ## Recent         <- index lines for auto-archived conversation summaries (cap INDEX_CAP)

Index lines look like ``- 260730143012 — one-line summary``; anything after a
marker that doesn't match that shape is preserved but not counted (tolerant to
hand editing). Evicted lines disappear from memory.md only — the chunk files
stay on disk and remain reachable through ``search``. Notes get their own
section and a higher cap because explicit "remember this" facts outrank
automatic summaries.

A chunk file is frontmatter + body. The body is ONLY the summary (or the full
note text): full transcripts already live in the sessions store, the single
ground truth — memory never duplicates them.

This module is STORAGE ONLY — no LLM calls (summaries are produced by
:mod:`app.engine.memory_compactor`) — so it is fully testable on a temp dir.

Concurrency contract: MUTATING calls (``write_chunk``, ``add_to_index``,
``add_note``) must run while holding ``lock(agent_id)`` and only do file I/O
(never LLM work: milliseconds, not minutes). Pure reads are safe lock-free:
writes are atomic replaces, so readers see the old or the new file, never a
torn one.

Crash safety: a chunk file is always written BEFORE memory.md references it.
A chunk missing from the index is still found by ``search`` (which scans the
files, not the index), and the compactor's idempotent retry — keyed on the
content ``hash`` in the frontmatter — repairs the index without re-paying the
LLM. ``add_to_index`` is a no-op when the id is already listed.

Legacy: the previous tree store (``tree.json`` + ``chunks/c-*.json``) is
simply ignored — all globs here are ``*.md``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from app.storage.sessions import now_iso

log = logging.getLogger(__name__)

# Section markers the code owns inside memory.md (exact stripped lines).
NOTES_HEADER = "## Notes"
RECENT_HEADER = "## Recent"
# Index caps: explicit notes outlive conversation summaries in the prompt.
NOTES_CAP = 20
INDEX_CAP = 10
# Defensive cap on what get_memory_md hands to the prompt builder.
MD_MAX_CHARS = 4000
# One index line: "- <id> — <summary>".
_INDEX_LINE = re.compile(r"^- (\d{12}(?:-\d+)?) — (.*)$")
_VALID_CHUNK_ID = re.compile(r"^\d{12}(-\d+)?$")
_HTML_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)
_MAX_KEYWORDS = 12
_LINE_SUMMARY_CHARS = 120


def _template(agent_id: str) -> str:
    return (
        f"# Memory — {agent_id}\n\n"
        "## Profile\n\n"
        "<!-- Durable facts about the user and standing instructions, edited by\n"
        "hand. This whole file is injected into the agent's prompt every turn:\n"
        "keep it short. HTML comments like this one are stripped on injection. -->\n\n"
        f"{NOTES_HEADER}\n\n"
        f"{RECENT_HEADER}\n"
    )


def _line_summary(text: str) -> str:
    """First sentence of a summary/note, flattened to one short index line."""
    flat = " ".join((text or "").split())
    for sep in (". ", "! ", "? "):
        idx = flat.find(sep)
        if 0 < idx < _LINE_SUMMARY_CHARS:
            return flat[: idx + 1]
    if len(flat) > _LINE_SUMMARY_CHARS:
        flat = flat[:_LINE_SUMMARY_CHARS].rstrip() + "…"
    return flat


def _parse_front(text: str) -> tuple[dict, str]:
    """Hand-rolled frontmatter: '---' + 'key: value' lines + '---' + body.
    No YAML lib on purpose (requirements stay small)."""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.split("\n")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return {}, text.strip()  # unterminated frontmatter: treat all as body


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class MemoryStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        # One asyncio.Lock per agent (same pattern as NamedSessionStore._locks):
        # serializes memory writes for the SAME agent without blocking others.
        self._locks: dict[str, asyncio.Lock] = {}
        # memory.md prompt-view cache keyed by agent_id, invalidated by mtime.
        self._md_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------ paths
    def _agent_dir(self, agent_id: str) -> Path:
        return self.base / agent_id

    def _md_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "memory.md"

    def _chunks_dir(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "chunks"

    def _chunk_path(self, agent_id: str, chunk_id: str) -> Path:
        return self._chunks_dir(agent_id) / f"{chunk_id}.md"

    # ------------------------------------------------------------------ locks
    def lock(self, agent_id: str) -> asyncio.Lock:
        lk = self._locks.get(agent_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[agent_id] = lk
        return lk

    # ------------------------------------------------------------------ reads
    def get_memory_md(self, agent_id: str) -> str:
        """Prompt-ready view of memory.md ("" when absent). Sync + mtime-cached:
        safe in the prompt-build hot path and in the autonomous wake prompt.
        Strips the H1 title and HTML comments (free hints for hand editors)."""
        path = self._md_path(agent_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cached = self._md_cache.get(agent_id)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        text = _HTML_COMMENT.sub("", text)
        lines = text.splitlines()
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        out = "\n".join(lines).strip()
        if len(out) > MD_MAX_CHARS:
            log.warning("memory.md of agent '%s' exceeds %d chars — truncated "
                        "in the prompt, trim the file", agent_id, MD_MAX_CHARS)
            out = out[:MD_MAX_CHARS].rstrip() + "\n[… memory.md truncated]"
        self._md_cache[agent_id] = (mtime, out)
        return out

    def read_chunk(self, agent_id: str, chunk_id: str) -> dict | None:
        """{"id", "meta": {frontmatter}, "body": str} or None. The id regex
        doubles as the path-traversal guard."""
        if not _VALID_CHUNK_ID.match(chunk_id or ""):
            return None
        try:
            text = self._chunk_path(agent_id, chunk_id).read_text(encoding="utf-8")
        except OSError:
            return None
        meta, body = _parse_front(text)
        return {"id": chunk_id, "meta": meta, "body": body}

    def _iter_chunks(self, agent_id: str, newest_first: bool = True):
        """Yield (chunk_id, meta, body) for every chunk file."""
        chunks_dir = self._chunks_dir(agent_id)
        if not chunks_dir.is_dir():
            return
        files = sorted(chunks_dir.glob("*.md"), reverse=newest_first)
        for f in files:
            if not _VALID_CHUNK_ID.match(f.stem):
                continue
            try:
                meta, body = _parse_front(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            yield f.stem, meta, body

    def find_chunk_by_hash(self, agent_id: str, session_id: str, hash_: str) -> str | None:
        """Id of an already-archived chunk with this content hash — the
        idempotent-retry key after a crash between the chunk write and the
        session splice. Chunks are small (summaries only), the scan is cheap."""
        if not hash_:
            return None
        for cid, meta, _body in self._iter_chunks(agent_id):
            if meta.get("hash") == hash_ and (
                not session_id or meta.get("session", "") == session_id
            ):
                return cid
        return None

    def search(self, agent_id: str, query: str, max_results: int = 5) -> list[dict]:
        """Case-insensitive token match over each chunk's frontmatter + body.
        Score = distinct tokens found; notes rank above conversation summaries
        on equal score ("remember this" outranks automatic archiving), then
        newest first. Returns [{id, date, kind, summary, score}]."""
        tokens = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1]
        if not tokens:
            return []
        results: list[dict] = []
        for cid, meta, body in self._iter_chunks(agent_id):
            text = (body + " " + meta.get("keywords", "")
                    + " " + meta.get("session", "")).lower()
            score = sum(1 for t in tokens if t in text)
            if score:
                results.append({
                    "id": cid,
                    "date": meta.get("date", ""),
                    "kind": meta.get("kind", "conversation"),
                    "summary": _line_summary(body),
                    "score": score,
                })
        # Three stable sorts, least significant first: recency, note-priority,
        # score. _iter_chunks already yields newest first.
        results.sort(key=lambda r: 0 if r["kind"] == "note" else 1)
        results.sort(key=lambda r: -r["score"])
        return results[:max_results]

    def list_recent(self, agent_id: str, n: int = 5) -> list[dict]:
        """Newest chunks (any kind) — the fallback for vague searches."""
        out = []
        for cid, meta, body in self._iter_chunks(agent_id):
            out.append({
                "id": cid,
                "date": meta.get("date", ""),
                "kind": meta.get("kind", "conversation"),
                "summary": _line_summary(body),
            })
            if len(out) >= n:
                break
        return out

    # ----------------------------------------------------------------- writes
    def write_chunk(self, agent_id: str, *, body: str, kind: str = "conversation",
                    session_id: str = "", channel: str = "", source: str = "",
                    keywords: list[str] | None = None, hash_: str = "") -> str:
        """Durably store one chunk file, return its id. Caller must hold
        lock(agent_id). Ids are local timestamps (sortable, human-readable);
        a same-second collision gets a -2/-3 suffix."""
        chunks_dir = self._chunks_dir(agent_id)
        chunks_dir.mkdir(parents=True, exist_ok=True)
        base = datetime.now().strftime("%y%m%d%H%M%S")
        chunk_id, n = base, 1
        while self._chunk_path(agent_id, chunk_id).exists():
            n += 1
            chunk_id = f"{base}-{n}"
        front = [
            "---",
            f"id: {chunk_id}",
            f"kind: {kind}",
            f"date: {now_iso()}",
            f"session: {session_id}",
            f"channel: {channel}",
            f"source: {source}",
            f"hash: {hash_}",
            f"keywords: {', '.join(list(keywords or [])[:_MAX_KEYWORDS])}",
            "---",
        ]
        _write_text_atomic(self._chunk_path(agent_id, chunk_id),
                           "\n".join(front) + "\n" + (body or "").strip() + "\n")
        return chunk_id

    def add_to_index(self, agent_id: str, chunk_id: str, line_summary: str,
                     kind: str = "conversation") -> None:
        """Add one line to memory.md's index (## Notes for notes, ## Recent
        otherwise), newest on top, capped per section. User prose before the
        markers is preserved byte-for-byte; non-index lines inside a section
        are preserved in place. Idempotent: an id already listed is a no-op
        (the crash-retry path re-runs this). Caller must hold lock(agent_id)."""
        target = NOTES_HEADER if kind == "note" else RECENT_HEADER
        cap = NOTES_CAP if kind == "note" else INDEX_CAP
        path = self._md_path(agent_id)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if not text.strip():
            text = _template(agent_id)
        lines = text.splitlines()
        for marker in (NOTES_HEADER, RECENT_HEADER):
            if not any(l.strip() == marker for l in lines):
                if lines and lines[-1].strip():
                    lines.append("")
                lines += [marker, ""]

        out: list[str] = []
        section = None
        kept = 0  # conforming entries kept in the target section
        added = False
        for line in lines:
            stripped = line.strip()
            if stripped in (NOTES_HEADER, RECENT_HEADER):
                section = stripped
                out.append(line)
                if section == target:
                    out.append(f"- {chunk_id} — {_line_summary(line_summary)}")
                    added = True
                continue
            m = _INDEX_LINE.match(stripped)
            if section == target and m:
                if m.group(1) == chunk_id:
                    return  # already indexed: idempotent no-op, don't rewrite
                kept += 1
                if kept >= cap:  # new entry took one slot: evict the oldest
                    continue
            out.append(line)
        if not added:  # defensive: markers were just ensured above
            out += [target, f"- {chunk_id} — {_line_summary(line_summary)}"]
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(path, "\n".join(out).rstrip() + "\n")
        self._md_cache.pop(agent_id, None)

    def add_note(self, agent_id: str, text: str, keywords: list[str] | None = None) -> str:
        """Store an explicit fact (memory_note): full text in the chunk, index
        line in ## Notes — immediately visible in the injected memory.md, no
        LLM involved. Caller must hold the lock."""
        body = (text or "").strip()
        chunk_id = self.write_chunk(
            agent_id, body=body, kind="note", source="tool:memory_note",
            keywords=keywords, hash_=hashlib.sha1(body.encode()).hexdigest(),
        )
        self.add_to_index(agent_id, chunk_id, body, kind="note")
        return chunk_id

    # ------------------------------------------------------------------ utils
    @staticmethod
    def hash_messages(messages: list[dict]) -> str:
        """Content hash of a cleaned message list — the idempotency key that
        makes a post-crash compaction retry find its already-archived chunk."""
        payload = json.dumps(
            [(m.get("role", ""), m.get("content", "")) for m in messages],
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()
