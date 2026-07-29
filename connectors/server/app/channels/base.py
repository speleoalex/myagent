"""Base connector: everything common to every messaging channel.

A concrete connector (e.g. Telegram) only has to implement the transport:
how to receive inbound messages and how to send a reply. All the shared logic
— access control, built-in commands, deriving the myagent session key, calling
the agent — lives here.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from app.models import Binding
from app.myagent_client import MyAgentClient
from app.storage import GrantStore

log = logging.getLogger("connectors.channel")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_session_id(prefix: str, chat_id) -> str:
    """Build a myagent-safe session key from a prefix and an external chat id.

    myagent validates session ids against ``^[A-Za-z0-9][A-Za-z0-9._-]*$``, so
    we sanitize the chat id (group chat ids are negative) and guarantee an
    alphanumeric first char via the prefix."""
    raw = f"{prefix}_{chat_id}"
    return _UNSAFE.sub("_", raw)


class ConnectorStatus:
    def __init__(self):
        self.state = "stopped"      # stopped | starting | running | error
        self.detail = ""            # bot @username, or last error message
        self.last_update = ""       # iso timestamp of last processed message
        self.messages = 0           # processed message count

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "last_update": self.last_update,
            "messages": self.messages,
        }


class BaseConnector:
    type = "base"

    def __init__(self, binding: Binding, client: MyAgentClient, grants: GrantStore):
        self.binding = binding
        self.client = client
        self.grants = grants
        self.status = ConnectorStatus()
        # Chats with a turn currently in flight. Prevents a user from stacking
        # requests (and hammering a slow local model) by firing many messages
        # before the previous answer is ready.
        self._busy: set = set()

    def session_id_for(self, chat_id) -> str:
        """The myagent session key this chat maps to.

        The ONE place that answers it. myagent must not recompute it: the key is
        built from ``session_prefix`` (which defaults to, but is not, the binding
        id) and a sanitized chat id, so it is not derivable from what myagent
        knows. ``/send`` returns this value precisely so an unsolicited message
        can be appended to the right conversation."""
        return _safe_session_id(self.binding.effective_prefix(), chat_id)

    # ------------------------------------------------- transport (subclass API)
    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(self, chat_id, text: str) -> None:
        raise NotImplementedError

    # ------------------------------------------------------- access control
    def _in_allowlist(self, user_id: int, username: str | None) -> bool:
        """A user is allowed if their numeric id OR their @username is listed."""
        if user_id in self.binding.allowed_ids:
            return True
        if username:
            uname = username.lstrip("@").lower()
            if uname in self.binding.allowed_usernames:
                return True
        return False

    def quick_authorized(self, user_id: int, username: str | None) -> bool:
        """Read-only authorization check (no password consumption). Used by the
        transport to decide whether to bother downloading a file before the full
        pipeline runs."""
        mode = self.binding.access_mode
        if mode == "open":
            return True
        if mode == "allowlist":
            return self._in_allowlist(user_id, username)
        if mode == "password":
            return user_id in self.grants.get(self.binding.id)
        return False

    def _authorized(self, user_id: int, username: str | None,
                    text: str) -> tuple[bool, str | None]:
        """Return (allowed, reply_if_denied_or_note). For password mode this may
        consume a ``/start <password>`` and grant access."""
        mode = self.binding.access_mode
        if mode == "open":
            return True, None
        if mode == "allowlist":
            if self._in_allowlist(user_id, username):
                return True, None
            return False, "⛔ You are not authorized to use this bot."
        if mode == "password":
            if user_id in self.grants.get(self.binding.id):
                return True, None
            # Expect "/start <password>" or the bare password.
            candidate = text.strip()
            if candidate.startswith("/start"):
                candidate = candidate[len("/start"):].strip()
            if self.binding.password and candidate == self.binding.password:
                self.grants.add(self.binding.id, user_id)
                return True, "✅ Access granted. Go ahead and send your message."
            return False, "🔒 This bot is protected. Send: /start <password>"
        return False, "⛔ Access is not configured."

    # ------------------------------------------------------- command handling
    async def _handle_command(self, chat_id, text: str) -> str | None:
        """Handle built-in slash commands. Returns a reply string if the message
        was a command (and thus fully handled), else None."""
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        if cmd == "/help":
            return self.binding.help_text or (
                "I'm an assistant. Send me a message.\n"
                "Commands: /reset to clear the conversation."
            )
        if cmd == "/reset":
            sid = self.session_id_for(chat_id)
            try:
                await self.client.reset_session(sid)
            except Exception as e:
                log.warning("reset failed for %s: %s", sid, e)
            return "🧹 Conversation cleared."
        return None

    # ------------------------------------------------------------- main flow
    async def process_message(self, chat_id, user_id: int, text: str,
                              username: str | None = None,
                              attachments: list[dict] | None = None) -> None:
        """Full inbound pipeline: access → commands → agent → reply.

        ``attachments`` is a list of Attachment dicts (image/text) the transport
        already downloaded and prepared for the agent."""
        text = text or ""
        attachments = attachments or None

        # Log every sender so the admin can discover ids/usernames to authorize
        # (visible via `journalctl -u myagent-connectors`).
        log.info("[%s] inbound from id=%s username=%s%s", self.binding.id, user_id,
                 ("@" + username) if username else "-",
                 f" ({len(attachments)} file)" if attachments else "")

        allowed, note = self._authorized(user_id, username, text)
        if not allowed:
            log.info("[%s] DENIED id=%s username=%s (add it to the allowlist to grant access)",
                     self.binding.id, user_id, ("@" + username) if username else "-")
            if note:
                await self.send(chat_id, note)
            return
        if note:  # e.g. password just accepted
            await self.send(chat_id, note)
            # A bare "/start <pw>" carries no real question — stop here.
            if text.strip().startswith("/start"):
                return

        # Commands and the greeting only apply to plain-text messages: a message
        # carrying a file goes straight to the agent (caption is the prompt).
        if not attachments:
            if text.strip().lower() in ("/start", ""):
                await self.send(chat_id, self.binding.welcome or "👋 Hi! How can I help you?")
                return
            handled = await self._handle_command(chat_id, text)
            if handled is not None:
                await self.send(chat_id, handled)
                return

        if chat_id in self._busy:
            await self.send(chat_id, "⏳ Sto ancora elaborando il messaggio precedente…")
            return

        sid = self.session_id_for(chat_id)
        self._busy.add(chat_id)
        # Keep a "typing…" indicator alive for the whole (possibly slow) agent
        # turn — a single action would expire after ~5s and look stalled.
        typing = asyncio.create_task(self._typing_loop(chat_id))
        try:
            reply = await self.client.chat(self.binding.agent_id, text, sid,
                                           attachments=attachments,
                                           source=self.type)
        except Exception as e:
            log.exception("agent call failed (%s): %s", self.binding.id, e)
            await self.send(chat_id, "⚠️ Error generating the reply. Please try again later.")
            return
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing
            self._busy.discard(chat_id)

        self.status.messages += 1
        await self.send(chat_id, reply or "(nessuna risposta)")

    async def _typing_loop(self, chat_id) -> None:
        """Transport hook: keep a 'typing…' indicator alive while the agent
        works. Default: no-op. Cancelled as soon as the reply is ready."""
        return None
