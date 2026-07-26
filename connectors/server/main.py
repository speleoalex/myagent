"""MyAgent Connectors — standalone messaging-bridge server.

Runs independently of myagent and talks to it only over HTTP. Owns the bot
bindings, their admin UI, and one polling task per enabled binding.

    python server/main.py            # dev
    MYAGENT_API_URL=http://host:8888 python server/main.py

Config via env (see app/config.py): MYAGENT_API_URL, MYAGENT_CONNECTORS_PORT, ...
"""
from __future__ import annotations

import contextlib
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.channels.manager import ConnectorManager
from app.myagent_client import MyAgentClient
from app.routers import bindings
from app.storage import BindingStore, GrantStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("connectors")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Polling tasks must live inside the running event loop -> start here,
    # not at import time.
    log.info("Connecting bots to myagent at %s", config.MYAGENT_API_URL)
    await app.state.manager.start_all()
    try:
        yield
    finally:
        log.info("Stopping connectors…")
        await app.state.manager.stop_all()


app = FastAPI(title="MyAgent Connectors", version="0.1.0", lifespan=lifespan)

config.ensure_dirs()
log.info("Connectors state directory: %s", config.STATE_DIR)

bindings_store = BindingStore(config.BINDINGS_DIR)
grants_store = GrantStore(config.GRANTS_DIR)
myagent_client = MyAgentClient()
manager = ConnectorManager(bindings_store, grants_store, myagent_client)

app.state.bindings = bindings_store
app.state.grants = grants_store
app.state.myagent = myagent_client
app.state.manager = manager

app.include_router(bindings.router, prefix="/api/bindings", tags=["bindings"])

# Admin UI (static). Created on first run so the mount never fails.
config.UI_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=config.UI_DIR, html=True), name="ui")

if __name__ == "__main__":
    log.info("Starting MyAgent Connectors on http://%s:%s", config.HOST, config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT)
