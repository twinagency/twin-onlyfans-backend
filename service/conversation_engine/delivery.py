"""Fencing and receipt projection for the existing TextAuthority.

This module does not submit externally or create a second outbox. It grants
only synthetic plain-text decisions, and fails closed on unknown ownership.
"""
import json
from sqlalchemy import select

from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision, OfEngineOwner, OfEngineRequest, OfEngineOfferGrant, OfFansConnection
from onlyfans_runtime.store import Refused, utcnow
from .webhook_conflict import conflicted_sources


def prepare(s, runtime, fan_id, origin, decision_id, body, catalog_item_id=None, quoted_price_cents=None):
    owner = s.get(OfEngineOwner, (runtime.account_id, fan_id))
    if decision_id is None:
        if owner is not None and (origin != 'manual' or owner.mode != 'human'):
            raise Refused('conversation owned by the new engine')
        return None
    decision = s.get(OfEngineDecision, (runtime.account_id, decision_id))
    state = s.get(OfConversationState, (runtime.account_id, fan_id))
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    if (origin != 'automated' or owner is None or owner.mode != 'active'
            or owner.binding_id != runtime.binding_id or decision is None
            or decision.fan_id != fan_id or decision.binding_id != runtime.binding_id
            or decision.owner_epoch != owner.epoch or decision.status != 'ready'
            or decision.action not in {'reply', 'offer'} or decision.body != body or decision.intent_id is not None
            or state is None or decision.control_revision != state.revision
            or policy is None or policy.transport_kind != 'synthetic'):
        raise Refused('engine decision is stale or unavailable')
    from .sequence import prior_state
    sequence_reason = prior_state(s, runtime, decision, state)
    if sequence_reason is not None:
        raise Refused('sequence ' + sequence_reason.replace('_', ' '))
    from .memory import current as memory_current
    if not memory_current(s, runtime, decision):
        raise Refused('fan memory changed or is unavailable for the decision')
    if decision.action == 'reply':
        if catalog_item_id is not None or quoted_price_cents is not None or decision.terms_json is not None:
            raise Refused('plain reply carries unexpected commercial terms')
    else:
        from .commerce import verified_terms
        grant = s.get(OfEngineOfferGrant, (runtime.account_id, decision.offer_grant_id))
        terms = verified_terms(s, runtime, owner, grant)
        if (decision.catalog_item_id != catalog_item_id or terms['price_cents'] != quoted_price_cents
                or json.loads(decision.terms_json or 'null') != terms):
            raise Refused('engine offer terms changed')
    if decision.connection_epoch is not None:
        connection = s.get(OfFansConnection, (runtime.account_id, decision.connection_epoch))
        if connection is None or connection.status != 'active' or connection.binding_id != runtime.binding_id:
            raise Refused('provider connection changed')
    request = s.get(OfEngineRequest, (runtime.account_id, decision.request_id))
    if request is None or request.fan_id != fan_id or request.binding_id != runtime.binding_id or request.status != 'open':
        raise Refused('engine request is unavailable')
    if (fan_id, request.source_message_id) in conflicted_sources(s, runtime.account_id):
        raise Refused('source webhook conflict')
    return decision


def attached(s, decision, intent):
    if decision.payload_hash != intent.payload_hash:
        raise Refused('engine action payload changed')
    s.flush()  # the existing intent is the only outgoing action record
    decision.intent_id, decision.status, decision.updated_at = intent.intent_id, 'queued', utcnow()
    if decision.action == 'offer':
        grant = s.get(OfEngineOfferGrant, (intent.account_id, decision.offer_grant_id), with_for_update=True)
        if grant is None or grant.used_intent_id is not None:
            raise Refused('offer authorization already used')
        grant.used_intent_id = intent.intent_id
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    request.next_step, request.updated_at = 'await_delivery', utcnow()
    request.revision += 1


def current(s, runtime, intent):
    """Return (allowed, is_engine_action); no value grants a network send."""
    owner = s.get(OfEngineOwner, (intent.account_id, intent.fan_id))
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if owner is None:
        return decision is None, False
    if owner.binding_id != runtime.binding_id:
        return False, decision is not None
    if intent.origin == 'manual':
        return owner.mode == 'human' and decision is None, False
    if decision is None:
        return False, False
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    connection = s.get(OfFansConnection, (intent.account_id, decision.connection_epoch)) if decision.connection_epoch is not None else None
    connection_valid = decision.connection_epoch is None or (connection is not None and connection.status == 'active' and connection.binding_id == runtime.binding_id)
    valid = (connection_valid and owner.mode == 'active' and decision.status == 'queued' and decision.action in {'reply', 'offer'}
             and decision.fan_id == intent.fan_id and decision.binding_id == runtime.binding_id
             and decision.owner_epoch == owner.epoch and decision.body == intent.body
             and decision.payload_hash == intent.payload_hash and request is not None
             and request.fan_id == intent.fan_id and request.status == 'open')
    if valid and (intent.fan_id, request.source_message_id) in conflicted_sources(s, intent.account_id):
        valid = False
    if valid:
        from .memory import current as memory_current
        valid = memory_current(s, runtime, decision)
    if valid and decision.action == 'offer':
        valid = media_current(s, runtime, intent) is True
    return bool(valid), True


