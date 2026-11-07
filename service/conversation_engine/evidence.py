"""Represent the precise coverage needed to support an absence claim.

Source authority must come from an approved capability registry, which is not
implemented here. Coverage never manufactures an ownership or purchase record.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .identity import ConversationKey, require_identifier


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must have a time zone")


class CoverageStatus(Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    CONFLICTING = "conflicting"


@dataclass(frozen=True, slots=True)
class EvidenceTarget:
    conversation: ConversationKey
    binding_epoch: int
    item_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.conversation, ConversationKey):
            raise ValueError("conversation must be a ConversationKey")
        if type(self.binding_epoch) is not int or self.binding_epoch < 1:
            raise ValueError("binding_epoch must be a positive integer")
        require_identifier(self.item_version, "item_version")


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _aware(self.start, "start")
        _aware(self.end, "end")
        if self.start >= self.end:
            raise ValueError("coverage interval must be non-empty")


@dataclass(frozen=True, slots=True)
class AbsenceCoverage:
    target: EvidenceTarget
    window: EvidenceWindow
    source: str
    observed_at: datetime
    valid_until: datetime
    status: CoverageStatus
    source_authorized_for_absence: bool

    def __post_init__(self) -> None:
        if not isinstance(self.target, EvidenceTarget) or not isinstance(self.window, EvidenceWindow):
            raise ValueError("typed target and window are required")
        if not isinstance(self.status, CoverageStatus) or type(self.source_authorized_for_absence) is not bool:
            raise ValueError("typed status and source authority are required")
        require_identifier(self.source, "source")
        _aware(self.observed_at, "observed_at")
        _aware(self.valid_until, "valid_until")
        if self.observed_at < self.window.end or self.valid_until < self.observed_at:
            raise ValueError("coverage timestamps are inconsistent")

    def establishes_absence(
        self, target: EvidenceTarget, window: EvidenceWindow, *, now: datetime, records_found: bool
    ) -> bool:
        _aware(now, "now")
        return (
            records_found is False
            and self.source_authorized_for_absence is True
            and self.status is CoverageStatus.COMPLETE
            and target == self.target
            and self.window.start <= window.start < window.end <= self.window.end
            and self.observed_at <= now <= self.valid_until
        )
