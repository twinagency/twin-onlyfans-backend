"""Derive a provider observation's creator binding from immutable webhook evidence.

Legacy and backfill rows without provable webhook lineage remain unscoped for
human review. No timestamp, amount or fan-name matching is used.
"""
import hashlib
import json
from sqlalchemy import and_, select

from db.onlyfans_engine_models import OfFansConnection, OfFansWebhook, OfFansWebhookConflict

_EVENTS = {
    'unlock': {'messages.ppv.unlocked'},
    'transaction': {'transactions.new'},
    'chargeback': {'chargebacks.new', 'transactions.new'},
    'human_sent': {'messages.sent'},
}


def webhook_binding(session, evidence):
    if evidence.source != 'webhook' or not evidence.event_key or evidence.kind not in _EVENTS:
        return None
    rows = session.execute(select(OfFansWebhook, OfFansConnection.binding_id).join(OfFansConnection, and_(
        OfFansConnection.account_id == OfFansWebhook.account_id, OfFansConnection.epoch == OfFansWebhook.epoch)).where(
        OfFansWebhook.account_id == evidence.account_id, OfFansWebhook.event_key == evidence.event_key,
        OfFansWebhook.event_type.in_(_EVENTS[evidence.kind]), OfFansWebhook.status == 'accepted')).all()
    matches = set()
    for webhook, binding in rows:
        raw = bytes(webhook.raw_body)
        if hashlib.sha256(raw).hexdigest() != webhook.payload_sha256:
            continue
        conflict = session.scalar(select(OfFansWebhookConflict.payload_sha256).where(
            OfFansWebhookConflict.account_id == webhook.account_id, OfFansWebhookConflict.epoch == webhook.epoch,
            OfFansWebhookConflict.event_key == webhook.event_key).limit(1))
        if conflict is not None:
            return None
        try:
            canonical = hashlib.sha256(json.dumps(json.loads(raw), sort_keys=True, separators=(',', ':'),
                                                  default=str).encode()).hexdigest()
        except (ValueError, TypeError, UnicodeError, RecursionError):
            continue
        if evidence.raw_sha256 in (webhook.payload_sha256, canonical):
            matches.add(binding)
    return next(iter(matches)) if len(matches) == 1 else None
