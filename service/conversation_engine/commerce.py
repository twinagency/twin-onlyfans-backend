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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfTextIntent
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


def provider_unlocked_media(s, runtime, fan_id, *, limit=10000):
    # Access events are separate from the transaction ledger. Use the newest
    # applied observation for each scoped intent to prevent repeat offers.
    rows = list(s.scalars(select(OfEngineAccessEvidence).join(OfTextIntent,
        (OfTextIntent.account_id == OfEngineAccessEvidence.account_id) &
        (OfTextIntent.intent_id == OfEngineAccessEvidence.intent_id)).where(
        OfEngineAccessEvidence.account_id == runtime.account_id,
        OfEngineAccessEvidence.fan_id == fan_id, OfEngineAccessEvidence.applied.is_(True),
        OfEngineAccessEvidence.evidence_ref.like('fansapi:%'),
        OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.status == 'accepted')
        .order_by(OfEngineAccessEvidence.occurred_at.desc(), OfEngineAccessEvidence.evidence_key.desc())
        .limit(limit + 1)))
    if len(rows) > limit:
        raise Refused('access evidence exceeds bounded read')
    seen, media = set(), set()
    for row in rows:
        if row.intent_id in seen:
            continue
        seen.add(row.intent_id)
        if row.status == 'confirmed':
            try:
                ids = json.loads(row.media_json)
                if type(ids) is not list:
                    raise ValueError()
                for value in ids:
                    _id(value)
                media.update(ids)
            except (ValueError, TypeError, Refused):
                raise Refused('access evidence is invalid') from None
    return media


def _unowned(s, account, fan_id, terms):
    assert_unowned(s, account, fan_id, terms)
    try:
        owned = owned_media_in_session(s, account, fan_id, limit=10000)
    except ValueError as exc:
        raise Refused('ownership evidence unavailable') from exc
    policy = s.get(OfDeliveryPolicy, account)
    if policy is not None and policy.transport_kind == 'fansapi':
        from db.onlyfans_models import OfRuntime
        runtime = s.get(OfRuntime, account)
        if runtime is None:
            raise Refused('access scope unavailable')
        owned |= provider_unlocked_media(s, runtime, fan_id)
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


# A grant's coverage evidence must match the policy kind that will send it: lab fixtures for the
# simulator, synced real vault media for the real provider. Neither can authorize the other.
_COVERAGE_PREFIX = {'synthetic': 'synthetic:', 'fansapi': 'vault:'}


def verified_terms(s, runtime, owner, grant, *, intent_id=None):
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or grant is None or grant.binding_id != runtime.binding_id or grant.fan_id != owner.fan_id
            or not grant.coverage_ref.startswith(('synthetic:', 'vault:')) or grant.expires_at <= utcnow()
            or grant.used_intent_id not in (None, intent_id) or not _sales_permits(owner, grant.item_id)):
        raise Refused('offer authorization is stale or unavailable')
    policy = s.get(OfDeliveryPolicy, runtime.account_id)
    if (policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or not policy.media_enabled
            or not grant.coverage_ref.startswith(_COVERAGE_PREFIX[policy.transport_kind])):
        raise Refused('synthetic media is unavailable')
    if policy.transport_kind == 'fansapi':
        _pilot_coverage(s, runtime, grant)
        # A real offer waits for a finished backfill of this fan's history and transactions (journey.py).
        from .journey import backfill_complete
        if not backfill_complete(s, runtime.account_id, owner.fan_id):
            raise Refused('offers wait for a finished backfill of this fan')
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
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        return _approve_catalogue_offer(s, runtime, fan_id, item_id, coverage)


