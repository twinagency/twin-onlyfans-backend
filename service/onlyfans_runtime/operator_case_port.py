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


def open_operator_case(*, account_id, fan_id, kind, reason, dedupe_key, evidence=None, source_message_id=None):
    if kind not in CASE_KINDS:
        raise ValueError("unknown operator case kind")
    request = {"account_id": account_id, "fan_id": fan_id, "kind": kind, "reason": reason, "dedupe_key": dedupe_key,
               "evidence": evidence or {}, "source_message_id": source_message_id}
    if _sink is None:
        PENDING.append(request)
        return None
    return _sink(**request)
