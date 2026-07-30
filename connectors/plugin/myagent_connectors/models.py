from __future__ import annotations

from pydantic import BaseModel, field_validator

# Binding ids become filenames and are used to derive channel session keys, so
# they share the core's single charset definition. This used to be a literal
# copy marked "keep in sync" — the plugin runs in myagent's process now, so it
# can just import it.
from app.ids import check_id


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

    # Access control
    access_mode: str = "allowlist"  # "allowlist" | "password" | "open"
    allowed_ids: list[int] = []     # messaging user ids (allowlist mode)
    # @usernames allowed (allowlist mode), stored normalized: no leading '@',
    # lowercased. Less secure than ids (a user can change/release a username),
    # but convenient.
    allowed_usernames: list[str] = []
    password: str = ""              # activation secret (password mode)

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
    """Address-book entry: a person, with the messaging identifiers needed to
    authorize them on a binding (the authorized-users field picks from here)
    or to notify them. No secrets inside.
    """
    id: str
    name: str = ""
    user_id: int | None = None      # numeric messaging user id (permanent)
    username: str = ""              # stored normalized: no leading '@', lowercased
    notes: str = ""

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        return check_id(v)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        return v.lstrip("@").lower().strip()
