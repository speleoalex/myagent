"""Telegram connector — long polling via the Bot API (no external SDK).

Uses raw httpx calls to https://api.telegram.org/bot<token>/<method>. Runs a
getUpdates long-poll loop; each text message is fed to the shared inbound
pipeline in BaseConnector and the reply is sent back (chunked to Telegram's
4096-char limit).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime

import httpx

from myagent_connectors import config
from myagent_connectors.channels.base import (BaseConnector, Unreachable,
                                             redact)

log = logging.getLogger("connectors.telegram")

# httpx logs every request line at INFO, and a Telegram URL embeds the bot token
# ("POST https://api.telegram.org/bot123456:ABC…/getMe"). With the plugin inside
# myagent that lands in the agent's own journal, so the credentials would be
# readable by anyone who can read the service log. Its INFO output is per-request
# noise anyway; warnings and errors still come through.
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
MAX_MSG = 4096              # Telegram hard limit per message
MAX_FILE = 15 * 1024 * 1024  # 15 MB (Telegram Bot API download cap is 20 MB)
# Long-poll seconds: getUpdates blocks up to this long. A transport knob, so it
# lives with the transport and not in the plugin's shared config.
POLL_TIMEOUT = int(os.environ.get("MYAGENT_TELEGRAM_POLL_TIMEOUT") or 30)

# Commands advertised via setMyCommands — they populate the client's "Menu"
# button and the "/" autocomplete list. These mirror the built-in commands
# handled in BaseConnector._handle_command.
BOT_COMMANDS = [
    {"command": "reset", "description": "Clear the conversation"},
    {"command": "help", "description": "Show help"},
    {"command": "start", "description": "Start the bot"},
]

# Persistent reply-keyboard buttons: (label shown on the button, command it
# stands for). A tap sends the label as a normal message, which _dispatch maps
# back to the command before the shared pipeline handles it.
COMMAND_BUTTONS = [("🧹 Reset", "/reset"), ("❓ Help", "/help")]
_BUTTON_CMD = {label: cmd for label, cmd in COMMAND_BUTTONS}
MENU_KEYBOARD = {
    "keyboard": [[{"text": label} for label, _ in COMMAND_BUTTONS]],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Documents we accept as text (by mime or extension). Everything else that
# isn't an image is refused — dumping binary as text would just feed the model
# garbage.
_TEXT_MIMES = {
    "application/json", "application/xml", "application/x-yaml",
    "application/yaml", "application/javascript", "application/x-sh",
    "application/x-python", "application/toml", "application/csv",
}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".ini", ".cfg", ".conf", ".toml", ".log", ".py", ".js", ".ts",
    ".html", ".htm", ".css", ".sh", ".sql", ".c", ".cpp", ".h", ".java",
    ".go", ".rs", ".rb", ".php", ".srt", ".vtt",
}
_AUDIO_EXTS = {
    ".oga", ".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac",
    ".wma", ".amr", ".weba",
}


class TelegramConnector(BaseConnector):
    type = "telegram"

    def __init__(self, binding, client, grants):
        super().__init__(binding, client, grants)
        self._offset = 0
        self._stop = asyncio.Event()
        self._http: httpx.AsyncClient | None = None
        # Chats we've already sent the command keyboard to. Reply keyboards are
        # sticky client-side, so one send per chat keeps the buttons visible
        # without re-attaching (and re-expanding) them on every reply.
        self._menu_shown: set = set()
        # "@botname" once known; the status line shows it while healthy.
        self._account = ""

    # ------------------------------------------------------------- API helper
    async def _call(self, method: str, timeout: float = 15.0, **params):
        url = API.format(token=self.binding.token, method=method)
        assert self._http is not None
        resp = await self._http.post(url, json=params, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data.get("result")

    async def verify(self) -> dict:
        """The shared 'test these credentials' hook: getMe, projected for the UI."""
        try:
            me = await self.get_me()
        except httpx.TransportError as e:
            # No network is not a bad token. Reframed HERE and not in get_me():
            # start()'s boot-time retry catches httpx.TransportError by type,
            # and a device that boots before the WiFi depends on it.
            raise Unreachable(f"cannot reach Telegram: "
                              f"{redact(str(e), self.binding.token) or type(e).__name__}")
        return {"bot": me.get("username"), "name": me.get("first_name")}

    async def get_me(self) -> dict:
        """Validate the token and return the bot account. Used by verify() and by
        start(), which also needs the @username for the status line."""
        async with httpx.AsyncClient() as c:
            url = API.format(token=self.binding.token, method="getMe")
            data = (await c.post(url, timeout=10.0)).json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "invalid token"))
        return data["result"]

    # -------------------------------------------------------------- transport
    async def send(self, chat_id, text: str) -> bool:
        # Split long replies to respect Telegram's per-message limit.
        n = len(text) or 1
        show_menu = chat_id not in self._menu_shown
        ok = True
        for i in range(0, n, MAX_MSG):
            chunk = text[i:i + MAX_MSG] or " "
            params = {"chat_id": chat_id, "text": chunk}
            # Attach the persistent command keyboard once per chat, on the last
            # chunk only (it stays visible client-side after that).
            if show_menu and i + MAX_MSG >= n:
                params["reply_markup"] = MENU_KEYBOARD
            try:
                await self._call("sendMessage", **params)
            except Exception as e:
                # Redacted: the failure text can quote a URL holding the token.
                log.warning("sendMessage failed (%s): %s", self.binding.id,
                            redact(str(e), self.binding.token))
                ok = False
                break
        self._menu_shown.add(chat_id)
        return ok

    async def _typing_loop(self, chat_id) -> None:
        # Telegram's "typing…" lasts ~5s; refresh it until the reply is ready
        # (loop is cancelled by the caller when the agent turn finishes).
        try:
            while True:
                try:
                    await self._call("sendChatAction", chat_id=chat_id, action="typing")
                except Exception:
                    return  # transient send error; cosmetic only, stop quietly
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ loop
    async def start(self) -> None:
        self.status.state = "starting"
        self._http = httpx.AsyncClient()
        # The initial getMe must survive a network that isn't there yet: at boot
        # this task starts before DNS/routes are up (systemd's network.target
        # doesn't wait for connectivity), and dying here left the bot in a
        # permanent "error" until a manual resume. Transport errors get the same
        # retry-with-backoff contract as the poll loop; a rejected token is not
        # transient and still fails hard.
        backoff = 1
        last_logged = ""
        while True:
            try:
                me = await self.get_me()
                break
            except httpx.TransportError as e:
                if self._stop.is_set():
                    await self._http.aclose()
                    self._http = None
                    return
                self.status.state = "error"
                self.status.detail = redact(str(e), self.binding.token)
                if self.status.detail != last_logged:
                    last_logged = self.status.detail
                    log.warning("start blocked (%s): %s — retrying, backoff %ss",
                                self.binding.id, self.status.detail, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self.status.state = "error"
                self.status.detail = redact(str(e), self.binding.token)
                await self._http.aclose()
                self._http = None
                raise
        # Kept so a recovery can restore it: on the way out of an error the
        # status would otherwise keep showing the stale failure message while
        # reporting "running".
        self._account = "@" + me.get("username", "?")
        self.status.detail = self._account
        # A bot in webhook mode can't use getUpdates — switch it to polling.
        try:
            await self._call("deleteWebhook")
        except Exception:
            pass
        # Advertise the built-in commands so they show up in the client's Menu
        # button and "/" autocomplete (best-effort; a failure isn't fatal).
        try:
            await self._call("setMyCommands", commands=BOT_COMMANDS)
        except Exception as e:
            log.warning("setMyCommands failed (%s): %s", self.binding.id, e)
        self.status.state = "running"
        log.info("Telegram connector '%s' running as %s", self.binding.id, self.status.detail)
        await self._poll_loop()

    async def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self.status.state = "stopped"

    async def _poll_loop(self) -> None:
        backoff = 1
        last_logged = ""
        while not self._stop.is_set():
            try:
                updates = await self._get_updates()
                backoff = 1
                self.status.errors = 0
                last_logged = ""
                if self.status.state == "error":
                    self.status.state = "running"
                    self.status.detail = self._account
                for upd in updates or []:
                    self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                    await self._dispatch(upd)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                self.status.errors += 1
                self.status.state = "error"
                self.status.detail = redact(str(e), self.binding.token)
                # The state stays "error" until a poll actually succeeds. It used
                # to be reset to "running" right here, which meant a bot broken
                # for hours still reported itself healthy.
                if self.status.errors >= config.MAX_CONSECUTIVE_ERRORS:
                    self.status.state = "paused"
                    log.warning(
                        "connector '%s' paused after %d consecutive errors: %s "
                        "(POST /api/connectors/bindings/%s/resume to retry)",
                        self.binding.id, self.status.errors, self.status.detail,
                        self.binding.id,
                    )
                    return
                # Log the first occurrence of each distinct failure, then stay
                # quiet: this loop shares its journal with the agent now.
                if self.status.detail != last_logged:
                    last_logged = self.status.detail
                    log.warning("poll error (%s): %s — retrying, backoff %ss",
                                self.binding.id, self.status.detail, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _get_updates(self):
        # 'timeout' is a Telegram param (long-poll seconds); the httpx read
        # timeout must be a bit longer so the connection isn't cut mid-poll.
        url = API.format(token=self.binding.token, method="getUpdates")
        params = {"offset": self._offset, "timeout": POLL_TIMEOUT,
                  "allowed_updates": ["message"]}
        resp = await self._http.post(
            url, json=params, timeout=POLL_TIMEOUT + 15
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getUpdates failed"))
        return data.get("result", [])

    # ------------------------------------------------------------ file download
    async def _fetch_file(self, file_id: str) -> bytes:
        """getFile -> download the file bytes. Raises on size/API/download error."""
        info = await self._call("getFile", file_id=file_id)
        size = info.get("file_size") or 0
        if size and size > MAX_FILE:
            raise ValueError("file is too large")
        path = info.get("file_path")
        if not path:
            raise ValueError("file path unavailable")
        url = FILE_API.format(token=self.binding.token, path=path)
        resp = await self._http.get(url, timeout=90.0)
        # Deliberately NOT raise_for_status(): httpx puts the failing URL in the
        # message, and this URL embeds the bot token. Callers relay these errors
        # to the chat, so that would publish the credentials.
        if resp.status_code >= 400:
            raise RuntimeError(f"download failed: HTTP {resp.status_code}")
        content = resp.content
        if len(content) > MAX_FILE:
            raise ValueError("file is too large")
        return content

    @staticmethod
    def _image_attachment(content: bytes, mime: str, name: str) -> dict:
        b64 = base64.b64encode(content).decode()
        return {"name": name, "kind": "image", "mime": mime,
                "data": f"data:{mime};base64,{b64}"}

    @staticmethod
    def _text_attachment(content: bytes, name: str) -> dict:
        return {"name": name, "kind": "text",
                "data": content.decode("utf-8", errors="replace")}

    @staticmethod
    def _audio_attachment(content: bytes, mime: str, name: str) -> dict:
        # Sent as-is (opus/oga/mp3/...); myagent lets the bound model read it
        # natively (if audio-capable) or transcribe it via document_extract.
        b64 = base64.b64encode(content).decode()
        return {"name": name, "kind": "audio", "mime": mime,
                "data": f"data:{mime};base64,{b64}"}

    @staticmethod
    def _binary_attachment(content: bytes, mime: str, name: str, kind: str = "file") -> dict:
        # Generic binary (e.g. PDF): myagent materializes it to a workspace file
        # and the model reads it via document_extract (never inlined as text).
        mime = mime or "application/octet-stream"
        b64 = base64.b64encode(content).decode()
        return {"name": name, "kind": kind, "mime": mime,
                "data": f"data:{mime};base64,{b64}"}

    async def _extract_attachments(self, msg: dict):
        """Return (attachments, caption, error_note). error_note is a user-facing
        string when a file was present but couldn't be accepted."""
        caption = msg.get("caption") or ""

        if msg.get("photo"):
            photo = msg["photo"][-1]  # largest rendition
            try:
                content = await self._fetch_file(photo["file_id"])
            except Exception as e:
                return [], caption, f"⚠️ Could not download the image: {redact(str(e), self.binding.token)}"
            return [self._image_attachment(content, "image/jpeg", "photo.jpg")], caption, None

        doc = msg.get("document")
        if doc:
            name = doc.get("file_name") or "file"
            mime = (doc.get("mime_type") or "").lower()
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            is_image = mime.startswith("image/")
            is_text = mime.startswith("text/") or mime in _TEXT_MIMES or ext in _TEXT_EXTS
            is_audio = mime.startswith("audio/") or ext in _AUDIO_EXTS
            is_pdf = mime == "application/pdf" or ext == ".pdf"
            if not (is_image or is_text or is_audio or is_pdf):
                return [], caption, (
                    "📎 Unsupported file type. I can handle images, text files "
                    f"audio and PDF (got: {mime or ext or 'unknown'})."
                )
            try:
                content = await self._fetch_file(doc["file_id"])
            except Exception as e:
                return [], caption, f"⚠️ Could not download the file: {redact(str(e), self.binding.token)}"
            if is_image:
                return [self._image_attachment(content, mime or "image/jpeg", name)], caption, None
            if is_text:
                return [self._text_attachment(content, name)], caption, None
            if is_audio:
                return [self._audio_attachment(content, mime or "audio/ogg", name)], caption, None
            return [self._binary_attachment(content, mime or "application/pdf", name)], caption, None

        # An audio *file* ('audio') → attachment; myagent lets the model read it
        # natively or transcribe it via document_extract. (Voice notes 'voice' are
        # handled separately: transcribed to text at the edge, see _dispatch.)
        audio = msg.get("audio")
        if audio:
            mime = (audio.get("mime_type") or "audio/mpeg").lower()
            name = audio.get("file_name") or "audio"
            try:
                content = await self._fetch_file(audio["file_id"])
            except Exception as e:
                return [], caption, f"⚠️ Could not download the audio: {redact(str(e), self.binding.token)}"
            return [self._audio_attachment(content, mime, name)], caption, None

        return [], caption, None

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        if not chat:
            return
        chat_id, user_id, username = chat.get("id"), frm.get("id"), frm.get("username")
        # Telegram's display name — many users have no @username, and this is
        # what the provenance line falls back to when the address book has no
        # contact for the id.
        sender_name = " ".join(
            p for p in (frm.get("first_name"), frm.get("last_name")) if p)
        text = msg.get("text")
        # A tapped reply-keyboard button arrives as its label text — map it back
        # to the command it stands for so the shared pipeline handles it.
        if text is not None:
            text = _BUTTON_CMD.get(text.strip(), text)
        has_media = bool(msg.get("photo") or msg.get("document")
                         or msg.get("voice") or msg.get("audio"))

        # Message types we still don't handle (video, sticker, …) and no text.
        if text is None and not has_media:
            if any(msg.get(k) for k in ("video", "video_note", "sticker")):
                if self.quick_authorized(user_id, username):
                    await self.send(chat_id, "📎 For now I handle text, images, text files and voice messages.")
            return

        self.status.last_update = datetime.now().isoformat(timespec="seconds")

        attachments = None
        if has_media:
            # Don't download anything from users who aren't authorized anyway.
            if not self.quick_authorized(user_id, username):
                await self.process_message(chat_id, user_id, text or "", username=username)
                return
            if msg.get("voice"):
                # Voice note = the user "speaking": transcribe to text at the edge
                # (standalone STT) so it works with any bound agent/model.
                text, err = await self._transcribe_voice(chat_id, msg)
                if err:
                    await self.send(chat_id, err)
                    return
            else:
                atts, caption, err = await self._extract_attachments(msg)
                if err:
                    await self.send(chat_id, err)
                    if not atts:
                        return
                attachments = atts or None
                text = caption

        await self.process_message(chat_id, user_id, text or "", username=username,
                                   attachments=attachments, sender_name=sender_name)

    async def _transcribe_voice(self, chat_id, msg: dict):
        """Download a voice note and transcribe it locally to text. Returns
        (text, error_note); error_note is a user-facing string on failure."""
        voice = msg.get("voice") or {}
        caption = msg.get("caption") or ""
        try:  # let the user see 'typing…' during download + transcription
            await self._call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            content = await self._fetch_file(voice["file_id"])
        except Exception as e:
            return None, f"⚠️ Could not download the voice message: {redact(str(e), self.binding.token)}"
        try:
            transcript = await self.client.transcribe(content, "voice.oga")
        except Exception as e:
            log.warning("transcription failed (%s): %s", self.binding.id, e)
            return None, "⚠️ Voice transcription is not available."
        if not transcript:
            return None, "🎙️ I could not make out any speech in that voice message."
        return (caption + "\n\n" + transcript if caption else transcript), None
