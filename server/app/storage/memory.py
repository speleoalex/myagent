"""Per-agent deep memory: a summary tree over archived conversation chunks.

Layout (one directory per agent under ``config.MEMORY_DIR``):

    <memory>/<agent_id>/
    ├── tree.json          # root digest + ALL summary nodes (small, one read)
    └── chunks/
        └── c-000003.json  # archived raw chunk (large, read only on drill-down)

``tree.json`` holds the long-term root summary plus every summary node
(``s-NNNNNN``); chunk ids (``c-NNNNNN``) appear only as node children and live
in their own files. Ids come from a monotonic ``next_seq`` shared by both
kinds, so they are sortable and human-readable.

This module is STORAGE ONLY — no LLM calls (summaries are produced by
:mod:`app.engine.memory_compactor`) — so it is fully testable on a temp dir.

Concurrency contract: every MUTATING call (``archive_chunk``, ``add_note``,
``apply_fold``, ``set_root``, and ``load_tree(adopt=True)``) must run while
holding ``lock(agent_id)``. Pure reads (``get_root_summary``, ``search``,
``read_node``) are safe lock-free: writes are atomic replaces, so readers see
either the old or the new file, never a torn one.

Crash safety: a chunk file is always written BEFORE the tree references it.
A crash in between leaves an orphan chunk, which ``load_tree(adopt=True)``
adopts on the next locked operation using the chunk's embedded summary (and
bumps ``next_seq`` past it, so its id can never be re-allocated to new data).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from app.storage.sessions import now_iso, read_json, write_json

_VALID_NODE_ID = re.compile(r"^[cs]-\d{6,}$")

# Cap on keywords carried by a node (folds union their children's keywords).
_MAX_KEYWORDS = 12


def _empty_tree(agent_id: str) -> dict:
    return {
        "version": 1,
        "agent_id": agent_id,
        "next_seq": 1,
        "updated_at": now_iso(),
        "root": {"summary": "", "updated_at": ""},
        "nodes": {},
    }


def _tokens_est(text: str) -> int:
    """Same ~4 chars/token heuristic as LLMProvider._estimate_tokens, inlined
    so the storage layer stays free of engine imports."""
    return len(text or "") // 4


def _chunk_text(chunk: dict) -> str:
    """All searchable text of a chunk (note text or flattened messages)."""
    if chunk.get("kind") == "note":
        return chunk.get("text") or ""
    parts = []
    for m in chunk.get("messages", []):
        c = m.get("content")
        if isinstance(c, str) and c:
            parts.append(c)
    return "\n".join(parts)


class MemoryStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        # One asyncio.Lock per agent (same pattern as NamedSessionStore._locks):
        # serializes compactions and memory-tool writes for the SAME agent
        # without blocking other agents.
        self._locks: dict[str, asyncio.Lock] = {}
        # tree.json cache keyed by agent_id, invalidated by mtime.
        self._trees: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ paths
    def _agent_dir(self, agent_id: str) -> Path:
        return self.base / agent_id

    def _tree_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "tree.json"

    def _chunks_dir(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "chunks"

    def _chunk_path(self, agent_id: str, chunk_id: str) -> Path:
        return self._chunks_dir(agent_id) / f"{chunk_id}.json"

    # ------------------------------------------------------------------ locks
    def lock(self, agent_id: str) -> asyncio.Lock:
        lk = self._locks.get(agent_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[agent_id] = lk
        return lk

    # ------------------------------------------------------------------- tree
    def load_tree(self, agent_id: str, adopt: bool = False) -> dict:
        """Load (and cache by mtime) the agent's tree.

        With ``adopt=True`` (callers MUST hold the lock) it also self-heals:
        orphan chunk files — written by a run that crashed before updating the
        tree — are adopted as level-1 nodes using their embedded summary, and
        ``next_seq`` is bumped past every id seen on disk so no orphan id can
        be re-allocated.
        """
        path = self._tree_path(agent_id)
        tree: dict | None = None
        try:
            mtime = path.stat().st_mtime
            cached = self._trees.get(agent_id)
            if cached and cached[0] == mtime:
                tree = cached[1]
            else:
                tree = read_json(path)
                if tree is not None:
                    self._trees[agent_id] = (mtime, tree)
        except OSError:
            pass
        if tree is None:
            tree = _empty_tree(agent_id)
        if adopt:
            tree = self._adopt_orphans(agent_id, tree)
        return tree

    def _write_tree(self, agent_id: str, tree: dict) -> None:
        tree["updated_at"] = now_iso()
        path = self._tree_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, tree)
        try:
            self._trees[agent_id] = (path.stat().st_mtime, tree)
        except OSError:
            self._trees.pop(agent_id, None)

    def _adopt_orphans(self, agent_id: str, tree: dict) -> dict:
        """Adopt chunk files the tree doesn't reference (crash between the
        chunk write and the tree write). Mutates + persists only when needed."""
        chunks_dir = self._chunks_dir(agent_id)
        if not chunks_dir.is_dir():
            return tree
        referenced: set[str] = set()
        max_seq = tree.get("next_seq", 1) - 1
        for node in tree.get("nodes", {}).values():
            referenced.update(node.get("children", []))
        for nid in tree.get("nodes", {}):
            max_seq = max(max_seq, self._seq_of(nid))
        changed = False
        for f in sorted(chunks_dir.glob("c-*.json")):
            cid = f.stem
            max_seq = max(max_seq, self._seq_of(cid))
            if cid in referenced:
                continue
            chunk = read_json(f)
            if chunk is None:
                continue
            changed = True
            seq = max_seq + 1
            max_seq = seq
            nid = f"s-{seq:06d}"
            tree["nodes"][nid] = self._node_for_chunk(nid, cid, chunk)
        if max_seq + 1 > tree.get("next_seq", 1):
            tree["next_seq"] = max_seq + 1
            changed = True
        if changed:
            self._write_tree(agent_id, tree)
        return tree

    @staticmethod
    def _seq_of(node_id: str) -> int:
        try:
            return int(node_id.split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    @staticmethod
    def _node_for_chunk(node_id: str, chunk_id: str, chunk: dict) -> dict:
        ts = chunk.get("created_at") or now_iso()
        return {
            "id": node_id,
            "level": 1,
            "parent": None,
            "children": [chunk_id],
            "summary": chunk.get("summary", ""),
            "keywords": list(chunk.get("keywords") or [])[:_MAX_KEYWORDS],
            "sessions": [s for s in [chunk.get("session_id")] if s],
            "channels": [c for c in [chunk.get("channel")] if c],
            "time_range": [ts, ts],
            "hash": chunk.get("hash", ""),
            "created_at": ts,
            "tokens_est": _tokens_est(_chunk_text(chunk)),
        }

    # ------------------------------------------------------------------ reads
    def get_root_summary(self, agent_id: str) -> str:
        """The long-term digest ("" when memory is empty). Sync + mtime-cached:
        safe in the prompt-build hot path, and the hook the always-on wake
        prompt will use without any chat session."""
        return (self.load_tree(agent_id).get("root") or {}).get("summary", "") or ""

    def find_chunk_by_hash(self, agent_id: str, session_id: str, hash_: str) -> str | None:
        """Node id of an already-archived chunk with this content hash (the
        idempotent-retry path after a crash between the tree write and the
        session splice). Callers should have adopted orphans first."""
        if not hash_:
            return None
        for nid, node in self.load_tree(agent_id).get("nodes", {}).items():
            if node.get("hash") == hash_ and (
                not session_id or session_id in node.get("sessions", [])
            ):
                return nid
        return None

    def read_node(self, agent_id: str, node_id: str) -> dict | None:
        """Structured view of one node for drill-down.

        Summary node -> {"type": "summary", "node": ..., "children": [{id,
        kind, summary, created_at}]}; chunk -> {"type": "chunk", "chunk": ...}.
        """
        if not _VALID_NODE_ID.match(node_id or ""):
            return None
        if node_id.startswith("c-"):
            chunk = read_json(self._chunk_path(agent_id, node_id))
            return {"type": "chunk", "chunk": chunk} if chunk else None
        tree = self.load_tree(agent_id)
        node = tree.get("nodes", {}).get(node_id)
        if node is None:
            return None
        children = []
        for cid in node.get("children", []):
            if cid.startswith("s-"):
                child = tree["nodes"].get(cid)
                if child:
                    children.append({
                        "id": cid, "kind": "summary",
                        "summary": child.get("summary", ""),
                        "created_at": child.get("created_at", ""),
                    })
            else:
                chunk = read_json(self._chunk_path(agent_id, cid))
                if chunk:
                    children.append({
                        "id": cid, "kind": chunk.get("kind", "conversation"),
                        "summary": chunk.get("summary", ""),
                        "created_at": chunk.get("created_at", ""),
                    })
        return {"type": "summary", "node": node, "children": children}

    def search(self, agent_id: str, query: str, max_results: int = 5,
               deep: bool = False) -> list[dict]:
        """Case-insensitive token match over every node's summary+keywords
        (score = matched tokens, recency tiebreak). ``deep`` also greps the
        chunk files' raw text. Returns [{id, created_at, summary, score}]."""
        tokens = [t for t in re.findall(r"\w+", (query or "").lower()) if len(t) > 1]
        if not tokens:
            return []
        results: list[tuple[int, str, dict]] = []
        tree = self.load_tree(agent_id)
        for nid, node in tree.get("nodes", {}).items():
            text = (node.get("summary", "") + " " + " ".join(node.get("keywords", []))).lower()
            score = sum(1 for t in tokens if t in text)
            if score:
                results.append((score, node.get("created_at", ""), {
                    "id": nid,
                    "created_at": node.get("created_at", ""),
                    "summary": node.get("summary", ""),
                    "score": score,
                }))
        if deep:
            chunks_dir = self._chunks_dir(agent_id)
            if chunks_dir.is_dir():
                for f in sorted(chunks_dir.glob("c-*.json")):
                    chunk = read_json(f)
                    if chunk is None:
                        continue
                    text = _chunk_text(chunk).lower()
                    score = sum(1 for t in tokens if t in text)
                    if score:
                        results.append((score, chunk.get("created_at", ""), {
                            "id": chunk.get("id", f.stem),
                            "created_at": chunk.get("created_at", ""),
                            "summary": chunk.get("summary", ""),
                            "score": score,
                        }))
        # Best score first; equal scores newest first (two stable sorts).
        results.sort(key=lambda r: r[1], reverse=True)
        results.sort(key=lambda r: -r[0])
        return [r[2] for r in results[:max_results]]

    def orphans_at_level(self, agent_id: str, level: int) -> list[dict]:
        """Parentless nodes at a level, oldest first (fold candidates)."""
        tree = self.load_tree(agent_id)
        out = [n for n in tree.get("nodes", {}).values()
               if n.get("level") == level and not n.get("parent")]
        out.sort(key=lambda n: n.get("id", ""))
        return out

    # ---------------------------------------------------------------- writes
    def archive_chunk(self, agent_id: str, messages: list[dict], *,
                      summary: str, keywords: list[str] | None = None,
                      session_id: str = "", channel: str = "", source: str = "",
                      kind: str = "conversation", text: str = "") -> tuple[str, dict]:
        """Durably archive one chunk and its level-1 summary node.

        Write order is the crash-safety contract: chunk file FIRST, tree AFTER.
        Caller must hold lock(agent_id). Returns (node_id, node).
        """
        tree = self.load_tree(agent_id, adopt=True)
        seq = tree.get("next_seq", 1)
        chunk_id = f"c-{seq:06d}"
        node_id = f"s-{seq + 1:06d}"
        chunk = {
            "id": chunk_id,
            "kind": kind,
            "agent_id": agent_id,
            "session_id": session_id,
            "channel": channel,
            "source": source,
            "created_at": now_iso(),
            "hash": self.hash_messages(messages) if messages else
                    hashlib.sha1((text or "").encode()).hexdigest(),
            "summary": summary,
            "keywords": list(keywords or [])[:_MAX_KEYWORDS],
            "messages": messages,
        }
        if kind == "note":
            chunk["text"] = text
        self._chunks_dir(agent_id).mkdir(parents=True, exist_ok=True)
        write_json(self._chunk_path(agent_id, chunk_id), chunk)

        node = self._node_for_chunk(node_id, chunk_id, chunk)
        tree["nodes"][node_id] = node
        tree["next_seq"] = seq + 2
        self._write_tree(agent_id, tree)
        return node_id, node

    def add_note(self, agent_id: str, text: str, keywords: list[str] | None = None) -> str:
        """Store a standalone fact (memory_note). Caller must hold the lock."""
        summary = (text or "").strip()[:1000]
        node_id, _ = self.archive_chunk(
            agent_id, [], summary=summary, keywords=keywords,
            source="tool:memory_note", kind="note", text=(text or "").strip(),
        )
        return node_id

    def apply_fold(self, agent_id: str, child_ids: list[str], summary: str,
                   level: int) -> str:
        """Group summary nodes under a new level-N parent (cascading
        compaction). Caller must hold the lock. Returns the new node id."""
        tree = self.load_tree(agent_id, adopt=True)
        nodes = tree.get("nodes", {})
        children = [nodes[c] for c in child_ids if c in nodes]
        if not children:
            raise ValueError("apply_fold: no valid children")
        seq = tree.get("next_seq", 1)
        node_id = f"s-{seq:06d}"
        keywords: list[str] = []
        sessions: list[str] = []
        channels: list[str] = []
        times: list[str] = []
        for c in children:
            keywords.extend(k for k in c.get("keywords", []) if k not in keywords)
            sessions.extend(s for s in c.get("sessions", []) if s not in sessions)
            channels.extend(ch for ch in c.get("channels", []) if ch not in channels)
            times.extend(t for t in c.get("time_range", []) if t)
            c["parent"] = node_id
        nodes[node_id] = {
            "id": node_id,
            "level": level,
            "parent": None,
            "children": [c["id"] for c in children],
            "summary": summary,
            "keywords": keywords[:_MAX_KEYWORDS],
            "sessions": sessions,
            "channels": channels,
            "time_range": [min(times), max(times)] if times else [now_iso(), now_iso()],
            "hash": "",
            "created_at": now_iso(),
            "tokens_est": _tokens_est(summary),
        }
        tree["next_seq"] = seq + 1
        self._write_tree(agent_id, tree)
        return node_id

    def set_root(self, agent_id: str, summary: str) -> None:
        """Replace the long-term root digest. Caller must hold the lock."""
        tree = self.load_tree(agent_id, adopt=True)
        tree["root"] = {"summary": summary, "updated_at": now_iso()}
        self._write_tree(agent_id, tree)

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
