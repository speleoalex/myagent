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

# "Send it to everyone." A reserved word rather than a second parameter: it rides
# along in the schema's enum for free, and a model that forgets a boolean flag
# cannot forget a value it was handed. Aliases because the user says "tutti" and
# the model echoes the word it read.
BROADCAST_WORD = "all"
BROADCAST_WORDS = frozenset({BROADCAST_WORD, "everyone", "tutti", "everybody"})


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

    def _channel_label(self, channel_type: str) -> str:
        channel = registry.get_channel(channel_type)
        return (channel.label if channel else channel_type) or channel_type

    def notify_targets(self) -> dict:
        """What the core needs to write the address book into a tool schema.

        One call because it feeds one place: the ``notify_user`` definition the
        executor narrows per turn. NAMES only — a handle is an id, and a model
        shown an id pastes it into ``chat_id`` and skips the lookup that makes the
        recipient verifiable. ``broadcast`` is here rather than hardcoded in the
        core so the vocabulary of "everyone" stays owned by the plugin that
        implements it.
        """
        return {
            "contacts": self.contact_names(),
            "channels": self.channel_labels(),
            "broadcast": BROADCAST_WORD,
        }

    def sender_display(self, channel_type: str, user_id: str = "",
                       username: str = "", name: str = "") -> str:
        """Who sent an inbound message, written for the agent's eyes.

        The reverse of ``resolve_recipients``: an id arrives from the transport
        and the address book turns it back into a person. Preference order —
        contact name (matched on the channel handle, id or @username), then the
        transport's own display name / @username, then the raw id, so the agent
        always gets SOMETHING it can repeat to notify_user or store in memory.
        Returns "" when there is nothing to say (no sender data at all).
        """
        handle = (user_id or "").strip()
        uname = (username or "").lstrip("@").strip().lower()
        who = ""
        for c in self._contacts():
            h = (c.handle_for(channel_type) or "").strip()
            if h and (h == handle or (uname and h.lstrip("@").lower() == uname)):
                who = c.name or c.id
                break
        if not who:
            who = ((name or "").strip()
                   or (f"@{uname}" if uname else "")
                   or (f"id {handle}" if handle else ""))
        if not who:
            return ""
        return f"{who} via {self._channel_label(channel_type)}"

    def _pick_binding(self, channel_type: str,
                      binding_id: str) -> tuple[Binding | None, str]:
        """Which bot sends. ``binding_id`` wins when given — without it there is no
        answer to the "several bots" error below, which is what made ``to=`` unusable
        on a server with two bots."""
        candidates = self._bindings_of(channel_type)
        if not candidates:
            where = f" of type '{channel_type}'" if channel_type else ""
            return None, f"no enabled bot{where} is configured"
        if binding_id:
            for b in candidates:
                if b.id == binding_id:
                    return b, ""
            listed = ", ".join(b.id for b in candidates[:MAX_LISTED])
            return None, (f"no enabled bot has id '{binding_id}'. Enabled bots: "
                          f"{listed}")
        if len(candidates) > 1:
            listed = ", ".join(f"{b.id} ({b.type})" for b in candidates[:MAX_LISTED])
            return None, (f"several bots could send this — pass binding_id to choose: "
                          f"{listed}")
        return candidates[0], ""

    def _broadcast(self, binding: Binding,
                   contacts: list[Contact]) -> tuple[list[Recipient], str, str]:
        """Every contact reachable on this binding's channel.

        Contacts without a handle there are NAMED in the note. A broadcast is the
        one case where a partial success reads as a total one: the agent goes on to
        tell the user it warned everybody, and the person who was left out is the
        one who needed telling.
        """
        label = self._channel_label(binding.type)
        out, skipped = [], []
        for c in sorted(contacts, key=lambda c: (c.name or c.id).lower()):
            handle = c.handle_for(binding.type)
            if handle:
                out.append(Recipient(binding.id, handle, f"{c.name or c.id} on {label}"))
            else:
                skipped.append(c.name or c.id)
        if not out:
            if not skipped:
                return [], "the address book is empty, so there is nobody to notify", ""
            return [], (f"no contact has a {label} handle: "
                        f"{', '.join(skipped[:MAX_LISTED])}"), ""
        note = ""
        if skipped:
            note = (f"skipped for having no {label} handle: "
                    f"{', '.join(skipped[:MAX_LISTED])}")
        return out, "", note

    def _recipient(self, binding: Binding, contact: Contact) -> Recipient:
        return Recipient(binding.id, contact.handle_for(binding.type),
                         f"{contact.name or contact.id} "
                         f"on {self._channel_label(binding.type)}")

    def _reach(self, contact: Contact, channel_type: str,
               binding_id: str) -> tuple[list[Recipient], str, str]:
        """Which bot can actually reach this contact.

        The bot is picked AFTER the person, never before: a contact's handles are
        the statement of which channels they exist on, and an unrelated default
        bot must not decide that for them. Picking first is what made *"greet
        Notebook HP"* fail with "has no Telegram handle" — the agent's configured
        ``notify_binding_id`` (a Telegram bot) had already fixed the channel, so
        the satellite the contact actually lives on was never considered.

        ``binding_id`` therefore ranks as a preference, not a constraint: a name
        the caller chose is the intent, the transport is a detail. When the asked
        bot cannot reach that name and exactly one other can, it goes through the
        other and SAYS SO in the note — the alternative is a hard failure, and a
        silent substitution is exactly what the note exists to prevent.
        """
        who = contact.name or contact.id
        candidates = [b for b in self._bindings_of(channel_type)
                      if contact.handle_for(b.type)]
        # WITHIN one channel, a handle that IS a binding id names its own bot, so
        # there is nothing left to disambiguate. That is how device channels work
        # — one binding is one device and the address book stores exactly that id
        # (satellite: chat_id ≡ binding id) — and filtering by TYPE alone left
        # both devices as candidates, so with two satellites every device
        # notification failed as "ambiguous". Inert for a chat channel, where a
        # handle is a chat id that cannot collide with a binding id.
        #
        # Per type, never across: a contact on Telegram AND on a speaker is a
        # genuine question ("which one?"), and answering it by coin flip is what
        # the ambiguity error below exists to prevent.
        by_type: dict[str, list[Binding]] = {}
        for b in candidates:
            by_type.setdefault(b.type, []).append(b)
        candidates = []
        for channel, bindings in by_type.items():
            named = [b for b in bindings if b.id == contact.handle_for(channel)]
            candidates += named or bindings
        if not candidates:
            if channel_type:
                # Deliberately distinct from "not found": the fix is to add a
                # handle, not to try another name.
                return [], (f"{who} has no {self._channel_label(channel_type)} "
                            f"handle. Add one in the address book, or pass "
                            f"chat_id explicitly"), ""
            if not contact.handles:
                return [], (f"{who} has no messaging handle in the address book. "
                            f"Add one, or pass chat_id explicitly"), ""
            listed = ", ".join(self._channel_label(t) for t in sorted(contact.handles))
            return [], (f"no enabled bot can reach {who}, who is only on: {listed}. "
                        f"Enable a bot there, or pass chat_id explicitly"), ""

        if binding_id:
            exact = next((b for b in candidates if b.id == binding_id), None)
            if exact:
                return [self._recipient(exact, contact)], "", ""
            if len(candidates) == 1:
                chosen = candidates[0]
                return [self._recipient(chosen, contact)], "", (
                    f"'{binding_id}' has no handle for {who}, so it went through "
                    f"'{chosen.id}' ({self._channel_label(chosen.type)}) instead")
            listed = ", ".join(f"{b.id} ({b.type})" for b in candidates[:MAX_LISTED])
            return [], (f"'{binding_id}' cannot reach {who}, but others can — "
                        f"pass binding_id to choose: {listed}"), ""
        if len(candidates) > 1:
            listed = ", ".join(f"{b.id} ({b.type})" for b in candidates[:MAX_LISTED])
            return [], (f"several bots could reach {who} — pass binding_id or "
                        f"channel to choose: {listed}"), ""
        return [self._recipient(candidates[0], contact)], "", ""

    def resolve_recipients(self, name: str = "", channel: str = "",
                           binding_id: str = "") -> tuple[list[Recipient], str, str]:
        """Turn a person's name (and optionally a channel) into destinations.

        Returns ``(recipients, error, note)``. On failure the error is written FOR
        THE MODEL: it says what was ambiguous and lists the candidates, because the
        caller's next move is to pick one. Nothing raises — an exception here
        would surface as a tool crash instead of a correctable answer. The note is
        for a send that only partly matched the ask — see ``_broadcast``.

        A named contact is resolved BEFORE any bot is chosen (``_reach``); the
        paths that have no name to go on — a broadcast, a raw id, nothing at all —
        still need a bot up front, and pick one with ``_pick_binding``.
        """
        channel_type = ""
        if channel:
            channel_type = registry.resolve_type(channel)
            if not channel_type:
                return [], (f"unknown channel '{channel}'. Available: "
                            f"{', '.join(self.channel_labels()) or 'none'}"), ""

        target = (name or "").strip()
        contacts = self._contacts()

        # "everyone". Only when no contact answers to that name: explicit data
        # beats a reserved word, so an address book holding an "All" (or an
        # "Allison", which prefix-matches) keeps addressing the person.
        is_broadcast = (target.lower() in BROADCAST_WORDS
                        and not any(_name_matches(target, c.name or c.id)
                                    for c in contacts))

        # An id or @username goes through untouched: the address book is a
        # convenience, not a gate.
        if target and not is_broadcast and not _looks_like_handle(target):
            matches = [c for c in contacts if _name_matches(target, c.name or c.id)]
            if not matches:
                known = (", ".join(self.contact_names()[:MAX_LISTED])
                         or "the address book is empty")
                return [], f"no contact matches '{target}'. Known contacts: {known}", ""
            if len(matches) > 1:
                listed = ", ".join((c.name or c.id) for c in matches[:MAX_LISTED])
                return [], (f"'{target}' matches several contacts — be more "
                            f"specific: {listed}"), ""
            return self._reach(matches[0], channel_type, binding_id)

        binding, error = self._pick_binding(channel_type, binding_id)
        if binding is None:
            return [], error, ""
        if not target:
            return [], ("no recipient. Pass 'to' with a name from the address book "
                        f"({', '.join(self.contact_names()[:MAX_LISTED]) or 'empty'}) "
                        "or an explicit chat_id"), ""
        if is_broadcast:
            return self._broadcast(binding, contacts)
        return [Recipient(binding.id, target, target)], "", ""


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
