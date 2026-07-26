"""Connector type registry — the pluggable part of the framework.

Add a new messaging channel by implementing a BaseConnector subclass and
registering it here under its ``type`` string. Nothing else in the codebase
needs to change.
"""
from __future__ import annotations

from app.channels.base import BaseConnector
from app.channels.telegram import TelegramConnector
from app.models import Binding
from app.myagent_client import MyAgentClient
from app.storage import GrantStore

CONNECTOR_TYPES: dict[str, type[BaseConnector]] = {
    "telegram": TelegramConnector,
    # future: "slack": SlackConnector, "discord": DiscordConnector, ...
}


def available_types() -> list[str]:
    return sorted(CONNECTOR_TYPES.keys())


def create_connector(binding: Binding, client: MyAgentClient,
                     grants: GrantStore) -> BaseConnector:
    cls = CONNECTOR_TYPES.get(binding.type)
    if cls is None:
        raise ValueError(f"Unknown connector type: {binding.type}")
    return cls(binding, client, grants)
