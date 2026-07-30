"""ConnectorManager — one background task per enabled binding.

Owns the lifecycle of every connector: start all enabled bindings at startup,
stop them on shutdown, and reconcile a single binding after a CRUD change
(start / stop / restart) without touching the others. A crashing connector is
isolated: its task records an error status but never brings the manager (or
sibling connectors) down.

Since these tasks now live in myagent's own process, two switches matter more
than they did as a separate service:

- ``reconcile`` is how bots are operated **hot**. Restarting the process to fix
  one bot would also kill in-flight web turns and the MCP child processes.
- ``set_enabled(False)`` is the plugin-wide kill switch, and it is PERSISTED.
  An in-memory flag would be undone by the next ``systemctl restart`` — which is
  exactly what someone does when a plugin is misbehaving.
"""
from __future__ import annotations

import asyncio
import json
import logging

from app.storage.sessions import write_json
from app.storage.store import JsonStore

from myagent_connectors import config
from myagent_connectors.channels.base import BaseConnector
from myagent_connectors.channels.registry import create_connector
from myagent_connectors.core import CoreClient
from myagent_connectors.models import Binding
from myagent_connectors.storage import GrantStore

log = logging.getLogger("connectors.manager")


class _Runner:
    def __init__(self, connector: BaseConnector):
        self.connector = connector
        self.task: asyncio.Task | None = None


class ConnectorManager:
    def __init__(self, bindings: JsonStore, grants: GrantStore,
                 client: CoreClient):
        self.bindings = bindings
        self.grants = grants
        self.client = client
        self.runners: dict[str, _Runner] = {}
        self.enabled = self._load_enabled()

    # ---------------------------------------------------------- kill switch
    def _load_enabled(self) -> bool:
        try:
            return bool(json.loads(config.STATE_FILE.read_text()).get("enabled", True))
        except (OSError, json.JSONDecodeError, AttributeError):
            return True

    async def set_enabled(self, enabled: bool) -> None:
        """Turn the whole plugin's traffic on or off, across restarts."""
        self.enabled = enabled
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        write_json(config.STATE_FILE, {"enabled": enabled})
        if enabled:
            await self.start_all()
        else:
            await self.stop_all()
        log.info("Connectors %s", "enabled" if enabled else "disabled (kill switch)")

    # ------------------------------------------------------------- lifecycle
    async def start_all(self) -> None:
        if not self.enabled:
            log.info("Connectors are disabled (kill switch): no bot started")
            return
        for data in self.bindings.list_all():
            try:
                b = Binding(**data)
            except Exception as e:
                log.warning("skipping malformed binding %r: %s", data.get("id"), e)
                continue
            if b.enabled:
                await self._start(b)

    async def stop_all(self) -> None:
        for bid in list(self.runners):
            await self._stop(bid)

    async def reconcile(self, binding_id: str) -> None:
        """Bring the running state of one binding in line with its stored
        definition (called after create/update/delete)."""
        await self._stop(binding_id)
        if not self.enabled:
            return
        data = self.bindings.get(binding_id)
        if data is None:
            return
        b = Binding(**data)
        if b.enabled and b.token:
            await self._start(b)

    async def resume(self, binding_id: str) -> None:
        """Restart a connector that paused itself after repeated failures.
        Same idea as the autonomy scheduler's resume: a paused connector stays
        down until someone says the underlying problem is fixed."""
        await self.reconcile(binding_id)

    # --------------------------------------------------------------- helpers
    async def _start(self, b: Binding) -> None:
        try:
            connector = create_connector(b, self.client, self.grants)
        except ValueError as e:
            log.warning("cannot create connector %s: %s", b.id, e)
            return
        runner = _Runner(connector)
        self.runners[b.id] = runner
        runner.task = asyncio.create_task(self._run(b.id, connector))

    async def _run(self, binding_id: str, connector: BaseConnector) -> None:
        try:
            await connector.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let one bad binding escape
            connector.status.state = "error"
            connector.status.detail = str(e)
            # exception(), not warning(): this is the only record of why a bot
            # died, and it is now buried in the agent's own journal.
            log.exception("connector '%s' stopped with error: %s", binding_id, e)

    async def _stop(self, binding_id: str) -> None:
        runner = self.runners.pop(binding_id, None)
        if runner is None:
            return
        try:
            await runner.connector.stop()
        except Exception:
            pass
        if runner.task and not runner.task.done():
            runner.task.cancel()
            try:
                await runner.task
            except (asyncio.CancelledError, Exception):
                pass

    def get_connector(self, binding_id: str) -> BaseConnector | None:
        """The running connector for a binding (None if it isn't running) —
        used by the notify_user tool for unsolicited outbound messages."""
        runner = self.runners.get(binding_id)
        return runner.connector if runner else None

    # ----------------------------------------------------------------- status
    def status(self, binding_id: str) -> dict:
        runner = self.runners.get(binding_id)
        if runner is None:
            return {"state": "stopped", "detail": "", "last_update": "",
                    "messages": 0, "errors": 0}
        return runner.connector.status.to_dict()

    def summary(self) -> dict:
        """Plugin-level state, for the UI's header and the kill switch."""
        states = [r.connector.status.state for r in self.runners.values()]
        return {
            "enabled": self.enabled,
            "bindings": len(self.bindings.list_all()),
            "running": sum(1 for s in states if s == "running"),
            "paused": sum(1 for s in states if s == "paused"),
        }
