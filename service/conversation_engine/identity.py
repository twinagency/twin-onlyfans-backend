"""Explicit identities for isolated conversation and submission decisions.

Provider connection identifiers are opaque and are not creator identifiers.
These values are data contracts, not a replacement for database authorization.
"""
from dataclasses import dataclass
import re


def require_identifier(value: str, name: str) -> None:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty opaque string")


@dataclass(frozen=True, slots=True)
class ConversationKey:
    tenant_id: str
    account_id: str
    creator_id: str
    fan_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "account_id", "creator_id", "fan_id"):
            require_identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ActionIdentity:
    conversation: ConversationKey
    binding_epoch: int
    provider: str
    connection_id: str
    action_id: str
    idempotency_key: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.conversation, ConversationKey):
            raise ValueError("conversation must be a ConversationKey")
        if type(self.binding_epoch) is not int or self.binding_epoch < 1:
            raise ValueError("binding_epoch must be a positive integer")
        for name in ("provider", "connection_id", "action_id", "idempotency_key"):
            require_identifier(getattr(self, name), name)
        if type(self.payload_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256):
            raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
