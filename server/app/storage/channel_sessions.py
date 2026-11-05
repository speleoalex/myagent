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
regular web history (via the ``archive`` store handed to the constructor),
where those fields let the UI show where the chat came from.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from pathlib import Path

from app import config
from app.ids import is_valid_id
from app.storage.sessions import (new_session, now_iso, read_json,
                                  session_summary, write_json)

#: The channel sessions whose lock the CURRENT turn holds, as
#: ``(id(store), session_id)``. A ContextVar because "current turn" is a task
#: context: the tool calls, the sub-agents and the connector code a turn runs
#: are all tasks spawned inside it, and they inherit the value; an unrelated
#: turn running concurrently does not see it.
#:
#: It exists because the session lock is an ``asyncio.Lock``, which is not
#: re-entrant. ``notify_user`` appends what it sent to the recipient's session
#: under that lock — and when the recipient is the very person the turn is
#: answering, the turn already holds it. Observed 2026-11-05 on a mail bot:
#: asked a question by mail, the agent answered with notify_user (the mail
#: went out), then waited on its own lock until the 180 s turn timeout — so the
#: person got the answer, then an error mail, and the turn was never recorded,
#: which left the next question without context.
_HELD: ContextVar[frozenset] = ContextVar("myagent_held_channel_sessions",
                                          default=frozenset())


@contextlib.contextmanager
def holding(store, session_id: str):
    """Mark *session_id* as held by the code running inside this block.

    To be entered while ``store.lock(session_id)`` is held, around the agent
    turn. Module-level (not a store method) so the test fakes of the store,
    which only implement ``lock``, keep working."""
    token = _HELD.set(_HELD.get() | {(id(store), session_id)})
    try:
        yield
    finally:
        _HELD.reset(token)


def held_by_caller(store, session_id: str) -> bool:
    """True when the caller runs inside a turn that holds this session's lock:
    taking it again would wait for the turn to end, i.e. forever."""
    return (id(store), session_id) in _HELD.get()


class NamedSessionStore:
    def __init__(self, base_dir: Path, archive=None):
        # base_dir is the shared sessions root; channel sessions live in a
        # dedicated subdirectory so the web UI's history listing never sees them.
        self.base = Path(base_dir) / "channels"
        self.base.mkdir(parents=True, exist_ok=True)
        # The web SessionStore that receives rotated-out logs and reset
        # conversations (see save_rotating / archive_and_reset). Optional only
        # for constructions that never rotate (tests).
        self._archive = archive
        # One lock per session id: serializes concurrent turns for the SAME
        # external chat (a user hammering the bot) without blocking other chats.
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, session_id: str) -> Path:
        # Same guard as JsonStore: ids become filenames. Callers validate at
        # the API boundary; this raise is the storage layer's own backstop.
        if not is_valid_id(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        return self.base / f"{session_id}.json"

    def lock(self, session_id: str) -> asyncio.Lock:
        lk = self._locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[session_id] = lk
        return lk

    # --------------------------------------------------------------- sessions
    def exists(self, session_id: str) -> bool:
        try:
            return self._path(session_id).exists()
        except ValueError:
            return False

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

    def save_rotating(self, session_id: str, session: dict) -> dict:
        """Persist a channel session, rotating it once it outgrows
        ``config.CHANNEL_ROTATE_BYTES``: the recorded log is archived into the
        web history (provenance preserved) and the file restarts with the same
        compact LLM conversation, so the bot keeps its context while the file —
        which, unlike a web chat, is never closed by "new chat" — stays
        bounded. Every writer that APPENDS to a channel session must use this
        (blocking I/O: call via ``asyncio.to_thread`` from async code)."""
        self.save(session_id, session)
        if self._archive is None:
            return session
        if self._path(session_id).stat().st_size <= config.CHANNEL_ROTATE_BYTES:
            return session
        self._archive.archive_session(session)
        fresh = new_session(session_id, session.get("agent_id", ""),
                            channel=session.get("channel") or session_id,
                            source=session.get("source", ""))
        fresh["conversation"] = session.get("conversation", [])
        # The medium-short memory travels with the conversation: losing it on
        # rotation would drop the archived-turn summaries and the rewind offset.
        if session.get("memory"):
            fresh["memory"] = session["memory"]
        self.save(session_id, fresh)
        return fresh

    async def archive_and_reset(self, session_id: str) -> tuple[bool, str | None]:
        """Close a channel conversation: archive its log into the web history,
        then delete the live file — the shared flow behind a connector /reset
        and the UI's delete of an autonomous session. Serialized on the session
        lock so it can't race an in-flight turn. Returns ``(existed,
        archived_history_id)``."""
        archived = None
        existed = False
        async with self.lock(session_id):
            if self.exists(session_id):
                existed = True
                session = await asyncio.to_thread(self.get, session_id)
                if self._archive is not None:
                    archived = await asyncio.to_thread(
                        self._archive.archive_session, session)
            self.delete(session_id)
        return existed, archived

    def list_summaries(self, prefix: str = "") -> list[dict]:
        """Summaries of (a subset of) channel sessions, same shape as
        SessionStore.list_history() plus ``live: True`` — used to surface the
        autonomous and connector sessions in the web UI's session list.

        The extra flag is what lets the UI tell these apart from an ARCHIVED
        channel chat, which carries the same channel/source provenance but is a
        closed history file. A live one cannot be resumed (the connector keeps
        writing to it), so the UI must not offer the action."""
        out = []
        for f in self.base.glob(f"{prefix}*.json"):
            s = read_json(f)
            if s is None or not s.get("messages"):
                continue
            summary = session_summary(s, fallback_id=f.stem)
            summary["live"] = True
            out.append(summary)
        return out

    def delete(self, session_id: str) -> bool:
        try:
            p = self._path(session_id)
        except ValueError:
            return False
        if p.exists():
            p.unlink()
            self._locks.pop(session_id, None)
            return True
        return False
