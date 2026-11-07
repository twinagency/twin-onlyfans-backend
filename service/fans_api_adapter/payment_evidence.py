"""Payment evidence: store first, then match, then feed the canonical commerce owner.

Two independent provider facts describe one purchase and may arrive in either
order, minutes apart, or twice:

* ``messages.ppv.unlocked``  names OUR sent message (``message_id``) and the fan
  (``chat_id``; ``user_id`` is the CREATOR in that event).
* ``transactions.new``       carries the provider transaction id and amount but
  no message id, and can be delayed by up to 3 minutes.

Neither alone creates a sale. A pending-evidence matcher pairs them (same fan,
equal amount, near in time), links the unlock to the intent and frozen offer
grant that sent the message, and only then calls ``CommerceIntake`` - the
existing canonical financial owner. A mismatched amount, an ambiguous pairing
or a missing grant is held for review and is never granted. Missing evidence
is never recorded as "not purchased": an unmatched row simply stays pending.
Chargebacks reuse the same owner (kind ``chargeback``) and so revoke access.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import OfConversationState, OfCreatorBinding
from db.onlyfans_delivery_models import OfTextIntent
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
    row.intent_id, row.reason, row.updated_at = intent.intent_id, 'awaiting_transaction', now
    return intent


def match(s, account, fan_id, now, *, window, mismatch_settle):
    """Pair pending unlocks with pending transactions for one fan. Idempotent; order independent."""
    rows = s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
        OfFansEvidence.fan_id == fan_id, OfFansEvidence.status == 'pending',
        OfFansEvidence.kind.in_(('unlock', 'transaction'))).order_by(OfFansEvidence.occurred_at,
        OfFansEvidence.evidence_id).with_for_update()).all()
    txs = [r for r in rows if r.kind == 'transaction']
    price = {}
    for u in [r for r in rows if r.kind == 'unlock']:
        intent = _link_unlock(s, u, now)
        if intent is not None:
            price[u.evidence_id] = intent.price_cents
    for t in txs:
        if t.reason is None:
            t.reason = 'awaiting_unlock'
    linked = [u for u in rows if u.kind == 'unlock' and u.status == 'pending' and u.evidence_id in price]

    def near(u, t):
        return u.source != 'webhook' or abs(u.occurred_at - t.occurred_at) <= window

    changed = False
    for u in linked:
        if u.status != 'pending':
            continue
        free = [t for t in txs if t.status == 'pending' and t.peer_id is None]
        candidates = [t for t in free if near(u, t)]
        exact = [t for t in candidates if t.amount_cents == price[u.evidence_id]]
        if len(exact) == 1:
            t = exact[0]
            rivals = [x for x in linked if x is not u and x.status == 'pending' and x.peer_id is None
                      and price[x.evidence_id] == t.amount_cents and near(x, t)]
            if rivals:
                for item in [u, t, *rivals]:
                    _hold(s, item, 'ambiguous_pairing', now)
            else:
                u.peer_id, t.peer_id, u.status, t.status = t.evidence_id, u.evidence_id, 'paired', 'paired'
                u.reason = t.reason = 'awaiting_commerce'
                u.updated_at = t.updated_at = now
            changed = True
        elif len(exact) > 1:
            for item in [u, *exact]:
                _hold(s, item, 'ambiguous_pairing', now)
            changed = True
        elif candidates and now - max(u.created_at, *(t.created_at for t in candidates)) >= mismatch_settle:
            # Same fan, same moment, different money: never grant, let a person decide.
            for item in [u, *candidates]:
                _hold(s, item, 'amount_mismatch', now)
            changed = True
    return changed


def purchase_time(unlock, tx):
    return unlock.occurred_at if unlock.source == 'webhook' else tx.occurred_at


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat()


def _tenant(s, account):
    from db.onlyfans_models import OfRuntime
    runtime = s.get(OfRuntime, account)
    binding = s.get(OfCreatorBinding, runtime.binding_id) if runtime is not None else None
    if binding is None:
        raise Refused('commerce scope unavailable')
    return binding.tenant_id


def paired_unlocks(s, account):
    return list(s.scalars(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.kind == 'unlock', OfFansEvidence.status == 'paired').order_by(OfFansEvidence.occurred_at)))


def pending_chargebacks(s, account):
    return list(s.scalars(select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.kind == 'chargeback', OfFansEvidence.status == 'pending').order_by(OfFansEvidence.occurred_at)))


def _commerce(store):
    from onlyfans_runtime.commerce import CommerceIntake
    return CommerceIntake(store)  # Refused without the worker role: callers defer, never hold


def settle_pair(store, account, unlock_id, now):
    """Hand one exact pairing to the canonical owner. Returns the evidence status or 'deferred'."""
    from onlyfans_runtime.commerce import SyntheticCommerceEvent
    try:
        intake = _commerce(store)
    except Refused:
        return 'deferred'
    with Session(store.engine) as s, s.begin():
        u = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
        if u is None or u.status != 'paired':
            return u.status if u is not None else 'missing'
        t = s.get(OfFansEvidence, (account, u.peer_id), with_for_update=True)
        intent = s.get(OfTextIntent, (account, u.intent_id))
        tenant = _tenant(s, account)
        event = SyntheticCommerceEvent(account_id=account, fan_id=u.fan_id, message_id=u.message_id,
            event_key=hashlib.sha256(f'{u.evidence_id}:{t.evidence_id}'.encode()).hexdigest()[:64],
            provider_transaction_id=t.provider_transaction_id, provider_revision=1, kind='purchase',
            amount_cents=intent.price_cents, currency='USD', occurred_at=_iso(purchase_time(u, t)),
            evidence_ref=f'fansapi:{u.dedupe_key}+{t.dedupe_key}'[:256],
            payout_status='pending' if t.provider_status == 'loading' else 'cleared')
    try:
        outcome = intake.record(tenant, account, event)
        reason = None if outcome == 'applied' else 'commerce_' + outcome
    except Refused as exc:
        outcome, reason = 'refused', 'commerce_refused:' + str(exc)[:60]
    with Session(store.engine) as s, s.begin():
        u = s.get(OfFansEvidence, (account, unlock_id), with_for_update=True)
        t = s.get(OfFansEvidence, (account, u.peer_id), with_for_update=True)
        for row in (u, t):
            row.updated_at = now
            if outcome == 'applied':
                row.status, row.reason = 'applied', None
            else:
                _hold(s, row, reason, now)
        return u.status


def settle_chargeback(store, account, chargeback_id, now):
    """Revoke through the same owner once the purchase it reverses has been applied."""
    from onlyfans_runtime.commerce import SyntheticCommerceEvent
    try:
        intake = _commerce(store)
    except Refused:
        return 'deferred'
    with Session(store.engine) as s, s.begin():
        cb = s.get(OfFansEvidence, (account, chargeback_id), with_for_update=True)
        if cb is None or cb.status != 'pending':
            return cb.status if cb is not None else 'missing'
        t = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
            OfFansEvidence.kind == 'transaction', OfFansEvidence.provider_transaction_id == cb.provider_transaction_id)
            .with_for_update())
        if t is None or t.status != 'applied':
            if cb.reason != 'awaiting_purchase':
                cb.reason, cb.updated_at = 'awaiting_purchase', now
            return 'pending'
        if cb.amount_cents != t.amount_cents or cb.fan_id != t.fan_id:
            _hold(s, cb, 'chargeback_mismatch', now)
            return 'held'
        u = s.get(OfFansEvidence, (account, t.peer_id))
        intent = s.get(OfTextIntent, (account, u.intent_id))
        tenant = _tenant(s, account)
        occurred = max(cb.occurred_at, purchase_time(u, t) + timedelta(seconds=1))
        event = SyntheticCommerceEvent(account_id=account, fan_id=u.fan_id, message_id=u.message_id,
            event_key=hashlib.sha256(f'chargeback:{cb.evidence_id}'.encode()).hexdigest()[:64],
            provider_transaction_id=t.provider_transaction_id, provider_revision=2, kind='chargeback',
            amount_cents=intent.price_cents, currency='USD', occurred_at=_iso(occurred),
            evidence_ref=f'fansapi:{cb.dedupe_key}'[:256])
    try:
        outcome = intake.record(tenant, account, event)
        reason = None if outcome == 'applied' else 'commerce_' + outcome
    except Refused as exc:
        outcome, reason = 'refused', 'commerce_refused:' + str(exc)[:60]
    with Session(store.engine) as s, s.begin():
        cb = s.get(OfFansEvidence, (account, chargeback_id), with_for_update=True)
        cb.updated_at = now
        if outcome == 'applied':
            cb.status, cb.reason = 'applied', None
        else:
            _hold(s, cb, reason, now)
        return cb.status


def fan_is_held(s, account, fan_id=None):
    """True while this fan (or, with no fan, any fan) has evidence held for human review."""
    query = select(OfFansEvidence.evidence_id).where(OfFansEvidence.account_id == account,
        OfFansEvidence.status == 'held', OfFansEvidence.kind != 'human_sent')
    if fan_id is not None:
        query = query.where(OfFansEvidence.fan_id == fan_id)
    return s.scalar(query.limit(1)) is not None