def _approve_catalogue_offer(s, runtime, fan_id, item_id, coverage):
    """Shared transaction path for explicit setup and standing-policy preparation."""
    from db.models import VaultItem
    account = runtime.account_id
    if (type(coverage) is not RealCatalogueCoverage or coverage.account_id != account or coverage.fan_id != fan_id
            or coverage.item_id != item_id):
        raise Refused('catalogue coverage scope mismatch')
    _id(fan_id); _id(item_id)
    if (type(coverage.evidence_ref) is not str or not coverage.evidence_ref.startswith('vault:')
            or len(coverage.evidence_ref) > 128 or not isinstance(coverage.valid_until, datetime)
            or coverage.valid_until.tzinfo is not None or coverage.valid_until <= utcnow()):
        raise Refused('catalogue coverage is incomplete')
    owner = s.get(OfEngineOwner, (account, fan_id))
    policy = s.get(OfDeliveryPolicy, account)
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or policy is None or policy.transport_kind != 'fansapi' or not policy.media_enabled
            or not _sales_permits(owner, item_id)):
        raise Refused('catalogue offer is not authorized')
    terms = catalog_terms(s, account, item_id)
    for saved in terms['files']:
        v = s.get(VaultItem, (account, saved['media_id']))
        if v is None or v.removed_at is not None or v.content_hash_kind != 'vault-metadata-v1':
            raise Refused('media is not a synced real vault item')
    _unowned(s, account, fan_id, terms)
    digest = _hash(terms)
    query = select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
        OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id,
        OfEngineOfferGrant.terms_hash == digest)
    prior = s.scalar(query)
    if prior is None:
        try:
            with s.begin_nested():
                prior = OfEngineOfferGrant(account_id=account, grant_id=str(uuid.uuid4()), fan_id=fan_id,
                    binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest,
                    coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until, created_at=utcnow())
                s.add(prior)
                s.flush()
        except IntegrityError:
            # Another legitimate preparer won the existing unique terms slot.
            # Validate that record; never delete uniqueness or mint another key.
            prior = s.scalar(query)
            if prior is None:
                raise Refused('offer preparation conflict needs review') from None
    if prior.binding_id != runtime.binding_id or prior.used_intent_id is not None or prior.expires_at <= utcnow():
        raise Refused('existing offer authorization needs review')
    _pilot_coverage(s, runtime, prior)
    return prior.grant_id


def _pilot_coverage(s, runtime, grant):
    from .pilot_scope import current, current_offer_coverage
    pilot, config = current(s, runtime)
    if pilot is None or 'offer_coverage' not in config:
        return
    coverage = current_offer_coverage(s, runtime, config)
    if (grant.fan_id not in config['fans'] or grant.item_id != config['product']['id']
            or grant.coverage_ref != coverage.evidence_ref or grant.expires_at > coverage.valid_until):
        raise Refused('existing offer coverage needs review')


def prepare_pilot_offer(store, tenant, account, fan_id, message_id, engine):
    """Prepare only the standing approved product, using explicit durable coverage.

    No model input grants authority. Missing, stale, expired or consumed
    evidence remains visible on the same request; it is never renewed here.
    """
    from db.models import OfConversationState
    from .journey import pilot_purchase_scope, live_current_request, backfill_complete
    from .history_scope import has_unscoped_messages
    from .pilot_scope import current, current_offer_coverage, planning_scope, delivery_block
    from .offers import match
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        message = s.get(Message, (account, fan_id, message_id))
        pilot, config = current(s, runtime)
        if pilot is None:
            return None
        scope = pilot_purchase_scope(s, runtime, fan_id, message)
        if not live_current_request(s, runtime, fan_id, message):
            return None
        owner = s.get(OfEngineOwner, (account, fan_id))
        # Standing permission makes the one eligible product available to the writer.
        # It is not a requirement to pitch and cannot reopen a paused sale. Only an
        # explicit purchase request waits on missing commercial evidence; chat continues.
        if not scope and (config['authorization_mode'] != 'autonomous'
                or fan_id not in config['fans'] or owner is None or owner.sales_mode != 'open'):
            return None
        request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
            OfEngineRequest.fan_id == fan_id, OfEngineRequest.source_message_id == message_id))
        if request is None:
            request = OfEngineRequest(account_id=account, request_id=str(uuid.uuid4()), fan_id=fan_id,
                binding_id=runtime.binding_id, source_message_id=message_id, status='open', owner='engine',
                next_step='prepare_reply', revision=0, created_at=utcnow(), updated_at=utcnow())
            s.add(request)
            s.flush()
        if request.status != 'open':
            return None
        reason = 'review_offer_authorization'
        try:
            planning_scope(s, runtime, fan_id, engine.model, draft_only=engine.draft_only)
            pilot, config = current(s, runtime)
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                    or state is None or state.mode != 'automated' or state.takeover_state != 'none'
                    or store._unresolved(s, account, fan_id, state)):
                raise Refused('offer_control_or_delivery_hold')
            blocked = delivery_block(s, runtime, fan_id, commercial=True)
            if blocked:
                raise Refused(blocked)
            if not backfill_complete(s, account, fan_id) or has_unscoped_messages(s, account, fan_id, runtime.binding_id):
                raise Refused('offer_history_coverage_missing')
            item_id = config['product']['id']
            profile = engine.model.profiles.get(account)
            if profile is None or not profile.may_offer(str(item_id)):
                raise Refused('offer_profile_not_permitted')
            terms = catalog_terms(s, account, item_id)
            existing = match(s, runtime, fan_id, message.body, include_cards=True).get('cards', [])
            if any(card['itemId'] == str(item_id) and card['sendState'] == 'accepted' for card in existing):
                return {'state': 'existing_card'}
            _unowned(s, account, fan_id, terms)
            if not _sales_permits(owner, item_id):
                raise Refused('offer_scoped_reopening_required')
            prior = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
                OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id,
                OfEngineOfferGrant.terms_hash == _hash(terms)))
            if prior is None and s.scalar(select(OfEngineOfferGrant.grant_id).where(
                    OfEngineOfferGrant.account_id == account, OfEngineOfferGrant.fan_id == fan_id,
                    OfEngineOfferGrant.item_id == item_id).limit(1)) is not None:
                raise Refused('offer_authorization_terms_need_review')
            if prior is None or 'offer_coverage' in config:
                coverage = current_offer_coverage(s, runtime, config)
                grant_id = _approve_catalogue_offer(s, runtime, fan_id, item_id,
                    RealCatalogueCoverage(account, fan_id, item_id, coverage.evidence_ref, coverage.valid_until))
                prior = s.get(OfEngineOfferGrant, (account, grant_id))
            verified_terms(s, runtime, owner, prior)
            if request.next_step != 'prepare_reply':
                request.next_step, request.updated_at = 'prepare_reply', utcnow()
                request.revision += 1
            return {'state': 'prepared', 'grantId': prior.grant_id}
        except Refused as exc:
            if not scope:
                return {'state': 'unavailable'}
            if str(exc) in {'catalogue media already owned', 'bundle overlaps owned media'}:
                return {'state': 'owned'}
            if str(exc).startswith(('offer_', 'pilot_')) and len(str(exc)) <= 64:
                reason = str(exc)
            elif 'coverage' in str(exc):
                reason = 'review_offer_coverage'
            elif any(word in str(exc) for word in ('catalogue', 'media', 'terms')):
                reason = 'review_offer_terms'
            if request.next_step != reason:
                request.next_step, request.updated_at = reason, utcnow()
                request.revision += 1
            return {'state': 'held', 'reason': reason, 'detail': str(exc)}


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


