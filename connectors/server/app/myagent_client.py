"""Thin async HTTP client for the myagent API.

The connectors server is just a client of myagent: it drives agent turns and
reads the agent list (to populate the admin UI). Nothing myagent-internal is
imported — only its public HTTP surface is used.
"""
from __future__ import annotations

import httpx

from app import config


class MyAgentClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or config.MYAGENT_API_URL).rstrip("/")
        self.token = token if token is not None else config.MYAGENT_API_TOKEN

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def chat(self, agent_id: str, message: str, session_id: str,
                   attachments: list[dict] | None = None,
                   timeout: float | None = None) -> str:
        """Run one agent turn against a channel-scoped session and return the
        assistant reply text. ``attachments`` is a list of Attachment dicts
        ({name, kind: 'image'|'text', data, mime?}) forwarded to the agent.
        Raises httpx.HTTPStatusError on API errors."""
        url = f"{self.base_url}/api/chat"
        payload = {
            "agent_id": agent_id,
            "message": message,
            "session_id": session_id,
        }
        if attachments:
            payload["attachments"] = attachments
        async with httpx.AsyncClient(timeout=timeout or config.CHAT_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        return data.get("reply", "")

    async def reset_session(self, session_id: str) -> bool:
        url = f"{self.base_url}/api/chat/sessions/{session_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(url, headers=self._headers())
            return resp.status_code == 200

    async def list_agents(self, selectable: bool = False) -> list[dict]:
        url = f"{self.base_url}/api/agents"
        if selectable:
            url += "?selectable=true"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def health(self) -> bool:
        try:
            await self.list_agents()
            return True
        except Exception:
            return False
