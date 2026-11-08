"""Application-proven independent replies, including a narrow supervised Fans API case.

The first proof class is bounded integer arithmetic: the application computes
and validates the *entire* response from the current canonical message and
compares it with a narrowly classified pending action. A model
claim, a changed topic, or an unverified fact is never an independence proof.
The proof and the second admission are durable and bind to the original action.
"""
from hashlib import sha256
import re
from sqlalchemy import select
from db.models import Message, OfMessageDelivery
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import (OfEngineAttempt, OfEngineIndependenceProof,
    OfEngineIndependentAdmission, OfEngineRequest, OfEngineDecision)
from onlyfans_runtime.store import Store, utcnow, UNRESOLVED_OUTCOMES

_ARITHMETIC_REPLY = re.compile(r'^(-?[0-9]{1,6}) \+ (-?[0-9]{1,6}) = (-?[0-9]{1,7})\.$')
_NEUTRAL_ANCHORS = frozenset({'Hello.', 'Hi.', 'Thanks for your message.', 'First answer.'})

_IDENTITY = re.compile(r'^\s*(?:are you (?:an? )?(?:ai|bot|artificial intelligence)|is this an? (?:ai|bot))\s*[?.!]*\s*$', re.IGNORECASE)
_IDENTITY_REPLY = 'I am an AI assistant.'

_ARITHMETIC = re.compile(r'^\s*(?:what is\s+)?(-?[0-9]{1,6})\s*\+\s*(-?[0-9]{1,6})\s*\??\s*$', re.IGNORECASE)


def _hash(value):
    return sha256(value.encode('utf-8')).hexdigest()


def arithmetic_answer(message):
    if type(message) is not str:
        return None
    match = _ARITHMETIC.fullmatch(message)
    if match is None:
        return None
    left, right = map(int, match.groups())
    return f'{left} + {right} = {left + right}.'


def verified_answer(message, *, account=None, policy=None, s=None):
    if type(message) is str and _IDENTITY.fullmatch(message):
        if policy is not None and policy.transport_kind == 'fansapi':
            from .writer_context import active_profile
            profile = active_profile(account, session=s)
            answer = profile.role_statement.strip() if profile is not None else ''
            return ('identity.v1', answer) if answer else (None, None)
        return 'identity.v1', _IDENTITY_REPLY
    # Arithmetic remains a synthetic-lab proof; a Fans API worker only uses
    # the creator-approved exact identity wording for its narrow first case.
    if policy is not None and policy.transport_kind == 'fansapi':
        return None, None
    answer = arithmetic_answer(message)
    return ('arithmetic.v1', answer) if answer is not None else (None, None)


def _anchor(s, runtime, fan_id, state, policy, *, own_intent=None):
    if (state is None or policy is None or policy.transport_kind not in {'synthetic', 'fansapi'}
            or policy.delivery_mode != 'gap' or state.unresolved_attempt_id is None):
        return None
    if policy.transport_kind == 'fansapi':
        # Fans API uses a per-fan pointer rather than the account-wide slot.
        anchor = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == runtime.account_id,
            OfTextIntent.fan_id == fan_id, OfTextIntent.attempt_id == state.unresolved_attempt_id))
    else:
        if policy.active_intent_id is None:
            return None
        anchor = s.get(OfTextIntent, (runtime.account_id, policy.active_intent_id))
    if (anchor is None or anchor.fan_id != fan_id or anchor.binding_id != runtime.binding_id
            or anchor.attempt_id != state.unresolved_attempt_id or anchor.status not in {'unknown', 'accepted'}
            or not anchor.reservation_held or anchor.wire_started_at is None):
        return None
    attempt = s.get(OfEngineAttempt, (runtime.account_id, anchor.attempt_id))
    if attempt is None or attempt.intent_id != anchor.intent_id or attempt.outcome not in {'unknown', 'accepted'}:
        return None
    if Store._native_unresolved(s, runtime.account_id, fan_id):
        return None
    if s.scalar(select(OfMessageDelivery.message_id).where(OfMessageDelivery.account_id == runtime.account_id,
        OfMessageDelivery.fan_id == fan_id, OfMessageDelivery.outcome.in_(UNRESOLVED_OUTCOMES)).limit(1)) is not None:
        return None
    others = select(OfTextIntent.intent_id).where(OfTextIntent.account_id == runtime.account_id,
        OfTextIntent.status.in_(('unknown', 'dispatching')), OfTextIntent.intent_id != anchor.intent_id)
    if policy.transport_kind == 'fansapi':
        others = others.where(OfTextIntent.fan_id == fan_id)
    if own_intent is not None:
        others = others.where(OfTextIntent.intent_id != own_intent)
    if s.scalar(others.limit(1)) is not None:
        return None
    active = s.scalar(select(OfEngineIndependentAdmission).where(
        OfEngineIndependentAdmission.account_id == runtime.account_id,
        OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))))
    if active is not None and (own_intent is None or active.intent_id != own_intent):
        return None
    return anchor