@dataclass(frozen=True)
class ProviderAccessObservation:
    """Real-provider purchase/chargeback observation for one accepted offer (evidence_ref 'fansapi:...')."""
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
    return _record_access(store, tenant, account, observation, SyntheticAccessObservation, 'synthetic', 'synthetic:')


def record_provider_access(store, tenant, account, observation):
    return _record_access(store, tenant, account, observation, ProviderAccessObservation, 'fansapi', 'fansapi:')


def _record_access(store, tenant, account, observation, cls, kind, prefix):
    if (type(observation) is not cls or observation.account_id != account
            or observation.status not in {'confirmed', 'revoked'} or type(observation.media_ids) is not tuple
            or type(observation.evidence_key) is not str or not 1 <= len(observation.evidence_key) <= 128
            or type(observation.evidence_ref) is not str or not observation.evidence_ref.startswith(prefix)
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
        if policy is None or policy.transport_kind != kind:
            raise Refused('access observation does not match the account delivery mode')
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
                or cls is SyntheticAccessObservation and observation.status == 'confirmed' and message.is_paid is not True):
            raise Refused('access evidence does not match the accepted purchase')
        if cls is ProviderAccessObservation:
            # Only the exact persisted, authenticated, message-bound unlock is
            # accepted. An arbitrary ref or unlinked reversal is insufficient.
            from db.onlyfans_fans_intake_models import OfFansEvidence
            dedupe = observation.evidence_ref.removeprefix('fansapi:')
            unlock = s.scalar(select(OfFansEvidence).where(OfFansEvidence.account_id == account,
                OfFansEvidence.dedupe_key == dedupe, OfFansEvidence.kind == 'unlock'))
            from fans_api_adapter.evidence_scope import webhook_binding
            if (observation.status != 'confirmed' or unlock is None or unlock.source != 'webhook'
                    or webhook_binding(s, unlock) != runtime.binding_id
                    or unlock.status not in {'pending', 'applied'}
                    or unlock.reason not in {'awaiting_commerce', 'unlock_confirmed_finance_unlinked'}
                    or unlock.intent_id != intent.intent_id or unlock.fan_id != observation.fan_id
                    or unlock.message_id != observation.message_id or unlock.grant_id != decision.offer_grant_id
                    or unlock.amount_cents != intent.price_cents or unlock.occurred_at != stamp
                    or observation.evidence_key != sha256(f'engine-access:confirmed:{unlock.evidence_id}'.encode()).hexdigest()):
                raise Refused('verified provider unlock evidence required')
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
