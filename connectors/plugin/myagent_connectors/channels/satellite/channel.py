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

from myagent_connectors.channels.base import BaseConnector, redact

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

    async def send(self, chat_id, text: str) -> bool:
        """Speak ``text`` on the device (notify_user's delivery path).

        ``chat_id`` is part of the BaseConnector contract but the device IS
        the chat — one satellite, one conversation (chat_id ≡ binding id, see
        ``ask()``) — so it plays no routing role here."""
        if not self.binding.url:
            self.status.detail = "no device URL configured"
            return False
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.post(self._device_url("/say"),
                                      json={"text": text}, headers=self._auth())
            if r.status_code != 200:
                self.status.errors += 1
                self.status.detail = f"/say answered {r.status_code}"
                return False
        except Exception as e:
            # Redacted defensively: an httpx failure can quote the request,
            # and the Authorization header carries the shared key.
            self.status.errors += 1
            self.status.detail = redact(str(e), self.binding.token)
            log.warning("say failed (%s): %s", self.binding.id, self.status.detail)
            return False
        self.status.errors = 0
        self.status.messages += 1
        self.status.last_update = datetime.now().isoformat(timespec="seconds")
        return True

    async def verify(self) -> dict:
        """Probe the device's /health — what the UI's test button calls."""
        if not self.binding.url:
            raise RuntimeError("no device URL configured")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                r = await client.get(self._device_url("/health"),
                                     headers=self._auth())
        except Exception as e:
            raise RuntimeError(redact(str(e), self.binding.token))
        if r.status_code != 200:
            raise RuntimeError(f"/health answered {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            data = {}
        return {"name": (data or {}).get("name", "")}

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