def media_current(s, runtime, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return None
    if decision.action == 'reply':
        return (intent.catalog_item_id is None and intent.media_json == '[]' and
                intent.preview_json == '[]' and intent.price_cents == 0)
    from .commerce import verified_terms
    try:
        owner = s.get(OfEngineOwner, (intent.account_id, intent.fan_id))
        grant = s.get(OfEngineOfferGrant, (intent.account_id, decision.offer_grant_id))
        terms = verified_terms(s, runtime, owner, grant, intent_id=intent.intent_id)
        return (decision.catalog_item_id == intent.catalog_item_id == terms['item']['id']
                and json.loads(decision.terms_json or 'null') == terms
                and json.loads(intent.catalog_snapshot_json or 'null') == terms
                and json.loads(intent.media_json) == terms['media_ids']
                and json.loads(intent.preview_json) == terms['preview_ids']
                and intent.price_cents == terms['price_cents'])
    except (Refused, ValueError, TypeError):
        return False


def is_engine_intent(s, intent):
    return s.scalar(select(OfEngineDecision.decision_id).where(OfEngineDecision.account_id == intent.account_id,
                                                               OfEngineDecision.intent_id == intent.intent_id)) is not None


def control_changed(s, account_id, fan_id, state):
    owner = s.get(OfEngineOwner, (account_id, fan_id))
    if owner is None:
        return
    owner.epoch += 1
    owner.revision += 1
    if state.mode == 'automated' and state.takeover_state == 'none':
        # A hand-back (or an explicit resume) re-activates engine ownership under
        # the new epoch; earlier decisions carry the old epoch and stay stale.
        owner.mode = 'active'
    else:
        owner.mode = 'human' if state.mode == 'human_controlled' else 'paused'
    owner.updated_at = utcnow()


def unknown(s, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return
    attempt_settled(s, intent, 'unknown')
    decision.status, decision.updated_at = 'unknown', utcnow()
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    if request.status not in {'handover_pending', 'handover_accepted'}:
        request.status = 'waiting_provider'
    request.next_step, request.updated_at = 'reconcile_existing_action', utcnow()
    request.revision += 1


def completed(s, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return
    decision.status, decision.updated_at = 'completed', utcnow()
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    if request.status in {'handover_pending', 'handover_accepted'}:
        request.next_step = 'review_receipt'
    elif decision.action == 'offer':
        request.status, request.next_step = 'waiting_fan', 'await_purchase_or_question'
    else:
        remaining = s.scalar(select(OfEngineDecision.decision_id).where(OfEngineDecision.account_id == intent.account_id,
            OfEngineDecision.request_id == decision.request_id, OfEngineDecision.generation == decision.generation,
            OfEngineDecision.part_index > decision.part_index,
            OfEngineDecision.status != 'completed').limit(1))
        request.status, request.next_step = ('open', 'send_next_part') if remaining is not None else ('completed', 'none')
    request.updated_at = utcnow()
    request.revision += 1


def attempt_started(s, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return
    s.add(OfEngineAttempt(account_id=intent.account_id, attempt_id=intent.attempt_id, intent_id=intent.intent_id,
        outcome='dispatching', payload_hash=intent.payload_hash, started_at=utcnow(), updated_at=utcnow()))


def attempt_settled(s, intent, outcome, evidence_json=None):
    if intent.attempt_id is None:
        return
    attempt = s.get(OfEngineAttempt, (intent.account_id, intent.attempt_id))
    if attempt is None:
        return
    if attempt.intent_id != intent.intent_id or attempt.payload_hash != intent.payload_hash:
        raise Refused('engine attempt identity changed')
    attempt.outcome, attempt.evidence_json, attempt.updated_at = outcome, evidence_json, utcnow()


def not_accepted(s, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return
    decision.status, decision.updated_at = 'held', utcnow()
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    if request.status not in {'handover_pending', 'handover_accepted'}:
        request.status = 'open'
    request.next_step, request.updated_at = 'review_not_sent', utcnow()
    request.revision += 1


def unsent_cancelled(s, intent):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None:
        return
    decision.status, decision.updated_at = 'cancelled', utcnow()
    later = s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
        OfEngineDecision.request_id == decision.request_id, OfEngineDecision.generation == decision.generation,
        OfEngineDecision.part_index > decision.part_index,
        OfEngineDecision.status == 'ready')).all()
    for part in later:
        part.status, part.updated_at = 'cancelled', utcnow()
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    if request.status not in {'handover_pending', 'handover_accepted'}:
        request.status = 'open'
    conflicted = (decision.fan_id, request.source_message_id) in conflicted_sources(s, intent.account_id)
    request.next_step, request.updated_at = ('review_webhook_conflict' if conflicted else 'review_changed_conversation'), utcnow()
    request.revision += 1


def payment_observed(s, intent, kind):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is None or decision.action != 'offer':
        return
    request = s.get(OfEngineRequest, (intent.account_id, decision.request_id))
    if request is None:
        raise Refused('engine request unavailable')
    if request.status not in {'handover_pending', 'handover_accepted'}:
        request.status = 'waiting_provider' if kind == 'purchase' else 'open'
    request.next_step = 'verify_access' if kind == 'purchase' else 'review_refund_or_chargeback'
    request.updated_at = utcnow()
    request.revision += 1
