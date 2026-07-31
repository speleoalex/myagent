"""Channel discovery — the pluggable part of the plugin.

A channel is a directory beside this file holding a ``channel.json`` manifest and
the module it names:

    channels/<type>/
        channel.json     {"type","label","module","class","hints":{…},"handle":{…},
                          "url":{…},"device":{…}}
        channel.py       a BaseConnector subclass
        requirements.txt optional, installed by connectors/install.sh

Channels are **discovered, not imported by name**: nothing in the shared code
mentions a specific transport, so adding one is dropping in a folder. The manifest
carries what the rest of the system needs without importing the module — the label
and hint keys the UI renders, and the shape of a per-person handle
(``Contact.handles[<type>]``), which is what lets an agent be told "message
Alessandro on Telegram".

Failure handling mirrors the server's plugin loader (``server/app/plugins.py``):
a channel that cannot be loaded — a missing dependency, a malformed manifest — is
skipped with a warning and **reported** with its error rather than hidden, and it
never takes the plugin down with it.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from myagent_connectors.channels.base import BaseConnector
from myagent_connectors.core import CoreClient
from myagent_connectors.models import Binding
from myagent_connectors.storage import GrantStore

log = logging.getLogger("connectors.registry")

CHANNELS_DIR = Path(__file__).resolve().parent

# Prefix for the synthetic module names channels are registered under, so a
# channel folder cannot shadow a real dependency in sys.modules.
_MODULE_PREFIX = "myagent_connectors_channel_"


@dataclass
class Channel:
    """One discovered channel: its manifest, plus the class when it loaded."""

    type: str
    label: str = ""
    hints: dict = field(default_factory=dict)
    handle: dict = field(default_factory=dict)
    # Shape of Binding.url for channels where WE call the device (label +
    # example, like `handle`). Missing/empty = the UI hides the URL field.
    url: dict = field(default_factory=dict)
    # Declares that the far end is a DEVICE holding its own settings, which
    # /bindings/{id}/device can read and write ({"config": true, "voices":
    # true}). A flag rather than a form schema: the UI renders whatever the
    # device's own answer contains, so no channel type is named anywhere.
    device: dict = field(default_factory=dict)
    connector: type[BaseConnector] | None = None
    error: str = ""

    @property
    def loaded(self) -> bool:
        return self.connector is not None

    def info(self) -> dict:
        """What the UI needs: how to name the channel, which hint keys to show,
        and how to label a person's handle on it."""
        return {
            "type": self.type,
            "label": self.label or self.type,
            "hints": self.hints,
            "handle": self.handle,
            # None, not {}: the UI gates the URL field on this being truthy,
            # and an empty JS object is truthy.
            "url": self.url or None,
            "device": self.device or None,
            "loaded": self.loaded,
            "error": self.error,
        }


_channels: dict[str, Channel] = {}


def _enabled(name: str) -> bool:
    """Folders skipped on purpose. The ``.disabled`` suffix is the documented way
    to park a channel without deleting it, so it has to actually disable it."""
    return not (name.startswith((".", "_")) or name.endswith(".disabled"))


def _load_one(directory: Path) -> Channel:
    manifest = directory / "channel.json"
    try:
        meta = json.loads(manifest.read_text())
    except Exception as e:
        return Channel(type=directory.name, error=f"{type(e).__name__}: {e}")

    channel = Channel(
        type=meta.get("type") or directory.name,
        label=meta.get("label", ""),
        hints=meta.get("hints") or {},
        handle=meta.get("handle") or {},
        url=meta.get("url") or {},
        device=meta.get("device") or {},
    )
    try:
        entry = directory / (meta.get("module") or "channel.py")
        spec = importlib.util.spec_from_file_location(
            f"{_MODULE_PREFIX}{channel.type}", entry
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls = getattr(module, meta.get("class") or "", None)
        if not (isinstance(cls, type) and issubclass(cls, BaseConnector)):
            raise AttributeError(
                f"{entry.name} has no BaseConnector subclass named "
                f"{meta.get('class')!r}"
            )
        channel.connector = cls
    except Exception as e:
        channel.error = f"{type(e).__name__}: {e}"
        log.warning("Channel '%s' not loaded: %s", channel.type, channel.error)
        log.debug("Channel '%s' traceback", channel.type, exc_info=True)
    return channel


def discover() -> dict[str, Channel]:
    """Scan for channels once and remember the result."""
    if _channels:
        return _channels
    for directory in sorted(CHANNELS_DIR.iterdir()):
        if not directory.is_dir() or not _enabled(directory.name):
            continue
        if not (directory / "channel.json").is_file():
            continue
        found = _load_one(directory)
        _channels[found.type] = found
    if _channels:
        log.info("Channels: %s", ", ".join(
            f"{c.type}{'' if c.loaded else ' (FAILED)'}" for c in _channels.values()
        ))
    return _channels


def available_types() -> list[dict]:
    """The channels the UI may offer, manifest included. Only loaded ones: a
    broken channel must not be selectable in the bot form."""
    return [c.info() for c in discover().values() if c.loaded]


def all_channels() -> list[dict]:
    """Every discovered channel, broken ones included — for the status endpoint."""
    return [c.info() for c in discover().values()]


def get_channel(channel_type: str) -> Channel | None:
    return discover().get(channel_type)


def resolve_type(name: str) -> str:
    """Map what a human wrote ("telegram", "Telegram") to a channel type.

    Needed because an agent is told *"send it via Telegram"*: the label is what
    the user says, the type is what the data uses."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return ""
    for channel in discover().values():
        if wanted in (channel.type.lower(), (channel.label or "").lower()):
            return channel.type
    return ""


def create_connector(binding: Binding, client: CoreClient,
                     grants: GrantStore) -> BaseConnector:
    channel = get_channel(binding.type)
    if channel is None:
        raise ValueError(f"Unknown connector type: {binding.type}")
    if not channel.loaded:
        raise ValueError(
            f"Channel '{binding.type}' is installed but failed to load: {channel.error}"
        )
    return channel.connector(binding, client, grants)
