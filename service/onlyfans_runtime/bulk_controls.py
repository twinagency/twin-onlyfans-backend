"""Bounded bulk controls through the canonical per-fan authority; never sends."""
import hashlib
from sqlalchemy.orm import Session
from onlyfans_read.contract import conversation_id_for, fan_id_from_conversation_id
from onlyfans_read.agency_scope import actor_allowed
from .store import Refused

MAX_BATCH = 50
ACTIONS = {"resume_automation": "automated", "pause_automation": "paused"}


def validate_items(account_id, items):
    if type(items) is not list or not 1 <= len(items) <= MAX_BATCH:
        raise ValueError("Use between 1 and 50 conversations.")
    seen = set()
    for item in items:
        if type(item) is not dict or set(item) != {"conversationId", "expectedRevision"}:
            raise ValueError("Invalid conversation entry.")
        cid, revision = item["conversationId"], item["expectedRevision"]
        fan = fan_id_from_conversation_id(cid, account_id) if type(cid) is str else None
        if (fan is None or not 0 < fan < 2**63 or conversation_id_for(account_id, fan) != cid
                or cid in seen or type(revision) is not int or not 0 <= revision < 2**31-1):
            raise ValueError("Invalid conversation entry.")
        seen.add(cid)
    return items


def apply_batch(store, tenant, account, *, action, items, command_key, actor):
    if action not in ACTIONS or not actor_allowed(actor):
        raise Refused("management permission required")
    validate_items(account, items)
    # Check the complete account/agency scope before any write or per-fan result.
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
    results = []
    reasons = {
        "bulk_human_control": "human_control", "bulk_human_review": "human_review",
        "bulk_contact_blocked": "contact_blocked",
        "control revision conflict": "revision_changed",
        "unresolved delivery holds resumption": "delivery_unresolved",
        "recorded renewed consent is required": "contact_blocked",
        "a stronger restriction holds": "contact_blocked",
        "explicit hand-back is required": "human_control",
        "replies are not paused": "state_changed", "replies are not automated": "state_changed",
        "conversation unavailable": "conversation_unavailable",
    }
    for item in items:
        cid, revision = item["conversationId"], item["expectedRevision"]
        fan = fan_id_from_conversation_id(cid, account)
        key = hashlib.sha256(f"{command_key}:{action}:{cid}:{revision}".encode()).hexdigest()
        try:
            recorded = store.control(tenant, account, fan, mode=ACTIONS[action], action=action,
                expected_revision=revision, command_key=key, actor=actor,
                reason="Dashboard bulk operator: " + action, bulk=True)
        except Refused as exc:
            # Definite per-fan refusals do not discard the rest of an authorized batch.
            # Unexpected errors still fail the request, preserving an uncertain outcome.
            if str(exc) not in reasons:
                raise
            results.append({"conversationId": cid, "outcome": "skipped", "reason": reasons[str(exc)]})
        else:
            results.append({"conversationId": cid, "outcome": "recorded", "recordedRevision": recorded})
    return results
