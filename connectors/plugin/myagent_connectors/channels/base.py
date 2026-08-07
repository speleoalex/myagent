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

from app import config as app_config

from myagent_connectors import config
from myagent_connectors.models import Binding, _as_handle
from myagent_connectors.core import CoreClient
from myagent_connectors.storage import DisclosureStore, GrantStore

log = logging.getLogger("connectors.channel")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Sent once per chat when Binding.disclose_ai is on (see the field's comment for
# why it exists). Deliberately three short lines: it lands before the answer the
# person is waiting for, so anything longer gets scrolled past, which is the one
# outcome that makes it worthless. The second line is not decoration — "it can
# be wrong" is what turns a label into something the reader can act on.
DEFAULT_AI_DISCLOSURE = (
    "🤖 Heads up: you're chatting with an AI assistant, not a person.\n"
    "It can be wrong or incomplete — double-check anything that matters."
)

# Files delivered per reply (resource channel): a runaway tool must not turn
# one Telegram answer into an album, and each send is a full upload.
MAX_FILES_PER_REPLY = 5
MAX_FILE_BYTES = 15 * 1024 * 1024


class Unreachable(RuntimeError):
    """The far end could not be contacted at all.

    A network fact, not a credential one, and worth its own type because the
    caller renders it: a failed connection used to surface as *"Invalid
    credentials: All connection attempts failed"* in the bot form — which sent
    the reader looking for a wrong token while the real answer was that the
    device was switched off. Whoever raises this has already put the address in
    the message, so the router shows it verbatim.
    """


def _safe_session_id(prefix: str, chat_id) -> str:
    """Build a myagent-safe session key from a prefix and an external chat id.

    myagent validates session ids against ``^[A-Za-z0-9][A-Za-z0-9._-]*$``, so
    we sanitize the chat id (group chat ids are negative) and guarantee an
    alphanumeric first char via the prefix."""
    raw = f"{prefix}_{chat_id}"
    return _UNSAFE.sub("_", raw)


def redact(text: str, *secrets: str) -> str:
    """Strip bot credentials out of a string before it leaves the process.

    Needed because the transport builds URLs that CONTAIN the token, and an HTTP
    client's error message quotes the URL it failed on. Such a message used to
    travel two ways that both hand the token to someone else: back to the chat
    as "could not download: <error>", and into ``status.detail``, which the admin
    API serves to the browser. Anyone reading it owns the bot.
    """
    for secret in secrets:
        if secret and len(secret) > 4:
            text = text.replace(secret, "***")
    return text


class ConnectorStatus:
    def __init__(self):
        # stopped | starting | running | error | paused (disabled is derived
        # from the binding, not stored here)
        self.state = "stopped"
        self.detail = ""            # bot @username, or last error message
        self.last_update = ""       # iso timestamp of last processed message
        self.messages = 0           # processed message count
        # Consecutive failures. Reset by the first success; when it reaches the
        # configured ceiling the connector pauses itself instead of retrying
        # forever — a dead token would otherwise fill the agent's own journal.
        self.errors = 0

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "last_update": self.last_update,
            "messages": self.messages,
            "errors": self.errors,
        }


