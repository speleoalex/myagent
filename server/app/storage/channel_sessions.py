"""Channel-scoped chat sessions.

The web UI works with a single active chat (``current.json``) archived into
``history/`` — see :mod:`app.storage.sessions`. External connectors (Telegram
and other messaging bridges) are instead multi-conversation: every external
chat is an independent, long-lived conversation addressed by a stable key
(e.g. ``telegram_supportbot_12345``).

This store keeps those conversations in their own namespace
(``sessions/channels/<id>.json``) so they never mix with the web UI's
``current.json`` / history flow (``list_history()`` only globs ``history/``).

Channel sessions use the SAME on-disk format as web sessions (the shared
``new_session`` factory in :mod:`app.storage.sessions`), plus two provenance
fields: ``channel`` (the stable external key) and ``source`` (the connector
type, e.g. ``telegram``). When a channel chat is reset it is archived into the
regular web history (``SessionStore.archive_session``), where those fields let
the UI show where the chat came from.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.storage.sessions import new_session, now_iso, read_json, write_json


class NamedSessionStore:
    def __init__(self, base_dir: Path):
        # base_dir is the shared sessions root; channel sessions live in a
        # dedicated subdirectory so the web UI's history listing never sees them.
        self.base = Path(base_dir) / "channels"
        self.base.mkdir(parents=True, exist_ok=True)
        # One lock per session id: serializes concurrent turns for the SAME
        # external chat (a user hammering the bot) without blocking other chats.
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, session_id: str) -> Path:
        return self.base / f"{session_id}.json"

    def lock(self, session_id: str) -> asyncio.Lock:
        lk = self._locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[session_id] = lk
        return lk

    # --------------------------------------------------------------- sessions
    def get(self, session_id: str, agent_id: str = "") -> dict:
        """Load a channel session, creating an empty one if it doesn't exist."""
        s = read_json(self._path(session_id))
        if s is not None:
            return s
        return new_session(session_id, agent_id, channel=session_id, source="")

    def save(self, session_id: str, session: dict) -> dict:
        session["updated_at"] = now_iso()
        write_json(self._path(session_id), session)
        return session

    def list_summaries(self, prefix: str = "") -> list[dict]:
        """Summaries of (a subset of) channel sessions, same shape as
        SessionStore.list_history() — used to surface the autonomous sessions
        (``autonomous_*``) in the web UI's session list."""
        out = []
        for f in self.base.glob(f"{prefix}*.json"):
            s = read_json(f)
            if s is None or not s.get("messages"):
                continue
            out.append({
                "id": s.get("id", f.stem),
                "title": s.get("title") or "(untitled)",
                "agent_id": s.get("agent_id", ""),
                "created_at": s.get("created_at", ""),
                "updated_at": s.get("updated_at", ""),
                "message_count": len(s.get("messages", [])),
                "channel": s.get("channel", ""),
                "source": s.get("source", ""),
            })
        return out

    def delete(self, session_id: str) -> bool:
        p = self._path(session_id)
        if p.exists():
            p.unlink()
            self._locks.pop(session_id, None)
            return True
        return False
