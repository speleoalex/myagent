"""Channel-scoped chat sessions.

The web UI works with a single active chat (``current.json``) archived into
``history/`` — see :mod:`app.storage.sessions`. External connectors (Telegram
and other messaging bridges) are instead multi-conversation: every external
chat is an independent, long-lived conversation addressed by a stable key
(e.g. ``telegram_supportbot_12345``).

This store keeps those conversations in their own namespace
(``sessions/channels/<id>.json``) so they never mix with the web UI's
``current.json`` / history flow (``list_history()`` only globs ``history/``).

Unlike the web sessions, channel sessions are kept deliberately light: we store
the compact ``conversation`` used to continue the chat with the LLM plus a
plain user/assistant text log — NOT the full recursive tool trace, which grows
large and is only useful to the web UI's inspector.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path


class NamedSessionStore:
    def __init__(self, base_dir: Path):
        # base_dir is the shared sessions root; channel sessions live in a
        # dedicated subdirectory so the web UI's history listing never sees them.
        self.base = Path(base_dir) / "channels"
        self.base.mkdir(parents=True, exist_ok=True)
        # One lock per session id: serializes concurrent turns for the SAME
        # external chat (a user hammering the bot) without blocking other chats.
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _path(self, session_id: str) -> Path:
        return self.base / f"{session_id}.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write(path: Path, session: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session, ensure_ascii=False, indent=2))
        tmp.replace(path)  # atomic

    def lock(self, session_id: str) -> asyncio.Lock:
        lk = self._locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[session_id] = lk
        return lk

    # --------------------------------------------------------------- sessions
    def get(self, session_id: str, agent_id: str = "") -> dict:
        """Load a channel session, creating an empty one if it doesn't exist."""
        s = self._read(self._path(session_id))
        if s is not None:
            return s
        return {
            "id": session_id,
            "agent_id": agent_id,
            "created_at": self._now(),
            "updated_at": self._now(),
            "messages": [],      # light user/assistant text log
            "conversation": [],  # compact history for the LLM
        }

    def save(self, session_id: str, session: dict) -> dict:
        session["updated_at"] = self._now()
        self._write(self._path(session_id), session)
        return session

    def delete(self, session_id: str) -> bool:
        p = self._path(session_id)
        if p.exists():
            p.unlink()
            self._locks.pop(session_id, None)
            return True
        return False