class BaseConnector:
    type = "base"

    def __init__(self, binding: Binding, client: CoreClient, grants: GrantStore):
        self.binding = binding
        self.client = client
        self.grants = grants
        self.status = ConnectorStatus()
        # Chats with a turn currently in flight. Prevents a user from stacking
        # requests (and hammering a slow local model) by firing many messages
        # before the previous answer is ready.
        self._busy: set = set()
        # Built here rather than injected like ``grants``: nothing outside this
        # class reads it, and create_connector's signature is the one every
        # channel has to absorb — the registry promises that adding a channel
        # needs no changes elsewhere.
        self._disclosed = DisclosureStore(config.DISCLOSED_DIR)
        # Mirror of the persisted set. If the disk copy cannot be written, this
        # still stops the notice from repeating on every single message until
        # the next restart — a disclosure that spams is one the operator turns
        # off, and then it protects nobody.
        self._disclosed_here: set = set()

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

    async def send(self, chat_id, text: str) -> bool:
        """Deliver text to a chat. Returns whether it went out.

        Best-effort by contract — the inbound path must not turn a failed reply
        into an exception, or one user who blocked the bot would drive the poll
        loop into auto-pause. But the RESULT matters to notify_user, which tells
        an agent (and through it the user) that a message was sent: without a
        return value it reported success on a dead token."""
        raise NotImplementedError

    async def send_file(self, chat_id, name: str, data: bytes, mime: str,
                        title: str) -> bool:
        """Deliver a file the turn produced (resource channel). Optional
        transport hook: the default says "not supported", which is the RIGHT
        answer for a voice satellite — the spoken reply already names the
        file, and the channel session keeps it visible in the web UI. Same
        best-effort contract as send()."""
        return False

    async def verify(self) -> dict:
        """Check the binding's credentials and describe the account they open.

        Returns a small dict for the UI (Telegram: ``{"bot": …, "name": …}``) or
        raises with a readable reason. Lives here so the router can offer "test
        this token" without importing a channel class and refusing every other
        type — the registry promises that adding a channel needs no changes
        elsewhere, and this is what makes that true."""
        raise NotImplementedError(
            f"the '{self.type}' channel does not support testing credentials"
        )

    # ------------------------------------------------------- access control
    def _in_allowlist(self, user_id: str, username: str | None) -> bool:
        """A user is allowed if their id OR their @username is listed."""
        if user_id in self.binding.allowed_ids:
            return True
        if username:
            uname = username.lstrip("@").lower()
            if uname in self.binding.allowed_usernames:
                return True
        return False

    def quick_authorized(self, user_id, username: str | None) -> bool:
        """Read-only authorization check (no password consumption). Used by the
        transport to decide whether to bother downloading a file before the full
        pipeline runs.

        Normalizes like process_message: this is the other entry point a
        transport calls directly, and it is called with the API's native id."""
        user_id = _as_handle(user_id)
        mode = self.binding.access_mode
        if mode == "open":
            return True
        if mode == "allowlist":
            return self._in_allowlist(user_id, username)
        if mode == "password":
            return user_id in self.grants.get(self.binding.id)
        return False

    def _authorized(self, user_id: str, username: str | None,
                    text: str) -> tuple[bool, str | None]:
        """Return (allowed, reply_if_denied_or_note). For password mode this may
        consume a ``/start <password>`` and grant access."""
        if self.quick_authorized(user_id, username):
            return True, None
        mode = self.binding.access_mode
        if mode == "allowlist":
            return False, "⛔ You are not authorized to use this bot."
        if mode == "password":
            # Expect "/start <password>" or the bare password.
            candidate = text.strip()
            if candidate.startswith("/start"):
                candidate = candidate[len("/start"):].strip()
            if self.binding.password and candidate == self.binding.password:
                self.grants.add(self.binding.id, user_id)
                return True, "✅ Access granted. Go ahead and send your message."
            return False, "🔒 This bot is protected. Send: /start <password>"
        return False, "⛔ Access is not configured."

    # ---------------------------------------------------------- AI disclosure
    async def _ensure_disclosed(self, chat_id) -> None:
        """Tell this chat once that it is talking to an AI (EU AI Act art. 50).

        Its own message rather than a prefix on the first answer: the Act asks
        for a "clear and distinguishable" notice, and a line glued to the top of
        a long reply is neither. It also keeps the disclosure out of what the
        agent said, which is the text the operator may forward or quote.

        Sent from HERE — the moment the sender is known to be authorized — and
        not from the welcome/help texts, because those only fire when someone
        types ``/start`` or ``/help``. In ``open`` and ``allowlist`` mode a
        person can simply write a question and get an answer, and that path used
        to reach the model without ever saying what was on the other end.

        Best-effort by construction: a chat that cannot be marked is told again
        later, and a transport that cannot deliver the notice must not swallow
        the question. Never let this raise into the message path.
        """
        if not self.binding.disclose_ai:
            return
        chat_key = _as_handle(chat_id)
        if chat_key in self._disclosed_here:
            return
        try:
            already = chat_key in self._disclosed.get(self.binding.id)
        except Exception as e:  # unreadable store: better to repeat than to skip
            log.warning("[%s] disclosure state unreadable: %s", self.binding.id, e)
            already = False
        if already:
            self._disclosed_here.add(chat_key)
            return
        try:
            await self.send(chat_id, self.binding.ai_disclosure or DEFAULT_AI_DISCLOSURE)
        except Exception as e:
            # Not marked as disclosed: the next message tries again.
            log.warning("[%s] could not send AI disclosure: %s", self.binding.id, e)
            return
        self._disclosed_here.add(chat_key)
        try:
            self._disclosed.add(self.binding.id, chat_key)
        except Exception as e:
            log.warning("[%s] could not persist disclosure state: %s", self.binding.id, e)

    # ------------------------------------------------------- command handling
    async def _handle_command(self, chat_id, text: str) -> str | None:
        """Handle built-in slash commands. Returns a reply string if the message
        was a command (and thus fully handled), else None."""
        cmd = text.strip().split()[0].lower() if text.strip() else ""
        if cmd == "/help":
            # Says "AI assistant", not "assistant": this is the one text a
            # person reads when they explicitly ask what they are talking to,
            # and the old wording answered that question wrongly.
            return self.binding.help_text or (
                "I'm an AI assistant — replies are generated by a language "
                "model and can be wrong.\n"
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
    async def process_message(self, chat_id, user_id, text: str,
                              username: str | None = None,
                              attachments: list[dict] | None = None,
                              sender_name: str | None = None) -> None:
        """Full inbound pipeline: access → commands → agent → reply.

        ``attachments`` is a list of Attachment dicts (image/text) the transport
        already downloaded and prepared for the agent.

        ``sender_name`` is the transport's display name for the sender (e.g.
        Telegram first/last name) — a fallback for the provenance line when the
        address book doesn't know this id.

        ``user_id`` is normalized to a string HERE, at the one entry point, so a
        transport may hand over whatever its API gives it (Telegram: an int) and
        everything downstream compares identifiers of one type."""
        text = text or ""
        attachments = attachments or None
        user_id = _as_handle(user_id)

        # Log every sender so the admin can discover ids/usernames to authorize
        # (visible via `journalctl -u myagent`).
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

        # First thing an authorized sender hears from us, on every path into the
        # agent: commands, greeting and plain questions all pass through here.
        # A DENIED sender is deliberately above this line — they never reach the
        # model, so there is no AI interaction to disclose.
        await self._ensure_disclosed(chat_id)

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
            await self.send(chat_id, "⏳ Still working on your previous message…")
            return

        sid = self.session_id_for(chat_id)
        self._busy.add(chat_id)
        # Keep a "typing…" indicator alive for the whole (possibly slow) agent
        # turn — a single action would expire after ~5s and look stalled.
        typing = asyncio.create_task(self._typing_loop(chat_id))
        try:
            reply, resources = await self.client.chat(
                self.binding.agent_id, text, sid,
                attachments=attachments,
                source=self.type,
                sender_id=user_id,
                sender_username=username or "",
                sender_name=sender_name or "")
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
        await self.send(chat_id, reply or "(no reply)")
        # Text first, then the files it talks about ("here's your report" and
        # THEN the report). Best-effort: the reply already went out.
        if resources:
            await self._deliver_resources(chat_id, resources)

    async def _deliver_resources(self, chat_id, resources: list[dict]) -> None:
        """Send the turn's delivered files through the transport hook.

        The paths were validated by the executor when the marker was
        extracted, but they crossed a session file since — so containment
        under the workspace is re-checked HERE, where the bytes are read.
        Stops at the first refusal: a transport that answered "not supported"
        once will answer it for every file (and the reply text already names
        them)."""
        root = app_config.WORKSPACE_DIR.resolve()
        sent = 0
        for r in resources:
            if sent >= MAX_FILES_PER_REPLY:
                log.info("[%s] resource cap reached, %d file(s) not sent",
                         self.binding.id, len(resources) - sent)
                break
            path = str((r or {}).get("path") or "")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            target = (root / path).resolve()
            try:
                if (not target.is_relative_to(root) or not target.is_file()
                        or target.stat().st_size > MAX_FILE_BYTES):
                    continue
                data = target.read_bytes()
            except OSError as e:
                log.warning("[%s] cannot read resource %s: %s",
                            self.binding.id, path, e)
                continue
            ok = await self.send_file(
                chat_id, target.name, data,
                r.get("mime") or "application/octet-stream",
                r.get("title") or target.name)
            if not ok:
                break
            sent += 1

    async def _typing_loop(self, chat_id) -> None:
        """Transport hook: keep a 'typing…' indicator alive while the agent
        works. Default: no-op. Cancelled as soon as the reply is ready."""
        return None
