"""Payment evidence: store first, then link only what the provider names, then feed the canonical commerce owner.

Provider facts about a purchase:

* ``messages.ppv.unlocked``  names OUR sent message (``message_id``), the fan
  (``chat_id``; ``user_id`` is the CREATOR in that event) and the amount. That
  is message-bound evidence: it is checked against the accepted send, the fan,
  the frozen price and the offer grant, and retained as an unlock observation.
* ``transactions.new``       carries a provider transaction id and an amount but
  NO message id, and can be delayed by minutes.
* ``chargebacks.new``        names a transaction id, never a message.

A transaction or chargeback is NEVER linked to an unlock merely because the
fan, amount and time happen to fit (that is a coincidence, not a link). They
stay separate, unlinked evidence rows (status ``pending``, reason
``unlinked_transaction`` / ``unlinked_reversal``) that a person can reconcile.
An unlinked reversal flags that fan for human review; it never revokes access
by guesswork. A mismatched unlock amount, a missing grant or a wrong fan is held
for review and is never granted. Missing evidence is never recorded as "not
purchased".
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import OfConversationState, OfCreatorBinding
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOfferGrant
from db.onlyfans_fans_intake_models import OfFansEvidence
from onlyfans_runtime.store import Refused

_TXID = re.compile(r'[A-Za-z0-9._:-]{1,64}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def cents(value):
    if type(value) is bool or value is None or type(value) is str:
        raise Refused('invalid provider amount')
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise Refused('invalid provider amount') from exc
    scaled = amount * 100
    if not scaled.is_finite() or scaled != scaled.to_integral_value() or not 0 < scaled < 10**10:
        raise Refused('provider amount is not whole cents')
    return int(scaled)


def stamp(value):
    """Provider timestamp -> naive UTC (a value without an offset is documented UTC)."""
    if type(value) is not str or not value.strip():
        raise Refused('provider timestamp is missing')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError as exc:
        raise Refused('provider timestamp is invalid') from exc
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def numeric(value):
    if type(value) is int and 0 < value < 2**63:
        return value
    if type(value) is str and value.isascii() and value.isdecimal() and 1 <= len(value) <= 19 and 0 < int(value) < 2**63:
        return int(value)
    raise Refused('provider identity is not a supported numeric id')


def transaction_id(value):
    value = str(value) if type(value) is int else value
    if type(value) is not str or not _TXID.fullmatch(value):
        raise Refused('invalid provider transaction id')
    return value


def attention(s, account, fan_id):
    state = s.get(OfConversationState, (account, fan_id))
    if state is not None:
        state.requires_attention = True


def add(s, account, *, dedupe, kind, source, event_key, fan_id, message_id=None, txid=None, amount=None,
        provider_status=None, occurred, raw_sha256, now):
    """Insert evidence once per natural key. Returns (row, outcome)."""
    row = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                                                OfFansEvidence.dedupe_key == dedupe).with_for_update())
    if row is not None:
        if (row.kind, row.fan_id, row.message_id, row.provider_transaction_id, row.amount_cents) != (
                kind, fan_id, message_id, txid, amount):
            if row.status != 'applied':
                row.status, row.reason = 'held', 'conflicting_duplicate'
            attention(s, account, fan_id)
            row.updated_at = now
            return row, 'conflict'
        return row, 'duplicate'
    row = OfFansEvidence(account_id=account, evidence_id=str(uuid.uuid4()), dedupe_key=dedupe, kind=kind,
        source=source, event_key=event_key, fan_id=fan_id, message_id=message_id, provider_transaction_id=txid,
        amount_cents=amount, provider_status=provider_status, occurred_at=occurred, status='pending',
        raw_sha256=raw_sha256, created_at=now, updated_at=now)
    s.add(row)
    s.flush()
    return row, 'new'


def _hold(s, row, reason, now):
    row.status, row.reason, row.updated_at = 'held', reason[:96], now
    attention(s, row.account_id, row.fan_id)


def _link_unlock(s, row, now):
    """Bind an unlock to the accepted intent (and frozen grant) that sent its message."""
    intent = s.scalar(select(OfTextIntent).where(OfTextIntent.account_id == row.account_id,
                                                 OfTextIntent.provider_message_id == row.message_id))
    if intent is None:
        row.reason = 'no_matching_send'
        return None
    policy = s.get(OfDeliveryPolicy, row.account_id)
    if policy is not None and policy.transport_kind == 'fansapi':
        from db.onlyfans_models import OfRuntime
        from .evidence_scope import webhook_binding
        runtime = s.get(OfRuntime, row.account_id)
        if runtime is None or intent.binding_id != runtime.binding_id or webhook_binding(s, row) != runtime.binding_id:
            row.reason, row.updated_at = 'creator_scope_unverified', now
            return None
    if intent.fan_id != row.fan_id:
        _hold(s, row, 'fan_mismatch', now)
        return None
    if intent.price_cents <= 0 or intent.catalog_item_id is None:
        _hold(s, row, 'not_a_priced_offer', now)
        return None
    if row.amount_cents is not None and row.amount_cents != intent.price_cents:
        _hold(s, row, 'amount_mismatch', now)
        return None
    if intent.status != 'accepted':
        row.reason = 'send_unresolved'
        return None
    decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == row.account_id,
                                                       OfEngineDecision.intent_id == intent.intent_id))
    if decision is not None and decision.action == 'offer':
        grant = s.get(OfEngineOfferGrant, (row.account_id, decision.offer_grant_id)) if decision.offer_grant_id else None
        if grant is None or grant.used_intent_id != intent.intent_id or grant.fan_id != row.fan_id:
            _hold(s, row, 'no_offer_grant', now)
            return None
        row.grant_id = grant.grant_id
    row.intent_id, row.reason, row.updated_at = intent.intent_id, 'awaiting_commerce', now
    return intent


def match(s, account, fan_id, now, *, window=None, mismatch_settle=None):
    """Link pending unlocks to the send that carries their message id. Transactions are never linked here.

    Idempotent and order independent. Returns True when an unlock became linked (ready for commerce).
    """
    rows = s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.status == 'pending',
        OfFansEvidence.kind.in_(('unlock', 'transaction'))).order_by(OfFansEvidence.occurred_at,
        OfFansEvidence.evidence_id).with_for_update()).all()
    changed = False
    for row in rows:
        if row.kind == 'unlock':
            was_linked = row.intent_id is not None and row.reason == 'awaiting_commerce'
            if _link_unlock(s, row, now) is not None and not was_linked:
                changed = True
        elif row.reason != 'unlinked_transaction':
            policy = s.get(OfDeliveryPolicy, account)
            if policy is not None and policy.transport_kind == 'fansapi':
                from db.onlyfans_models import OfRuntime
                from .evidence_scope import webhook_binding
                runtime = s.get(OfRuntime, account)
                if runtime is None or webhook_binding(s, row) != runtime.binding_id:
                    row.reason, row.updated_at = 'creator_scope_unverified', now
                    continue
            row.reason, row.updated_at = 'unlinked_transaction', now
    return changed


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat()


def _tenant(s, account):
    from db.onlyfans_models import OfRuntime
    runtime = s.get(OfRuntime, account)
    binding = s.get(OfCreatorBinding, runtime.binding_id) if runtime is not None else None
    if binding is None:
        raise Refused('commerce scope unavailable')
    return binding.tenant_id


def linked_unlocks(s, account, fan_id=None):
    """Unlocks the provider tied to one accepted send and that commerce has not applied yet."""
    return list(s.scalars(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.kind == 'unlock', OfFansEvidence.status == 'pending',
        OfFansEvidence.intent_id.is_not(None), OfFansEvidence.reason == 'awaiting_commerce',
        True if fan_id is None else OfFansEvidence.fan_id == fan_id)
        .order_by(OfFansEvidence.occurred_at)))


def pending_chargebacks(s, account, fan_id=None):
    return list(s.scalars(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.kind == 'chargeback', OfFansEvidence.status == 'pending',
        True if fan_id is None else OfFansEvidence.fan_id == fan_id).order_by(OfFansEvidence.occurred_at)))


def _commerce(store):
    from onlyfans_runtime.commerce import CommerceIntake
    return CommerceIntake(store)  # Refused without the worker role: callers defer, never hold


def _engine_access(store, account, unlock_id, now):
    """Real-provider accounts only: record an unlock observation for the accepted offer.

    The provider's matching unlock is access evidence, not a transaction or settlement.
    A refusal is retained on the evidence row for operator review.
    """
    from conversation_engine.commerce import ProviderAccessObservation, record_provider_access
    with Session(store.engine) as s:
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind != 'fansapi':
            return None
        u = s.get(OfFansEvidence, (account, unlock_id))
        intent = s.get(OfTextIntent, (account, u.intent_id)) if u is not None and u.intent_id else None
        if intent is None:
            return None
        tenant = _tenant(s, account)
        key = hashlib.sha256(f'engine-access:confirmed:{u.evidence_id}'.encode()).hexdigest()
        observation = ProviderAccessObservation(account_id=account, fan_id=u.fan_id, intent_id=intent.intent_id,
            message_id=u.message_id, media_ids=tuple(json.loads(intent.media_json)), evidence_key=key,
            evidence_ref=f'fansapi:{u.dedupe_key}'[:128], status='confirmed',
            occurred_at=u.occurred_at.replace(tzinfo=timezone.utc))
    try:
        return record_provider_access(store, tenant, account, observation)
    except Refused as exc:
        with Session(store.engine) as s, s.begin():
            row = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
            row.reason, row.updated_at = ('engine_access:' + str(exc))[:96], now
        return 'refused'


def settle_unlock(store, account, unlock_id, now):
    """Record a real-provider unlock as access, without inventing a money record.

    Synthetic lab accounts retain their explicitly synthetic commerce fixture.
    """
    from onlyfans_runtime.commerce import SyntheticCommerceEvent
    # A real provider unlock is message-specific access evidence, not a
    # provider transaction or settlement. Never manufacture a financial row.
    with Session(store.engine) as s:
        policy = s.get(OfDeliveryPolicy, account)
        real_provider = policy is not None and policy.transport_kind == 'fansapi'
    if real_provider:
        if os.environ.get('ONLYFANS_PROCESS_ROLE') != 'worker':
            return 'deferred'
        with Session(store.engine) as s:
            u = s.get(OfFansEvidence, (account, unlock_id))
            if u is None or u.status != 'pending' or u.intent_id is None or u.reason != 'awaiting_commerce':
                return u.status if u is not None else 'missing'
        result = _engine_access(store, account, unlock_id, now)
        with Session(store.engine) as s, s.begin():
            u = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
            if u is None:
                return 'missing'
            if u.status != 'pending':
                return u.status
            if result == 'confirmed':
                u.status, u.reason, u.updated_at = 'applied', 'unlock_confirmed_finance_unlinked', now
            else:
                _hold(s, u, 'unlock_access_review', now)
            return u.status
    try:
        intake = _commerce(store)
    except Refused:
        return 'deferred'
    with Session(store.engine) as s, s.begin():
        u = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
        if u is None or u.status != 'pending' or u.intent_id is None or u.reason != 'awaiting_commerce':
            return u.status if u is not None else 'missing'
        intent = s.get(OfTextIntent, (account, u.intent_id))
        tenant = _tenant(s, account)
        event = SyntheticCommerceEvent(account_id=account, fan_id=u.fan_id, message_id=u.message_id,
            event_key=hashlib.sha256(f'unlock:{u.evidence_id}'.encode()).hexdigest()[:64],
            provider_transaction_id=f'unlock:{u.message_id}', provider_revision=1, kind='purchase',
            amount_cents=intent.price_cents, currency='USD', occurred_at=_iso(u.occurred_at),
            evidence_ref=f'fansapi:{u.dedupe_key}'[:256], payout_status='pending')
    try:
        outcome = intake.record(tenant, account, event)
        reason = None if outcome == 'applied' else 'commerce_' + outcome
    except Refused as exc:
        outcome, reason = 'refused', 'commerce_refused:' + str(exc)[:60]
    with Session(store.engine) as s, s.begin():
        u = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
        u.updated_at = now
        if outcome == 'applied':
            u.status, u.reason = 'applied', None
        else:
            _hold(s, u, reason, now)
        result = u.status
    if outcome == 'applied':
        _engine_access(store, account, unlock_id, now)
    return result


def flag_unlinked_reversal(store, account, chargeback_id, now):
    """A chargeback names a transaction id only. With no supported link to a purchase it is kept as separate
    evidence and the fan is flagged for a person; nothing is revoked or counted from a guess."""
    with Session(store.engine) as s, s.begin():
        cb = s.get(OfFansEvidence, (account, chargeback_id), with_for_update=True)
        if cb is None or cb.status != 'pending':
            return cb.status if cb is not None else 'missing'
        policy = s.get(OfDeliveryPolicy, account)
        if policy is not None and policy.transport_kind == 'fansapi':
            from db.onlyfans_models import OfRuntime
            from .evidence_scope import webhook_binding
            runtime = s.get(OfRuntime, account)
            if runtime is None or webhook_binding(s, cb) != runtime.binding_id:
                cb.reason, cb.updated_at = 'creator_scope_unverified', now
                return 'pending'
        if cb.reason != 'unlinked_reversal':
            cb.reason, cb.updated_at = 'unlinked_reversal', now
            attention(s, account, cb.fan_id)
    return 'pending'


def fan_is_held(s, account, fan_id=None):
    """True while this fan (or, with no fan, any fan) has evidence held for human review."""
    query = select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.status == 'held', OfFansEvidence.kind != 'human_sent')
    if fan_id is not None:
        query = query.where(OfFansEvidence.fan_id == fan_id)
    return s.scalar(query.limit(1)) is not None
