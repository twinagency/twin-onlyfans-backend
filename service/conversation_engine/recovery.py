"""Conservative decisions from already authenticated attempt evidence.

No HTTP classification, sending, retries, storage or provider guarantees live
here. A caller must load the complete durable history, validate each evidence
source and apply the current owner, revision and commercial checks separately.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .identity import ActionIdentity, require_identifier


class Outcome(Enum):
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    IN_FLIGHT = "in_flight"
    ACCEPTED = "accepted"
    CONFLICT = "conflict"
    KEY_MISMATCH = "key_mismatch"


class NextStep(Enum):
    INVESTIGATE = "investigate"
    STOP = "stop"
    RECONCILE = "reconcile"
    PROJECT_RECEIPT = "project_receipt"
    HOLD = "hold"
    RETRY_SAME_ACTION = "retry_same_action"


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    identity: ActionIdentity
    attempt_id: str
    outcome: Outcome
    source: str
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ActionIdentity) or not isinstance(self.outcome, Outcome):
            raise ValueError("typed identity and outcome are required")
        require_identifier(self.attempt_id, "attempt_id")
        require_identifier(self.source, "source")
        if self.outcome is Outcome.ACCEPTED:
            require_identifier(self.receipt_id, "receipt_id")
        elif self.receipt_id is not None:
            raise ValueError("only accepted evidence may carry a receipt")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    next_step: NextStep
    reason: str


def decide_recovery(
    identity: ActionIdentity,
    evidence: Sequence[AttemptEvidence],
    *,
    history_complete: bool,
    approved_retry_budget_available: bool,
) -> RecoveryDecision:
    """Choose a next step without treating an absent record as proof.

    Retry eligibility is intentionally limited to complete histories in which
    every attempt is proven not sent. Unknown outcomes remain for reconciliation
    even if a provider might offer a bounded idempotency mechanism. The caller
    must separately enforce an approved retry budget and final-send controls.
    """
    if history_complete is not True or not evidence:
        return RecoveryDecision(NextStep.INVESTIGATE, "attempt_history_incomplete")
    unique: dict[str, AttemptEvidence] = {}
    for item in evidence:
        if not isinstance(item, AttemptEvidence) or item.identity != identity:
            return RecoveryDecision(NextStep.INVESTIGATE, "action_identity_mismatch")
        prior = unique.get(item.attempt_id)
        if prior is not None and prior != item:
            return RecoveryDecision(NextStep.INVESTIGATE, "conflicting_attempt_evidence")
        unique[item.attempt_id] = item
    outcomes = {item.outcome for item in unique.values()}
    if Outcome.KEY_MISMATCH in outcomes:
        return RecoveryDecision(NextStep.STOP, "idempotency_key_mismatch")
    if outcomes & {Outcome.UNKNOWN, Outcome.IN_FLIGHT, Outcome.CONFLICT}:
        return RecoveryDecision(NextStep.RECONCILE, "unresolved_attempt")
    receipts = {item.receipt_id for item in unique.values() if item.outcome is Outcome.ACCEPTED}
    if len(receipts) > 1:
        return RecoveryDecision(NextStep.RECONCILE, "multiple_accepted_receipts")
    if receipts:
        return RecoveryDecision(NextStep.PROJECT_RECEIPT, "accepted_receipt")
    if outcomes == {Outcome.NOT_SENT} and approved_retry_budget_available is True:
        return RecoveryDecision(NextStep.RETRY_SAME_ACTION, "all_attempts_proven_not_sent")
    return RecoveryDecision(NextStep.HOLD, "retry_not_authorized")
