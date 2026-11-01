"""Interface to the operator case/owner/status model (built in R1, a separate branch).

R3 and R7 only OPEN cases through ``open_operator_case``; they never define a case model, owner or status.
R1's implementation registers itself with ``register_operator_case_sink``. Until it does (and in tests),
calls are recorded in ``PENDING`` and return None, so nothing is silently dropped and nothing raises.

Dependency to resolve at integration: R1 must register a callable with this signature and return a
case id (or the id of an existing open case with the same ``dedupe_key``).
"""
from __future__ import annotations

from typing import Callable, Optional

CASE_KINDS = ("identity_question", "disputed_content", "unresolved_delivery", "unsupported_custom",
              "business_enquiry", "attachment_unreadable")
PENDING: list[dict] = []
_sink: Optional[Callable[..., Optional[str]]] = None


def register_operator_case_sink(fn) -> None:
    global _sink
    _sink = fn


def open_operator_case(*, account_id, fan_id, kind, reason, dedupe_key, evidence=None, source_message_id=None, sink=None):
    if kind not in CASE_KINDS:
        raise ValueError("unknown operator case kind")
    request = {"account_id": account_id, "fan_id": fan_id, "kind": kind, "reason": reason, "dedupe_key": dedupe_key,
               "evidence": evidence or {}, "source_message_id": source_message_id}
    target = sink or _sink
    if target is None:
        PENDING.append(request)
        return None
    return target(**request)


def durable_sink(session):
    """Use R1's existing durable case writer inside the caller's transaction."""
    def write(**request):
        from .operator_cases import open_case
        mapping = {"identity_question": ("identity", "all"), "disputed_content": ("complaint", "commercial"),
                   "unresolved_delivery": ("complaint", "commercial"), "unsupported_custom": ("custom_request", "offer"),
                   "business_enquiry": ("business", "all"), "attachment_unreadable": ("attachment", "offer")}
        category, scope = mapping[request["kind"]]
        case, _ = open_case(session, str(request["account_id"]), int(request["fan_id"]), category,
            reason=request["reason"], evidence={**request["evidence"], "dedupe_key": request["dedupe_key"]},
            source_message_id=request["source_message_id"], origin="detector", scope=scope)
        return case.case_id
    return write