def _noninterfering_anchor(s, runtime, anchor, answer):
    # No inference from a different topic: classify the *entire* pending
    # payload as a neutral fixture acknowledgement or another exact factual
    # calculation. Every other pending action stays held for reconciliation.
    old = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == runtime.account_id,
        OfEngineDecision.intent_id == anchor.intent_id))
    if (old is None or old.action != 'reply' or old.body != anchor.body or
            anchor.media_json != '[]' or anchor.preview_json != '[]' or anchor.price_cents != 0):
        return False
    if anchor.body in _NEUTRAL_ANCHORS:
        return True
    previous = _ARITHMETIC_REPLY.fullmatch(anchor.body)
    current = _ARITHMETIC_REPLY.fullmatch(answer)
    if previous is None or current is None:
        return False
    left, right, result = map(int, previous.groups())
    next_left, next_right, next_result = map(int, current.groups())
    return (left + right == result and next_left + next_right == next_result and
            sorted((left, right)) != sorted((next_left, next_right)))


def can_prepare_identity(s, runtime, fan_id, source_message_id, state, policy):
    # Only the exact approved identity answer after a neutral unknown reply.
    if policy is None or policy.transport_kind != 'fansapi' or source_message_id is None:
        return False
    source = s.get(Message, (runtime.account_id, fan_id, source_message_id))
    if source is None or source.direction != 'in' or source.is_unsent:
        return False
    kind, answer = verified_answer(source.body, account=runtime.account_id, policy=policy, s=s)
    anchor = _anchor(s, runtime, fan_id, state, policy)
    return bool(kind == 'identity.v1' and answer and anchor is not None
                and _noninterfering_anchor(s, runtime, anchor, answer))


def create_proof(s, runtime, decision, source, state, policy, *, extra_parts=False):
    kind, answer = verified_answer(source.body, account=runtime.account_id, policy=policy, s=s)
    if policy is not None and policy.transport_kind == 'fansapi' and extra_parts:
        return None
    if (decision.action != 'reply' or answer is None or decision.body != answer
            or source.direction != 'in' or source.is_unsent):
        return None
    anchor = _anchor(s, runtime, decision.fan_id, state, policy)
    if anchor is None or not _noninterfering_anchor(s, runtime, anchor, answer):
        return None
    proof = OfEngineIndependenceProof(account_id=runtime.account_id, decision_id=decision.decision_id,
        fan_id=decision.fan_id, source_message_id=source.message_id, anchor_intent_id=anchor.intent_id,
        kind=kind, source_sha256=_hash(source.body), answer_sha256=_hash(answer), created_at=utcnow())
    s.add(proof)
    return proof


def certified_anchor(s, runtime, decision, state, policy, *, intent=None):
    if decision is None or decision.action != 'reply':
        return None
    proof = s.get(OfEngineIndependenceProof, (runtime.account_id, decision.decision_id))
    if proof is None or proof.kind not in {'arithmetic.v1', 'identity.v1'} or proof.fan_id != decision.fan_id:
        return None
    request = s.get(OfEngineRequest, (runtime.account_id, decision.request_id))
    if request is None or request.source_message_id != proof.source_message_id:
        return None
    source = s.get(Message, (runtime.account_id, decision.fan_id, proof.source_message_id))
    if (source is None or source.direction != 'in' or source.is_unsent
            or _hash(source.body) != proof.source_sha256
            or verified_answer(source.body, account=runtime.account_id, policy=policy, s=s) != (proof.kind, decision.body)
            or _hash(decision.body) != proof.answer_sha256):
        return None
    anchor = _anchor(s, runtime, decision.fan_id, state, policy,
                     own_intent=intent.intent_id if intent is not None else None)
    if (anchor is None or anchor.intent_id != proof.anchor_intent_id
            or not _noninterfering_anchor(s, runtime, anchor, decision.body)):
        return None
    return anchor


def certified_for_intent(s, runtime, intent, state, policy):
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == intent.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    return certified_anchor(s, runtime, decision, state, policy, intent=intent)


def admission_for(s, row):
    if row.attempt_id is None:
        return None
    admission = s.get(OfEngineIndependentAdmission, (row.account_id, row.attempt_id))
    if admission is None or admission.intent_id != row.intent_id or admission.fan_id != row.fan_id:
        return None
    return admission


def begin_admission(s, row, anchor):
    if anchor is None or row.attempt_id is None:
        raise ValueError('independent admission requires an anchor and attempt')
    s.add(OfEngineIndependentAdmission(account_id=row.account_id, attempt_id=row.attempt_id,
        intent_id=row.intent_id, anchor_intent_id=anchor.intent_id, fan_id=row.fan_id,
        status='dispatching', created_at=utcnow(), updated_at=utcnow()))


def retain_unknown(s, row):
    admission = admission_for(s, row)
    if admission is None or admission.status not in {'dispatching', 'unknown'}:
        return False
    admission.status, admission.updated_at = 'unknown', utcnow()
    return True


def settle_and_promote(s, row, policy, state):
    """Retain both attempts; transfer the single old pointer if needed."""
    own = admission_for(s, row)
    if own is not None and own.status in {'dispatching', 'unknown'}:
        own.status, own.updated_at = 'settled', utcnow()
    anchored = s.scalar(select(OfEngineIndependentAdmission).where(
        OfEngineIndependentAdmission.account_id == row.account_id,
        OfEngineIndependentAdmission.anchor_intent_id == row.intent_id,
        OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).with_for_update())
    if anchored is None:
        return None
    other = s.get(OfTextIntent, (row.account_id, anchored.intent_id))
    if (other is None or other.attempt_id != anchored.attempt_id or not other.reservation_held
            or other.status not in {'dispatching', 'unknown', 'accepted'}):
        raise RuntimeError('independent admission cannot be safely promoted')
    return other
