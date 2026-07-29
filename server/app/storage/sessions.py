"""Disk-backed chat sessions.

One file per chat. The active chat is ``current.json``; starting a new chat
archives it into ``history/<id>.json``. Each session records everything about
the conversation: user messages (with attachments), tools called (name, args,
result preview), agents involved, and assistant replies — plus the compact
``conversation`` used to continue the chat with the LLM.

The module-level helpers (``now_iso``, ``read_json``, ``write_json``,
``new_session``) define the on-disk session format and are shared with the
channel-scoped store (:mod:`app.storage.channel_sessions`), so web and
connector chats stay format-compatible.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, session: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2))
    tmp.replace(path)  # atomic


def new_session(session_id: str, agent_id: str = "", **extra) -> dict:
    """A blank session in the canonical format. ``extra`` adds provenance
    fields (e.g. ``channel``/``source`` for connector chats)."""
    return {
        "id": session_id,
        "title": "",
        "agent_id": agent_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "messages": [],       # rich event log for display
        "conversation": [],   # compact history for the LLM
        **extra,
    }


class SessionStore:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.history_dir = self.base / "history"
        self.current_file = self.base / "current.json"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        # Summary cache for list_history(): sessions hold full recursive traces
        # and grow large, so re-parsing every file on each listing gets slow.
        # Keyed by path, invalidated by mtime (same pattern as ToolRegistry).
        self._summaries: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ utils
    def _gen_id(self) -> str:
        """A timestamped, sortable session id used as the on-disk file name
        (``history/<id>.json``). Second-granularity, so two chats created within
        the same second get a ``-2``, ``-3`` … suffix to avoid clobbering."""
        base = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        candidate, n = base, 2
        while (self.history_dir / f"{candidate}.json").exists():
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # ---------------------------------------------------------------- current
    def get_current(self) -> dict:
        if self.current_file.exists():
            s = read_json(self.current_file)
            if s is not None:
                return s
        s = new_session(self._gen_id())
        write_json(self.current_file, s)
        return s

    def save_current(self, session: dict) -> dict:
        session["updated_at"] = now_iso()
        write_json(self.current_file, session)
        return session

    def persist(self, session: dict) -> dict:
        """Write a session back to wherever it currently lives: current.json if
        it is still the active chat, otherwise history/<id>.json. Used by
        background generation, which may finish after the user has already
        started/opened a different chat (so current.json must not be clobbered)."""
        session["updated_at"] = now_iso()
        sid = session.get("id")
        cur = read_json(self.current_file) if self.current_file.exists() else None
        if cur is not None and cur.get("id") == sid:
            write_json(self.current_file, session)
        else:
            write_json(self.history_dir / f"{sid}.json", session)
        return session

    def archive_current(self) -> str | None:
        """Move the current chat into history (only if it has content)."""
        if not self.current_file.exists():
            return None
        s = read_json(self.current_file)
        if s is None or not s.get("messages"):
            self.current_file.unlink(missing_ok=True)  # drop empty/corrupt
            return None
        sid = s.get("id") or self._gen_id()
        s["id"] = sid
        write_json(self.history_dir / f"{sid}.json", s)
        self.current_file.unlink(missing_ok=True)
        return sid

    def archive_session(self, session: dict) -> str | None:
        """Archive an arbitrary session dict (e.g. an external channel session)
        into history under a fresh, unique id — so repeated resets of the same
        external chat don't clobber each other. Returns the new history id, or
        None if there's nothing worth keeping. The channel key is preserved in
        ``channel`` (with ``source``, when known) so the history list can show
        where the chat came from."""
        if not session.get("messages"):
            return None
        sid = self._gen_id()
        s = dict(session)
        s.setdefault("channel", session.get("id"))
        s["id"] = sid
        if not s.get("title"):
            # Legacy light-format sessions carry no title: fall back to the
            # first user message, then to the channel key.
            first = next((m.get("text", "") for m in s.get("messages", [])
                          if m.get("role") == "user" and m.get("text")), "")
            base = first.strip().replace("\n", " ")
            title = base[:60] + ("…" if len(base) > 60 else "")
            s["title"] = title or s.get("channel") or "(channel)"
        write_json(self.history_dir / f"{sid}.json", s)
        return sid

    def new_chat(self, agent_id: str = "") -> dict:
        """Archive the current chat and start a fresh empty one."""
        self.archive_current()
        s = new_session(self._gen_id(), agent_id)
        write_json(self.current_file, s)
        return s

    def resume(self, session_id: str) -> dict | None:
        """Reopen an archived chat as the active one (archiving the current
        chat first). The resumed session keeps its original agent_id, so the
        caller can restore the agent it was held with."""
        src = self.history_dir / f"{session_id}.json"
        if not src.exists():
            cur = self.get_current()
            return cur if cur.get("id") == session_id else None
        s = read_json(src)
        if s is None:
            return None
        self.archive_current()          # park whatever is active now
        write_json(self.current_file, s)
        src.unlink(missing_ok=True)     # it is now the current chat, not history
        return s

    # ---------------------------------------------------------------- history
    def list_history(self) -> list[dict]:
        out = []
        seen: set[str] = set()
        for f in self.history_dir.glob("*.json"):
            key = str(f)
            seen.add(key)
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            cached = self._summaries.get(key)
            if cached and cached[0] == mtime:
                out.append(cached[1])
                continue
            s = read_json(f)
            if s is None:
                continue
            summary = {
                "id": s.get("id", f.stem),
                "title": s.get("title") or "(untitled)",
                "agent_id": s.get("agent_id", ""),
                "created_at": s.get("created_at", ""),
                "updated_at": s.get("updated_at", ""),
                "message_count": len(s.get("messages", [])),
                # Provenance of archived connector chats ("channel_id" is the
                # legacy spelling written by older archives).
                "channel": s.get("channel") or s.get("channel_id") or "",
                "source": s.get("source", ""),
            }
            self._summaries[key] = (mtime, summary)
            out.append(summary)
        # Drop cache entries for deleted files
        for key in list(self._summaries):
            if key not in seen:
                del self._summaries[key]
        out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return out

    def get(self, session_id: str) -> dict | None:
        f = self.history_dir / f"{session_id}.json"
        if f.exists():
            return read_json(f)
        cur = self.get_current()
        return cur if cur.get("id") == session_id else None

    def delete(self, session_id: str) -> bool:
        f = self.history_dir / f"{session_id}.json"
        if f.exists():
            f.unlink()
            return True
        return False
