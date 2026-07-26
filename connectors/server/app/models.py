from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

# Binding ids become filenames and are used to derive channel session keys on
# myagent, which validates the same charset — keep them compatible.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_id(v: str) -> str:
    if not _VALID_ID.match(v or "") or ".." in v:
        raise ValueError(
            "id may only contain letters, digits, dots, hyphens and underscores"
        )
    return v


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
        return _check_id(v)

    @field_validator("session_prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        return "" if not v else _check_id(v)

    def effective_prefix(self) -> str:
        return self.session_prefix or self.id
