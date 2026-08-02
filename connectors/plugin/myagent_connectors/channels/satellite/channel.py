"""Voice-satellite connector — a small speaker/mic device (PC, Raspberry) on
the LAN, running the standalone client in the repo's ``satellite/`` folder.

The transport is inverted relative to Telegram: the DEVICE calls US. Inbound
speech arrives on the plugin's ``POST /api/connectors/inbound/{binding_id}``
route (audio is transcribed server-side, then ``ask()`` below runs the turn)
and the agent's reply travels back **in the same HTTP response** — a voice
exchange must not race a push. Outbound (``send()``, what notify_user ends up
calling) POSTs to the device's ``/say`` endpoint, which speaks the text;
``verify()`` probes ``/health`` for the UI's test button. Both directions
share one credential: the binding token.

There is no poll loop, so ``start()`` returns immediately (the manager's
runner task simply finishes); the connector stays registered and reachable
through ``manager.get_connector()``.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from myagent_connectors.channels.base import BaseConnector, Unreachable, redact

log = logging.getLogger("connectors.satellite")

# The device is on the LAN and /say only queues text for its TTS: it answers
# within a couple of seconds or not at all.
HTTP_TIMEOUT = 10.0


class SatelliteConnector(BaseConnector):
    type = "satellite"

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        # Nothing to wait on (inbound is pushed by the device), and no blocking
        # health probe either: the device may be off, and at boot the network
        # may not be up yet — same reasoning as Telegram's transport retry.
        self.status.state = "running"
        self.status.detail = self.binding.url or "inbound only (no device URL)"

    async def stop(self) -> None:
        self.status.state = "stopped"

    # -------------------------------------------------------------- outbound
    def _device_url(self, path: str) -> str:
        return self.binding.url.rstrip("/") + path

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.binding.token}"}

    def _unreachable(self, e: Exception) -> Unreachable:
        """A device on a LAN is off, moved or asleep more often than it is
        misconfigured, so the message leads with the address that was tried —
        httpx's own text ("All connection attempts failed") names neither the
        host nor the port."""
        detail = redact(str(e), self.binding.token) or type(e).__name__
        return Unreachable(f"cannot reach the device at {self.binding.url}: "
                           f"{detail}")

    async def send(self, chat_id, text: str) -> bool:
        """Speak ``text`` on the device (notify_user's delivery path).

        ``chat_id`` is part of the BaseConnector contract but the device IS
        the chat — one satellite, one conversation (chat_id ≡ binding id, see
        ``ask()``) — so it plays no routing role here."""
        try:
            await self._device_call("POST", "/say", {"text": text})
        except Exception as e:
            # Already redacted by _device_call; best-effort by contract, so a
            # failure is recorded in the status, never raised.
            self.status.errors += 1
            self.status.detail = str(e)
            log.warning("say failed (%s): %s", self.binding.id, self.status.detail)
            return False
        self.status.errors = 0
        self.status.messages += 1
        self.status.last_update = datetime.now().isoformat(timespec="seconds")
        return True

    async def verify(self) -> dict:
        """Probe the device's /health — what the UI's test button calls."""
        data = await self._device_call("GET", "/health")
        return {"name": data.get("name", "")}

    # ------------------------------------------------- remote configuration
    # A voice device has settings only it can know are wrong — the silence
    # threshold against this room, which voice, which language is spoken here —
    # and they used to be reachable only by ssh'ing to the device and editing
    # config.json. These three methods put that file behind the binding form.
    # They are a second door onto it, not a replacement: the device reads the
    # same file at startup and runs on it with no server in sight.
    async def device_config(self) -> dict:
        """The device's current settings (GET /config). The device never
        returns the shared key, so nothing here needs masking."""
        return await self._device_call("GET", "/config")

    async def device_config_update(self, patch: dict) -> dict:
        """Write settings to the device (PUT /config). Only what the device
        declares writable is applied — it enforces that, not us: the pairing
        fields are refused there, where the file is."""
        return await self._device_call("PUT", "/config", patch)

    async def install_voice(self, name: str, use: bool = True) -> dict:
        """Ask the device to download a Piper voice. Returns as soon as the
        download STARTS (a voice is tens of MB): the caller polls
        ``device_config()['voice_install']`` for the outcome."""
        return await self._device_call("POST", "/voices/install",
                                      {"name": name, "use": use})

    async def _device_call(self, method: str, path: str,
                           payload: dict | None = None) -> dict:
        """One place for the device HTTP calls, so every one of them fails the
        same legible way. Raises RuntimeError with the device's own message —
        the caller is a router that turns it into a 400 for a form to show."""
        if not self.binding.url:
            raise RuntimeError("no device URL configured")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.request(method, self._device_url(path),
                                         json=payload, headers=self._auth())
        except httpx.TransportError as e:
            raise self._unreachable(e)
        except Exception as e:
            # The URL carries no secret, but the Authorization header does and
            # httpx errors can quote the request: redact defensively.
            raise RuntimeError(redact(str(e), self.binding.token))
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            detail = (data or {}).get("detail") or f"HTTP {r.status_code}"
            raise RuntimeError(f"{path} answered {r.status_code}: {detail}")
        return data or {}

    # --------------------------------------------------------------- inbound
    async def ask(self, text: str, sender_name: str = "") -> str:
        """One spoken exchange: run the agent turn and RETURN the reply — the
        device is waiting on it in the inbound HTTP response.

        chat_id ≡ binding id: the device has exactly one conversation, and an
        address-book contact reaches it with ``handles["satellite"] = binding
        id``, so notify_user's session append (``session_id_for(handle)``)
        lands in this same session. ``sender_id`` is the binding id too, which
        makes the provenance line resolve to that contact's name when one
        exists ("[Message from Cucina via Satellite]")."""
        chat_key = self.binding.id
        if chat_key in self._busy:
            # Spoken back by the device — same wording the other channels use.
            return "⏳ Still working on your previous message…"
        self._busy.add(chat_key)
        try:
            # The built-in commands too (/reset, /help): they are the same
            # conversation, and the device's Reset button sends "/reset" here
            # exactly as a Telegram user types it. Inside the busy guard so a
            # reset cannot land in the middle of a turn it would half-erase.
            handled = await self._handle_command(chat_key, text)
            if handled is not None:
                return handled
            sid = self.session_id_for(chat_key)
            reply = await self.client.chat(
                self.binding.agent_id, text, sid,
                source=self.type,
                sender_id=chat_key,
                sender_name=sender_name or self.binding.name)
        finally:
            self._busy.discard(chat_key)
        self.status.messages += 1
        self.status.last_update = datetime.now().isoformat(timespec="seconds")
        return reply or ""
