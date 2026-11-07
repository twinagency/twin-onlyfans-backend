"""Synthetic-only offer authority using the existing catalogue and ledger.

Coverage supplied here is a test fixture, not evidence of a real fan's lifetime
ownership. Real commercial operation requires approved source and workflow rules.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from onlyfans_runtime.media_delivery import assert_unowned, catalog_terms
from onlyfans_runtime.store import Refused, _id, utcnow
from ownership import owned_media_in_session


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def _sales_permits(owner, item_id):
    if owner.sales_mode == 'open':
        return True
    if owner.sales_mode != 'reopened_scoped':
        return False
    try:
        scope = json.loads(owner.sales_scope_json)
    except (TypeError, ValueError):
        return False
    return type(scope) is list and str(item_id) in scope


def _unowned(s, account, fan_id, terms):
    assert_unowned(s, account, fan_id, terms)
    try:
        owned = owned_media_in_session(s, account, fan_id, limit=10000)
    except ValueError as exc:
        raise Refused('ownership evidence unavailable') from exc
    if set(terms['media_ids']) & set(owned):
        raise Refused('bundle overlaps owned media')


@dataclass(frozen=True)
class SyntheticCoverage:
    account_id: str
    fan_id: int
    item_id: int
    media_ids: tuple[int, ...]
    evidence_ref: str
    valid_until: datetime


def approve_simulated_offer(store, tenant, account, fan_id, item_id, coverage):
    if type(coverage) is not SyntheticCoverage or coverage.account_id != account or coverage.fan_id != fan_id or coverage.item_id != item_id:
        raise Refused('synthetic coverage scope mismatch')
    _id(fan_id); _id(item_id)
    if (type(coverage.evidence_ref) is not str or not coverage.evidence_ref.startswith('synthetic:')
            or len(coverage.evidence_ref) > 128 or not isinstance(coverage.valid_until, datetime)
            or coverage.valid_until.tzinfo is not None or coverage.valid_until <= utcnow()):
        raise Refused('synthetic coverage is incomplete')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        owner = s.get(OfEngineOwner, (account, fan_id))
        policy = s.get(OfDeliveryPolicy, account)
        if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                or policy is None or policy.transport_kind != 'synthetic' or not policy.media_enabled
                or not _sales_permits(owner, item_id)):
            raise Refused('synthetic offer is not authorized')
        terms = catalog_terms(s, account, item_id)
        if type(coverage.media_ids) is not tuple or tuple(terms['media_ids']) != coverage.media_ids:
            raise Refused('synthetic ownership coverage does not match the bundle')
        _unowned(s, account, fan_id, terms)
        digest = _hash(terms)
        prior = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
            OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id,
            OfEngineOfferGrant.terms_hash == digest))
        if prior is not None:
            if prior.binding_id != runtime.binding_id or prior.used_intent_id is not None or prior.expires_at <= utcnow():
                raise Refused('existing offer authorization needs review')
            return prior.grant_id
        grant_id = str(uuid.uuid4())
        s.add(OfEngineOfferGrant(account_id=account, grant_id=grant_id, fan_id=fan_id,
            binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest,
            coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until, created_at=utcnow()))
        return grant_id


def verified_terms(s, runtime, owner, grant, *, intent_id=None):
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or grant is None or grant.binding_id != runtime.binding_id or grant.fan_id != owner.fan_id
            or not grant.coverage_ref.startswith(('synthetic:', 'vault:')) or grant.expires_at <= utcnow()
            or grant.used_intent_id not in (None, intent_id) or not _sales_permits(owner, grant.item_id)):
        raise Refused('offer authorization is stale or unavailable')
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    if policy is None or policy.transport_kind != 'synthetic' or not policy.media_enabled:
        raise Refused('synthetic media is unavailable')
    terms = catalog_terms(s, runtime.account_id, grant.item_id)
    if _hash(terms) != grant.terms_hash:
        raise Refused('approved offer terms changed')
    _unowned(s, runtime.account_id, owner.fan_id, terms)
    from onlyfans_runtime.quote import request_exists
    if request_exists(s, runtime.account_id, owner.fan_id, terms):
        raise Refused('an existing offer needs review')
    return terms


@dataclass(frozen=True)
class RealCatalogueCoverage:
    """Approval that a synced, operator-approved catalogue item may be offered to one fan.

    Unlike SyntheticCoverage it asserts no ownership fixture: fan ownership still comes
    from the local ledger (`_unowned`). Media must be live vault records from a real sync.
    """
    account_id: str
    fan_id: int
    item_id: int
    evidence_ref: str
    valid_until: datetime


def approve_catalogue_offer(store, tenant, account, fan_id, item_id, coverage):
    from db.models import VaultItem
    if (type(coverage) is not RealCatalogueCoverage or coverage.account_id != account or coverage.fan_id != fan_id
            or coverage.item_id != item_id):
        raise Refused('catalogue coverage scope mismatch')
    _id(fan_id); _id(item_id)
    if (type(coverage.evidence_ref) is not str or not coverage.evidence_ref.startswith('vault:')
            or len(coverage.evidence_ref) > 128 or not isinstance(coverage.valid_until, datetime)
            or coverage.valid_until.tzinfo is not None or coverage.valid_until <= utcnow()):
        raise Refused('catalogue coverage is incomplete')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        owner = s.get(OfEngineOwner, (account, fan_id))
        policy = s.get(OfDeliveryPolicy, account)
        if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                or policy is None or policy.transport_kind != 'synthetic' or not policy.media_enabled
                or not _sales_permits(owner, item_id)):
            raise Refused('catalogue offer is not authorized')
        terms = catalog_terms(s, account, item_id)  # approved, enabled, priced, media live
        for saved in terms['files']:
            v = s.get(VaultItem, (account, saved['media_id']))
            if v is None or v.removed_at is not None or v.content_hash_kind != 'vault-metadata-v1':
                raise Refused('media is not a synced real vault item')
        _unowned(s, account, fan_id, terms)
        digest = _hash(terms)
        prior = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
            OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id,
            OfEngineOfferGrant.terms_hash == digest))
        if prior is not None:
            if prior.binding_id != runtime.binding_id or prior.used_intent_id is not None or prior.expires_at <= utcnow():
                raise Refused('existing offer authorization needs review')
            return prior.grant_id
        grant_id = str(uuid.uuid4())
        s.add(OfEngineOfferGrant(account_id=account, grant_id=grant_id, fan_id=fan_id,
            binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest,
            coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until, created_at=utcnow()))
        return grant_id


@dataclass(frozen=True)
class SyntheticAccessObservation:
    account_id: str
    fan_id: int
    intent_id: str
    message_id: int
    media_ids: tuple[int, ...]
    evidence_key: str
    evidence_ref: str
    status: str
    occurred_at: datetime


def record_simulated_access(store, tenant, account, observation):
    if (type(observation) is not SyntheticAccessObservation or observation.account_id != account
            or observation.status not in {'confirmed', 'revoked'} or type(observation.media_ids) is not tuple
            or type(observation.evidence_key) is not str or not 1 <= len(observation.evidence_key) <= 128
            or type(observation.evidence_ref) is not str or not observation.evidence_ref.startswith('synthetic:')
            or len(observation.evidence_ref) > 128 or not isinstance(observation.occurred_at, datetime)
            or observation.occurred_at.tzinfo is None or observation.occurred_at.utcoffset() is None):
        raise Refused('invalid synthetic access observation')
    _id(observation.fan_id); _id(observation.message_id)
    for media_id in observation.media_ids: _id(media_id)
    stamp = observation.occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    encoded_media = json.dumps(list(observation.media_ids), separators=(',', ':'))
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        policy = s.get(OfDeliveryPolicy, account)
        if policy is None or policy.transport_kind != 'synthetic':
            raise Refused('access observation is limited to the synthetic lab')
        prior = s.get(OfEngineAccessEvidence, (account, observation.evidence_key))
        if prior is not None:
            if (prior.intent_id, prior.fan_id, prior.message_id, prior.media_json, prior.status,
                    prior.evidence_ref, prior.occurred_at) != (observation.intent_id, observation.fan_id,
                    observation.message_id, encoded_media, observation.status, observation.evidence_ref, stamp):
                raise Refused('access evidence identity conflict')
            return prior.status if prior.applied else 'stale'
        intent = s.get(OfTextIntent, (account, observation.intent_id))
        decision = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                                                           OfEngineDecision.intent_id == observation.intent_id))
        message = s.get(Message, (account, observation.fan_id, observation.message_id))
        if (intent is None or decision is None or decision.action != 'offer' or intent.status != 'accepted'
                or intent.binding_id != runtime.binding_id or intent.fan_id != observation.fan_id
                or intent.provider_message_id != observation.message_id or intent.media_json != encoded_media
                or message is None or message.direction != 'out' or stamp < message.created_at
                or observation.status == 'confirmed' and message.is_paid is not True):
            raise Refused('access evidence does not match the accepted purchase')
        request = s.get(OfEngineRequest, (account, decision.request_id))
        if request is None:
            raise Refused('engine request unavailable')
        latest = s.scalar(select(OfEngineAccessEvidence).where(OfEngineAccessEvidence.account_id == account,
            OfEngineAccessEvidence.intent_id == observation.intent_id, OfEngineAccessEvidence.applied.is_(True))
            .order_by(OfEngineAccessEvidence.occurred_at.desc()).limit(1))
        stale = latest is not None and stamp <= latest.occurred_at
        s.add(OfEngineAccessEvidence(account_id=account, evidence_key=observation.evidence_key,
            intent_id=observation.intent_id, fan_id=observation.fan_id, message_id=observation.message_id,
            media_json=encoded_media, status=observation.status, applied=not stale, evidence_ref=observation.evidence_ref,
            occurred_at=stamp, created_at=utcnow()))
        if stale:
            return 'stale'
        if request.status not in {'handover_pending', 'handover_accepted'}:
            request.status = 'completed' if observation.status == 'confirmed' else 'open'
        request.next_step = ('review_access' if request.status in {'handover_pending', 'handover_accepted'} else
                             'none' if observation.status == 'confirmed' else 'review_access_revocation')
        request.revision += 1
        request.updated_at = utcnow()
        return observation.status
