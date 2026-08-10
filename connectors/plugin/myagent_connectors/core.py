"""The seam between a connector and the agent engine.

This replaces the HTTP client the connectors used when they were a separate
server. It keeps the same shape on purpose — the connectors hold it as
``self.client`` and call ``chat`` / ``reset_session`` — so the channel code did
not have to change when the transport went away.

Calling the engine directly rather than looping back over HTTP: a loopback
request would have to carry MYAGENT_API_KEY to get past our own middleware (a
secret the process already *is*), and would add a socket, a JSON round-trip and
a second timeout for no isolation in return.
"""
from __future__ import annotations

import asyncio
import logging

from app.engine.channel_turn import run_channel_turn
from app.engine.executor import AgentExecutor
from app.models import ChatRequest
from app.storage.attachments import store_attachment

from myagent_connectors import config

log = logging.getLogger("connectors.core")


def collect_resources(trace) -> list[dict]:
    """Files the turn delivered through the resource channel — walked
    RECURSIVELY through sub-agent traces, so a page master delegated to
    html-designer still reaches the Telegram user. Mirrors the UI's
    collectResources (ui/js/chat.js); dedup by path, document order."""
    out: list[dict] = []
    seen: set = set()

    def walk(steps):
        for s in steps or []:
            for r in (s.get("resources") or []):
                path = r.get("path") if isinstance(r, dict) else None
                if path and path not in seen:
                    seen.add(path)
                    out.append(r)
            sub = s.get("sub_trace")
            if isinstance(sub, dict):
                walk(sub.get("steps"))

    if isinstance(trace, dict):
        walk(trace.get("steps"))
    return out


class CoreClient:
    """Drives agent turns on channel-scoped sessions.

    ``state`` is the FastAPI ``app.state``: read at call time, never cached, so
    it does not matter that the plugin is registered before some services are
    put there.
    """

    def __init__(self, state, semaphore: asyncio.Semaphore | None = None):
        self._state = state
        # Shared across every binding: the point is to bound total load on the
        # model, not per-bot fairness.
        self._turns = semaphore or asyncio.Semaphore(config.MAX_CONCURRENT_TURNS)

    async def chat(self, agent_id: str, message: str, session_id: str,
                   attachments: list[dict] | None = None,
                   source: str | None = None,
                   sender_id: str = "", sender_username: str = "",
                   sender_name: str = "",
                   transcribed: bool = False) -> tuple[str, list[dict]]:
        """Run one agent turn; return ``(reply text, delivered resources)``.

        The resources are the files the turn flagged for the user (the
        resource channel, ``app/tools/resources.py``): the transport decides
        what to do with them — Telegram sends them as photos/documents, a
        voice satellite has nothing to show and skips them (they stay visible
        in the channel session from the web UI).

        ``sender_*`` is whatever the transport knows about who wrote (id,
        @username, display name); it is resolved against the address book here
        — the one place that has both the transport data and the services —
        into the ``ChatRequest.sender`` provenance line the model sees.

        ``transcribed`` says the message arrived as SPEECH and is a machine
        transcription (satellite /listen, Telegram voice note): the executor
        adds a turn-scoped system note so a garbled transcript earns a short
        "please repeat" instead of a best guess.

        Raises on failure (a bad agent id, a model that is down, a turn that
        exceeds CHAT_TIMEOUT); the caller turns that into a message to the user.
        """
        state = self._state
        sender = ""
        # getattr, not an import: services.py imports this module, and the
        # attribute is only there once register() completed.
        svc = getattr(state, "connectors", None)
        if svc is not None and (sender_id or sender_username or sender_name):
            try:
                sender = svc.sender_display(source or "", sender_id,
                                            sender_username, sender_name)
            except Exception as e:
                log.warning("sender lookup failed: %s", e)  # provenance is best-effort
        req = ChatRequest(
            agent_id=agent_id,
            message=message,
            session_id=session_id,
            attachments=attachments or [],
            source=source,
            sender=sender or None,
            transcribed=transcribed,
        )
        async with self._turns:
            executor = await AgentExecutor.create_for_agent(
                agent_id, state.tool_registry, state.stores
            )
            response = await asyncio.wait_for(
                run_channel_turn(req, state.named_sessions, executor),
                timeout=config.CHAT_TIMEOUT,
            )
        return response.reply, collect_resources(response.trace)

    async def transcribe(self, content: bytes, name: str,
                         language: str | None = None) -> str:
        """Transcribe a voice message, via the bundled document_extract tool.

        The plugin used to carry its own faster-whisper wrapper. Running it here
        would put a heavy native library (ctranslate2) inside the agent's own
        process, where a segfault takes down every chat — precisely the isolation
        a separate service used to provide for free. document_extract already
        does the identical job (same ffmpeg normalization, same model, same
        MYAGENT_WHISPER_MODEL) and the registry runs it as a subprocess with a
        timeout and a guaranteed kill, so this both deletes a duplicate
        implementation and buys the isolation back.

        Called directly rather than through the agent's granted tools: this is
        transport plumbing, like the executor writing attachments to disk, not
        something the model decided to do.
        """
        path = store_attachment(content, name, "audio")
        if not path:
            raise RuntimeError("could not store the audio file")
        args = {"path": path}
        if language:
            args["language"] = language
        out = await self._state.tool_registry.execute("document_extract", args)
        if out.startswith("ERROR"):
            raise RuntimeError(out[:200])
        # Output is "# <name>\n\n_Audio transcription…_\n\n<text>" — keep the text.
        parts = out.split("\n\n", 2)
        return (parts[2] if len(parts) > 2 else out).strip()

    async def reset_session(self, session_id: str) -> bool:
        """Clear a channel conversation, parking the old log in the web history
        (same thing DELETE /api/chat/sessions/{id} does)."""
        try:
            await self._state.named_sessions.archive_and_reset(session_id)
        except Exception as e:
            log.warning("reset of session %s failed: %s", session_id, e)
            return False
        return True
