"""Sales permission transitions independent of purchase and delivery state.

The caller supplies a reviewed interpretation of an explicit refusal or buying
interest. This module does not classify text, decide prices or submit offers.
"""
from dataclasses import dataclass
from enum import Enum

from .identity import ConversationKey, require_identifier


class SalesMode(Enum):
    OPEN = "open"
    PAUSED = "paused"
    REOPENED_SCOPED = "reopened_scoped"


@dataclass(frozen=True, slots=True)
class SalesPermission:
    conversation: ConversationKey
    mode: SalesMode = SalesMode.OPEN
    source_event_id: str | None = None
    item_scope: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.conversation, ConversationKey) or not isinstance(self.mode, SalesMode):
            raise ValueError("typed conversation and sales mode are required")
        if type(self.item_scope) is not frozenset:
            raise ValueError("item_scope must be an immutable set")
        for item in self.item_scope:
            require_identifier(item, "item scope")
        if self.mode is SalesMode.OPEN:
            if self.source_event_id is not None or self.item_scope:
                raise ValueError("open permission has no reopening scope")
        else:
            require_identifier(self.source_event_id, "source_event_id")
            if (self.mode is SalesMode.PAUSED and self.item_scope) or (
                self.mode is SalesMode.REOPENED_SCOPED and not self.item_scope
            ):
                raise ValueError("sales scope does not match its mode")

    def decline(self, source_event_id: str) -> "SalesPermission":
        return SalesPermission(self.conversation, SalesMode.PAUSED, source_event_id)

    def reopen_for(self, source_event_id: str, item_ids: frozenset[str]) -> "SalesPermission":
        """Record explicitly renewed interest only within the supplied scope."""
        if self.mode is not SalesMode.PAUSED:
            raise ValueError("only paused selling can be reopened")
        return SalesPermission(self.conversation, SalesMode.REOPENED_SCOPED, source_event_id, item_ids)

    def permits_offer(self, conversation: ConversationKey, item_id: str) -> bool:
        require_identifier(item_id, "item_id")
        if conversation != self.conversation or self.mode is SalesMode.PAUSED:
            return False
        return self.mode is SalesMode.OPEN or item_id in self.item_scope
