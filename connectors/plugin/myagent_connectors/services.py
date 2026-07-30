"""The plugin's services, and the one way routers reach them.

Everything the plugin owns hangs off a single namespaced key,
``app.state.connectors``. ``app.state`` is shared with the core and with any
other plugin, so claiming generic names there (``bindings``, ``contacts``,
``manager``) would be a collision waiting for the second plugin.

This is also the seam the core's ``notify_user`` tool uses to turn *"message
Alessandro on Telegram"* into an actual chat id — see ``resolve_recipients``.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.storage.store import JsonStore

from myagent_connectors.channels import registry
from myagent_connectors.channels.manager import ConnectorManager
from myagent_connectors.core import CoreClient
from myagent_connectors.models import Binding, Contact
from myagent_connectors.storage import GrantStore

STATE_KEY = "connectors"

# How many candidates an error message lists. The address book can grow, and this
# text goes into a model's context — same reasoning as the executor's cap on the
# Available Agents directory.
MAX_LISTED = 12


@dataclass
class Recipient:
    """One resolved destination: which bot sends, and to which chat."""

    binding_id: str
    chat_id: str
    display: str  # e.g. "Alessandro Vernassa on Telegram" — for the tool's answer


def _looks_like_handle(value: str) -> bool:
    """A raw identifier rather than a person's name: digits (Telegram/phone) or
    an @username. Lets the caller pass an id straight through."""
    v = (value or "").strip()
    return bool(v) and (v.startswith(("@", "+")) or v.lstrip("-").isdigit())


def _name_matches(query: str, name: str) -> bool:
    """Exact, then prefix, then single word — because a person says "Alessandro"
    and the address book holds "Alessandro Vernassa"."""
    q, n = query.strip().lower(), (name or "").strip().lower()
    if not q or not n:
        return False
    return q == n or n.startswith(q) or q in n.split()


@dataclass
class Connectors:
    bindings: JsonStore
    contacts: JsonStore
    grants: GrantStore
    core: CoreClient
    manager: ConnectorManager

    # ------------------------------------------------------- recipient lookup
    def _bindings_of(self, channel_type: str) -> list[Binding]:
        out = []
        for data in self.bindings.list_all():
            try:
                b = Binding(**data)
            except Exception:
                continue
            if b.enabled and (not channel_type or b.type == channel_type):
                out.append(b)
        return out

    def _contacts(self) -> list[Contact]:
        out = []
        for data in self.contacts.list_all():
            try:
                out.append(Contact(**data))
            except Exception:
                continue
        return out

    def contact_names(self) -> list[str]:
        return sorted(c.name or c.id for c in self._contacts())

    def channel_labels(self) -> list[str]:
        return [c["label"] for c in registry.available_types()]

    def resolve_recipients(self, name: str = "",
                           channel: str = "") -> tuple[list[Recipient], str]:
        """Turn a person's name (and optionally a channel) into destinations.

        Returns ``(recipients, error)``. On failure the error is written FOR THE
        MODEL: it says what was ambiguous and lists the candidates, because the
        caller's next move is to pick one. Nothing raises — an exception here
        would surface as a tool crash instead of a correctable answer.
        """
        channel_type = ""
        if channel:
            channel_type = registry.resolve_type(channel)
            if not channel_type:
                return [], (f"unknown channel '{channel}'. Available: "
                            f"{', '.join(self.channel_labels()) or 'none'}")

        candidates = self._bindings_of(channel_type)
        if not candidates:
            where = f" of type '{channel_type}'" if channel_type else ""
            return [], f"no enabled bot{where} is configured"
        if len(candidates) > 1:
            listed = ", ".join(f"{b.id} ({b.type})" for b in candidates[:MAX_LISTED])
            return [], (f"several bots could send this — pass binding_id to choose: "
                        f"{listed}")
        binding = candidates[0]

        target = (name or "").strip()
        if not target:
            return [], ("no recipient. Pass 'to' with a name from the address book "
                        f"({', '.join(self.contact_names()[:MAX_LISTED]) or 'empty'}) "
                        "or an explicit chat_id")

        # An id or @username goes through untouched: the address book is a
        # convenience, not a gate.
        if _looks_like_handle(target):
            return [Recipient(binding.id, target, target)], ""

        matches = [c for c in self._contacts() if _name_matches(target, c.name or c.id)]
        if not matches:
            known = ", ".join(self.contact_names()[:MAX_LISTED]) or "the address book is empty"
            return [], f"no contact matches '{target}'. Known contacts: {known}"
        if len(matches) > 1:
            listed = ", ".join((c.name or c.id) for c in matches[:MAX_LISTED])
            return [], f"'{target}' matches several contacts — be more specific: {listed}"

        contact = matches[0]
        handle = contact.handle_for(binding.type)
        if not handle:
            label = registry.get_channel(binding.type)
            label = (label.label if label else binding.type) or binding.type
            # Deliberately distinct from "not found": the fix is to add a handle,
            # not to try another name.
            return [], (f"{contact.name or contact.id} has no {label} handle. "
                        f"Add one in the address book, or pass chat_id explicitly")
        return [Recipient(binding.id, handle,
                          f"{contact.name or contact.id} on {binding.type}")], ""


def services(request: Request) -> Connectors:
    """The plugin's services for this request.

    The 503 is a safety net, not the normal "plugin not installed" path: when
    the plugin isn't loaded these routes don't exist at all and FastAPI answers
    404. This only fires if register() somehow half-completed.
    """
    state = getattr(request.app.state, STATE_KEY, None)
    if state is None:
        raise HTTPException(503, "Connectors plugin is not available")
    return state
