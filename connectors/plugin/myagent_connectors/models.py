from __future__ import annotations

from pydantic import BaseModel, field_validator, model_validator

# Binding ids become filenames and are used to derive channel session keys, so
# they share the core's single charset definition. This used to be a literal
# copy marked "keep in sync" — the plugin runs in myagent's process now, so it
# can just import it.
from app.ids import check_id


def _as_handle(value) -> str:
    """Normalize one messaging identifier to a string.

    Identifiers are strings, not integers, even when a channel's happen to look
    numeric: a phone number is E.164 (``+39…``) and a Slack user id is
    ``U024BE7LH``. They used to be typed ``int`` because Telegram was the only
    channel — which made the shared access-control code unusable for any second
    one. Ints are still accepted on read so existing files load untouched.
    """
    if value is None:
        return ""
    return str(value).strip()


class Binding(BaseModel):
    """One bot ↔ agent link, configured from the admin UI.

    A binding says: 'messages arriving on THIS bot are answered by THIS agent,
    for THESE users only'. The bot token is a secret (masked toward the UI).
    """
    id: str
    name: str = ""
    type: str = "telegram"          # pluggable connector type
    enabled: bool = True
    agent_id: str = ""              # myagent agent that answers

    token: str = ""                 # bot credentials (secret)

    # Base URL of the device, for channels where WE call THEM (e.g. a voice
    # satellite's /say + /health). Empty for polled channels like Telegram.
    # Not a secret: no masking.
    url: str = ""

    # Access control
    # Messaging user ids allowed in allowlist mode. Strings: see _as_handle.
    allowed_ids: list[str] = []
    access_mode: str = "allowlist"  # "allowlist" | "password" | "open"
    # @usernames allowed (allowlist mode), stored normalized: no leading '@',
    # lowercased. Less secure than ids (a user can change/release a username),
    # but convenient.
    allowed_usernames: list[str] = []
    password: str = ""              # activation secret (password mode)

    @field_validator("allowed_ids", mode="before")
    @classmethod
    def normalize_ids(cls, v):
        """Coerce to strings, so a stored ``[123456789, …]`` (written when these
        were ints) loads without rewriting the file."""
        if not isinstance(v, (list, tuple)):
            return v
        return [h for h in (_as_handle(x) for x in v) if h]

    @field_validator("allowed_usernames")
    @classmethod
    def normalize_usernames(cls, v: list[str]) -> list[str]:
        return [u.lstrip("@").lower() for u in v if u and u.strip()]

    # Channel-scoped session key prefix on myagent. Empty -> derived from id.
    # Final key sent to myagent: "<prefix>_<chat_id>".
    session_prefix: str = ""

    # Optional canned texts
    welcome: str = ""
    help_text: str = ""

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return check_id(v)

    @field_validator("session_prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        return "" if not v else check_id(v)

    def effective_prefix(self) -> str:
        return self.session_prefix or self.id


class Contact(BaseModel):
    """Address-book entry: a person and how to reach them on each channel.

    This is what lets an agent be told *"message Alessandro on Telegram"*: the
    name is the human key, ``handles`` maps a channel type to that person's
    identifier there. One identifier was never enough — the same person has a
    Telegram id AND a phone number — which is why the original ``user_id`` /
    ``username`` pair is folded into ``handles`` on load.
    """
    id: str
    name: str = ""
    # channel type -> identifier on that channel, e.g. {"telegram": "123456789"}
    handles: dict[str, str] = {}
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_handles(cls, data):
        """Back-compat: contacts used to carry a single Telegram identifier as
        ``user_id`` (numeric) plus ``username``. Lift whichever is present into
        ``handles["telegram"]`` — same approach as ModelConfig.migrate_num_ctx,
        so no stored file has to be rewritten.
        """
        if not isinstance(data, dict):
            return data
        if "user_id" not in data and "username" not in data:
            return data
        handles = dict(data.get("handles") or {})
        if not handles.get("telegram"):
            # Prefer the numeric id: it is permanent, a username can be changed.
            legacy = _as_handle(data.get("user_id")) or _as_handle(data.get("username"))
            if legacy:
                handles["telegram"] = legacy
        return {k: v for k, v in data.items()
                if k not in ("user_id", "username")} | {"handles": handles}

    @field_validator("handles", mode="before")
    @classmethod
    def normalize_handles(cls, v):
        if not isinstance(v, dict):
            return v
        return {str(k): h for k, h in ((k, _as_handle(x)) for k, x in v.items()) if h}

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return check_id(v)

    def handle_for(self, channel_type: str) -> str:
        """This person's identifier on a channel, "" when they have none there."""
        return self.handles.get(channel_type, "")
