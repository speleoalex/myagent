"""ConnectorManager — one background task per enabled binding.

Owns the lifecycle of every connector: start all enabled bindings at server
startup, stop them on shutdown, and reconcile a single binding after a CRUD
change (start / stop / restart) without touching the others. A crashing
connector is isolated: its task records an error status but never brings the
manager (or sibling connectors) down.
"""
from __future__ import annotations

import asyncio
import logging

from app.channels.base import BaseConnector
from app.channels.registry import create_connector
from app.models import Binding
from app.myagent_client import MyAgentClient
from app.storage import BindingStore, GrantStore

log = logging.getLogger("connectors.manager")


class _Runner:
    def __init__(self, connector: BaseConnector):
        self.connector = connector
        self.task: asyncio.Task | None = None


class ConnectorManager:
    def __init__(self, bindings: BindingStore, grants: GrantStore,
                 client: MyAgentClient):
        self.bindings = bindings
        self.grants = grants
        self.client = client
        self.runners: dict[str, _Runner] = {}

    # ------------------------------------------------------------- lifecycle
    async def start_all(self) -> None:
        for data in self.bindings.list_all():
            b = Binding(**data)
            if b.enabled:
                await self._start(b)

    async def stop_all(self) -> None:
        for bid in list(self.runners):
            await self._stop(bid)

    async def reconcile(self, binding_id: str) -> None:
        """Bring the running state of one binding in line with its stored
        definition (called after create/update/delete)."""
        await self._stop(binding_id)
        data = self.bindings.get(binding_id)
        if data is None:
            return
        b = Binding(**data)
        if b.enabled and b.token:
            await self._start(b)

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
            log.warning("connector '%s' stopped with error: %s", binding_id, e)

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

    # ----------------------------------------------------------------- status
    def status(self, binding_id: str) -> dict:
        runner = self.runners.get(binding_id)
        if runner is None:
            return {"state": "stopped", "detail": "", "last_update": "", "messages": 0}
        return runner.connector.status.to_dict()
