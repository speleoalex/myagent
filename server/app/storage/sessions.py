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
import os
from datetime import datetime
from pathlib import Path

# Ids arriving from URL path segments become filenames here: same guard as
# JsonStore (ids.py is the single definition of the charset).
from app.ids import is_valid_id


def now_iso() -> str:
    """Timestamps in one canonical shape. The format is load-bearing, not
    cosmetic: sessions sort on ``updated_at`` and the event store compares
    ``due_at`` lexicographically — every producer must emit exactly this."""
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data: dict, mode: int | None = None) -> None:
    """Atomic JSON write (tmp + rename). ``mode`` sets restrictive permissions
    on the temp file BEFORE the rename, for files that may hold secrets."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    if mode is not None:
        os.chmod(tmp, mode)
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


# --------------------------------------------------------------- turn format
# How a chat turn is recorded INTO a session dict. These used to be private
# helpers of the chat router, which forced the autonomy engine to import from
# a FastAPI router (an inverted dependency); they live here because they are
# the other half of the on-disk format that new_session() starts.

def title_from(message: str, attachments=None) -> str:
    """A chat's title from its first user message (60 chars, single line)."""
    t = (message or "").strip().replace("\n", " ")
    if t:
        return t[:60] + ("…" if len(t) > 60 else "")
    return "(attachment)" if attachments else "New chat"


def memory_context(session: dict) -> list | None:
    """This session's archived-turn summaries (injected at prompt build)."""
    return (session.get("memory") or {}).get("context")


def record_user_turn(session: dict, message: str, attachments: list[dict],
                     agent_id: str) -> None:
    """Append the user turn to the session and set the title on first message."""
    session.setdefault("messages", []).append({
        "role": "user",
        "text": message,
        "attachments": attachments or [],
        "agent_id": agent_id,
        "ts": now_iso(),
    })
    session["agent_id"] = agent_id
    if not session.get("title"):
        session["title"] = title_from(message, attachments)


def tool_message_from_step(step: dict) -> dict:
    """Turn one executor trace step into a stored 'tool' message. Keeps the full
    result and — for call_agent — the nested sub_trace, so the archived session
    holds the complete recursive flow (sub-agent calls, results and tools)."""
    msg = {
        "role": "tool",
        "tool": step.get("tool"),
        "arguments": step.get("arguments"),
        "result_preview": step.get("result_preview"),
        "result": step.get("result"),
        "ts": step.get("ts") or now_iso(),
    }
    if step.get("resources"):
        msg["resources"] = step["resources"]
    if step.get("sub_trace"):
        msg["sub_trace"] = step["sub_trace"]
    return msg


def steps_from(trace, tool_events: list[dict] | None) -> list[dict]:
    """Prefer the rich recursive trace; fall back to flat tool summaries
    (older path / no trace available)."""
    if trace and trace.get("steps"):
        return trace["steps"]
    return [
        {
            "tool": t.get("tool"),
            "arguments": t.get("arguments"),
            "result_preview": t.get("result_preview"),
            "result": t.get("result") or t.get("result_preview"),
            **({"resources": t["resources"]} if t.get("resources") else {}),
        }
        for t in (tool_events or [])
    ]


# The tool whose result is the ONLY record of what a sub-agent found. That
# result is deliberately absent from ``conversation`` (the scaffolding
# predicate drops every ``tool`` message, or the model would mimic the
# protocol in later turns), so it is read back from ``messages``, where
# tool_message_from_step keeps it whole. Nothing extra is persisted for this:
# every session file — web, channel and autonomous alike — already has it.
DELEGATION_TOOL = "call_agent"


def delegation_history(session: dict, limit: int | None = None) -> list[dict]:
    """The sub-agent replies recorded in this session, oldest first.

    Ids are ``d<N>`` numbered from the OLDEST: they must stay stable as the
    chat appends new delegations, because the model quotes them back to us
    (recall_delegation). Numbering from the newest would renumber every entry
    at every turn.

    ``limit`` keeps the newest N entries — with their original ids, so a
    trimmed window still resolves.
    """
    out: list[dict] = []
    for m in session.get("messages") or []:
        if m.get("role") != "tool" or m.get("tool") != DELEGATION_TOOL:
            continue
        args = m.get("arguments") if isinstance(m.get("arguments"), dict) else {}
        sub = m.get("sub_trace") if isinstance(m.get("sub_trace"), dict) else {}
        out.append({
            "id": f"d{len(out) + 1}",
            "agent_id": args.get("agent_id") or sub.get("agent_id") or "",
            "message": args.get("message") or "",
            # A failed delegation is kept (its reply reads "ERROR: ..."): "we
            # tried and it did not work" is information, not noise.
            "reply": m.get("result") or sub.get("reply") or m.get("result_preview") or "",
            "ts": m.get("ts") or "",
        })
    if limit and len(out) > limit:
        return out[-limit:]
    return out


def record_turn(session: dict, steps: list[dict], reply: str, conversation,
                reasoning: str = "") -> None:
    """Append the tool calls (recursive trace) and assistant reply, and update
    the compact LLM history.

    `reasoning` is a thinking model's chain-of-thought: kept on the display
    message (the chat shows it collapsed) and deliberately absent from
    `conversation`, which is what goes back to the model next turn."""
    for step in steps:
        session["messages"].append(tool_message_from_step(step))
    msg = {"role": "assistant", "text": reply, "ts": now_iso()}
    if reasoning:
        msg["reasoning"] = reasoning
    session["messages"].append(msg)
    if conversation is not None:
        session["conversation"] = [
            m for m in conversation
            if (m.get("role") if isinstance(m, dict) else m.role) != "system"
        ]


def session_summary(s: dict, fallback_id: str = "") -> dict:
    """The listing projection of a session — ONE shape for both the web
    history and the channel store, so the sessions list never shows two
    different summaries for the same format."""
    return {
        "id": s.get("id") or fallback_id,
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
            title = title_from(first) if first.strip() else ""
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
        if not is_valid_id(session_id):
            return None
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
            summary = session_summary(s, fallback_id=f.stem)
            self._summaries[key] = (mtime, summary)
            out.append(summary)
        # Drop cache entries for deleted files
        for key in list(self._summaries):
            if key not in seen:
                del self._summaries[key]
        out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return out

    def get(self, session_id: str) -> dict | None:
        if not is_valid_id(session_id):
            return None
        f = self.history_dir / f"{session_id}.json"
        if f.exists():
            return read_json(f)
        cur = self.get_current()
        return cur if cur.get("id") == session_id else None

    def delete(self, session_id: str) -> bool:
        if not is_valid_id(session_id):
            return False
        f = self.history_dir / f"{session_id}.json"
        if f.exists():
            f.unlink()
            return True
        return False
