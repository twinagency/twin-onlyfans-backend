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

from db.models import ContentOffer, Message
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner, OfEngineRequest
from onlyfans_runtime.media_delivery import assert_unowned, catalog_terms
from onlyfans_runtime.store import Refused, _id, utcnow
from ownership import owned_media_in_session


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def _terms_stamp(terms):
    """What a commercial grant records about the approved terms it covers (revision, variant, offered amount); None for legacy terms."""
    block = terms.get('commercial') if isinstance(terms, dict) else None
    if not isinstance(block, dict):
        return None
    return {'revision': block['revision'], 'variant': block['variant'], 'priceCents': terms['price_cents']}


def _grant_stamp(grant):
    try:
        doc = json.loads(grant.authorization_json)
    except (TypeError, ValueError):
        return None
    stamp = doc.get('terms') if isinstance(doc, dict) else None
    return stamp if isinstance(stamp, dict) and type(stamp.get('priceCents')) is int else None


def _grant_price(grant):
    stamp = _grant_stamp(grant)
    return None if stamp is None else stamp['priceCents']


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
    # assert_unowned also applies the imported-history ownership rule (owned / unknown paid assets) when configured.
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
    if terms.get('commercial') is not None:
        # Commercial products also refuse on unknown ownership (unmapped paid evidence); the legacy pilot checks above are unchanged.
        from .ownership_gate import ownership_gate
        ownership_gate(s, account, fan_id, terms)


@dataclass(frozen=True)
class SyntheticCoverage:
    account_id: str
    fan_id: int
    item_id: int
    media_ids: tuple[int, ...]
    evidence_ref: str
    valid_until: datetime


def approve_simulated_offer(store, tenant, account, fan_id, item_id, coverage, *, price_cents=None):
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
        terms = catalog_terms(s, account, item_id, price_cents=price_cents)
        if type(coverage.media_ids) is not tuple or tuple(terms['media_ids']) != coverage.media_ids:
            raise Refused('synthetic ownership coverage does not match the bundle')
        _unowned(s, account, fan_id, terms)
        digest = _hash(terms)
        prior = _latest_grant(s, account, fan_id, item_id, digest)
        if prior is not None:
            if prior.binding_id != runtime.binding_id or prior.used_intent_id is not None or prior.expires_at <= utcnow():
                raise Refused('existing offer authorization needs review')
            return prior.grant_id
        grant_id = str(uuid.uuid4())
        stamp = _terms_stamp(terms)
        extra = {} if stamp is None else {'authorization_json': json.dumps(
            {'kind': 'commercial_initial', 'terms': stamp}, sort_keys=True, separators=(',', ':'))}
        s.add(OfEngineOfferGrant(account_id=account, grant_id=grant_id, fan_id=fan_id,
            binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest,
            coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until, created_at=utcnow(), **extra))
        return grant_id


# A grant's coverage evidence must match the policy kind that will send it: lab fixtures for the
# simulator, synced real vault media for the real provider. Neither can authorize the other.
_COVERAGE_PREFIX = {'synthetic': 'synthetic:', 'fansapi': 'vault:'}



def _latest_grant(s, account, fan_id, item_id, digest):
    return s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
        OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id,
        OfEngineOfferGrant.terms_hash == digest).order_by(OfEngineOfferGrant.issuance.desc()).limit(1))


def _grant_authority(s, runtime, coverage, policy):
    from .pilot_scope import current
    pilot, config = current(s, runtime)
    return {'kind': 'standing_pilot' if pilot else 'explicit_catalogue_coverage',
        'binding': runtime.binding_id, 'policyRevision': policy.revision,
        'coverageRef': coverage.evidence_ref, 'validUntil': coverage.valid_until.isoformat(),
        'pilot': None if pilot is None else {'revision': pilot.revision, 'fingerprint': pilot.fingerprint,
            'actor': config['operator']['actor'], 'coverage': config.get('offer_coverage')}}


def _explicit_renewal(s, runtime, prior, pilot, config):
    from onlyfans_runtime.launch_operations_models import OfLaunchCommand
    raw = config.get('offer_coverage')
    if raw is None or datetime.fromisoformat(raw['valid_until']) <= prior.expires_at:
        raise Refused('offer_explicit_renewal_required')
    # Only a later configure command that explicitly changed coverage can renew
    # authority. Stop/start and unrelated config retain equal before/after values.
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB
    before_doc, result_doc = cast(OfLaunchCommand.before_json, JSONB), cast(OfLaunchCommand.result_json, JSONB)
    # Select renewal evidence itself; unrelated status/config audits cannot push
    # a still-valid permission out of an arbitrary latest-N scan.
    commands = s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.account_id == runtime.account_id,
        OfLaunchCommand.binding_id == runtime.binding_id, OfLaunchCommand.kind == 'plan_c_scope',
        OfLaunchCommand.created_at > prior.created_at,
        result_doc['fingerprint'].astext == pilot.fingerprint, result_doc['status'].astext == 'configured',
        before_doc['offerCoverageAfter'] == raw,
        before_doc['offerCoverageAfter'].is_distinct_from(before_doc['offerCoverageBefore']))
        .order_by(OfLaunchCommand.created_at.desc()).limit(1))
    for command in commands:
        before, result = json.loads(command.before_json), json.loads(command.result_json)
        if (result.get('fingerprint') == pilot.fingerprint and result.get('status') == 'configured'
                and before.get('offerCoverageAfter') == raw
                and before.get('offerCoverageBefore') != raw):
            return {'commandKey': command.command_key, 'actor': command.actor,
                'requestHash': command.request_hash, 'coverage': raw}
    raise Refused('offer_explicit_renewal_required')


def _never_dispatched(s, runtime, prior, policy, state):
    """Terminal engine cancellation plus the final wire fence, under runtime/policy/intent locks."""
    from db.onlyfans_engine_models import OfEngineAttempt, OfEngineIndependentAdmission
    intent = s.get(OfTextIntent, (runtime.account_id, prior.used_intent_id), with_for_update=True)
    if (intent is None or intent.binding_id != runtime.binding_id or intent.fan_id != prior.fan_id
            or intent.catalog_item_id != prior.item_id or intent.status != 'cancelled' or intent.wire_version != 1
            or intent.wire_started_at is not None or intent.provider_message_id is not None
            or intent.receipt_json is not None or intent.projected_at is not None or intent.offer_id is not None
            or intent.reservation_held or policy.active_intent_id == intent.intent_id
            or intent.attempt_id is not None and state.unresolved_attempt_id == intent.attempt_id):
        raise Refused('offer_previous_dispatch_not_fenced')
    decisions = list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == runtime.account_id,
        OfEngineDecision.offer_grant_id == prior.grant_id)))
    if (len(decisions) != 1 or decisions[0].intent_id != intent.intent_id or decisions[0].status != 'cancelled'
            or decisions[0].binding_id != runtime.binding_id or decisions[0].fan_id != prior.fan_id):
        raise Refused('offer_previous_decision_not_retired')
    attempts = list(s.scalars(select(OfEngineAttempt).where(OfEngineAttempt.account_id == runtime.account_id,
        OfEngineAttempt.intent_id == intent.intent_id)))
    if any(a.outcome in ('accepted', 'unknown', 'not_accepted') for a in attempts):
        raise Refused('offer_previous_wire_evidence_present')
    if s.scalar(select(OfEngineIndependentAdmission.attempt_id).where(
            OfEngineIndependentAdmission.account_id == runtime.account_id,
            OfEngineIndependentAdmission.intent_id == intent.intent_id,
            OfEngineIndependentAdmission.status.in_(('dispatching', 'unknown'))).limit(1)) is not None:
        raise Refused('offer_previous_admission_unresolved')
    return {'intentId': intent.intent_id, 'decisionId': decisions[0].decision_id,
        'status': intent.status, 'wireVersion': intent.wire_version, 'wireStartedAt': None,
        'attemptId': intent.attempt_id, 'dispatchTokenHash': _hash(intent.dispatch_token),
        'attempts': [{'id': a.attempt_id, 'outcome': a.outcome} for a in attempts],
        'reason': intent.reason, 'fencedAt': intent.updated_at.isoformat()}


def _successor(s, runtime, owner, prior, coverage, policy):
    from db.models import OfConversationState
    from .pilot_scope import require, current_offer_coverage, delivery_block
    from .journey import backfill_complete
    from .history_scope import has_unscoped_messages
    from .writer_context import active_profile
    pilot, config = require(s, runtime, active=True)
    from .scope_contract import product_of
    state = s.get(OfConversationState, (runtime.account_id, prior.fan_id))
    profile = active_profile(runtime.account_id, session=s)
    if (config['authorization_mode'] != 'autonomous' or prior.fan_id not in config['fans']
            or product_of(config)['id'] != prior.item_id or not runtime.enabled or policy.enabled is not True
            or state is None or state.mode != 'automated' or state.takeover_state != 'none'
            or profile is None or not profile.may_offer(str(prior.item_id))
            or not backfill_complete(s, runtime.account_id, prior.fan_id)
            or has_unscoped_messages(s, runtime.account_id, prior.fan_id, runtime.binding_id)):
        raise Refused('offer_successor_not_eligible')
    blocked = delivery_block(s, runtime, prior.fan_id, commercial=True)
    if blocked:
        raise Refused(blocked)
    current_coverage = current_offer_coverage(s, runtime, config)
    if (coverage.evidence_ref != current_coverage.evidence_ref
            or coverage.valid_until != current_coverage.valid_until):
        raise Refused('offer_successor_coverage_mismatch')
    # Policy is already locked by approval; this locks only the old exact intent.
    proof = _never_dispatched(s, runtime, prior, policy, state) if prior.used_intent_id else None
    expired = prior.expires_at <= utcnow()
    renewal = _explicit_renewal(s, runtime, prior, pilot, config) if expired else None
    if proof is None and not expired:
        raise Refused('offer_successor_not_required')
    terms = _real_catalogue_terms(s, runtime.account_id, prior.item_id, price_cents=_grant_price(prior))
    if _hash(terms) != prior.terms_hash:
        raise Refused('offer_authorization_terms_need_review')
    _unowned(s, runtime.account_id, prior.fan_id, terms)
    if (not _sales_permits(owner, prior.item_id)
            or s.scalar(select(OfTextIntent.intent_id).where(OfTextIntent.account_id == runtime.account_id,
                OfTextIntent.binding_id == runtime.binding_id, OfTextIntent.fan_id == prior.fan_id,
                OfTextIntent.status.in_(('dispatching', 'unknown'))).limit(1)) is not None):
        raise Refused('offer_successor_delivery_hold')
    from onlyfans_runtime.quote import request_exists
    if request_exists(s, runtime.account_id, prior.fan_id, terms):
        raise Refused('an existing offer needs review')
    authority = _grant_authority(s, runtime, coverage, policy)
    authority.update({'predecessor': prior.grant_id, 'neverDispatched': proof, 'explicitRenewal': renewal})
    if _grant_stamp(prior) is not None:
        authority['terms'] = _grant_stamp(prior)
    successor = OfEngineOfferGrant(account_id=runtime.account_id, grant_id=str(uuid.uuid4()), fan_id=prior.fan_id,
        binding_id=runtime.binding_id, item_id=prior.item_id, terms_hash=prior.terms_hash,
        coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until if renewal else min(prior.expires_at, coverage.valid_until),
        issuance=prior.issuance + 1, predecessor_grant_id=prior.grant_id,
        issuance_reason='expired_renewal' if renewal else 'cancelled_unsent',
        authorization_json=json.dumps(authority, sort_keys=True, separators=(',', ':')), created_at=utcnow())
    s.add(successor);s.flush()
    return successor


def terms_successor(s, runtime, owner, prior, policy, *, variant=None, fresh_message=None):
    """A new grant for the same fan and product after the approved terms changed (new revision or discount variant).

    Allowed only while the prior card is provably unsent: no intent at all, or the exact terminal-cancellation proof of
    `_never_dispatched` (no wire start, no provider message, no receipt, no attempt evidence, no unresolved admission). An
    accepted, unknown, dispatching or wire-started card is never replaced: the existing-card path applies and nothing is repriced.
    The predecessor is kept; the successor carries issuance+1, its predecessor and the old/new revision and hashes; the unique
    successor constraint makes a racing second preparer see the same row.
    """
    from db.models import OfConversationState
    from onlyfans_runtime.catalogue_terms import live_terms
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id or prior.binding_id != runtime.binding_id
            or prior.fan_id != owner.fan_id or not _sales_permits(owner, prior.item_id)):
        raise Refused('offer_successor_not_eligible')
    if prior.expires_at <= utcnow():
        raise Refused('offer_successor_not_eligible')     # an expired grant is renewed only by explicit stopped renewal (_successor)
    existing = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == runtime.account_id,
        OfEngineOfferGrant.predecessor_grant_id == prior.grant_id))
    if existing is not None:
        return existing
    stamp = _grant_stamp(prior) or {}
    variant = variant or stamp.get('variant') or 'standard'
    if variant not in ('standard', 'discount'):
        raise Refused('offer_successor_variant_invalid')
    state = s.get(OfConversationState, (runtime.account_id, prior.fan_id))
    proof = _never_dispatched(s, runtime, prior, policy, state) if prior.used_intent_id else None
    live = live_terms(s, runtime.account_id, prior.item_id, lock=True)
    if live is None:
        raise Refused('offer_successor_terms_unavailable')
    price = live.discount_price_cents if variant == 'discount' else live.standard_price_cents
    if price is None:
        raise Refused('offer_successor_variant_unavailable')
    if variant == 'discount':
        from .price_intent import discount_blocked
        if proof is None and prior.used_intent_id is None and discount_blocked(s, runtime.account_id, prior.fan_id, prior.item_id):
            raise Refused('offer_successor_discount_blocked')
    resolve = _real_catalogue_terms if policy.transport_kind == 'fansapi' else catalog_terms
    terms = resolve(s, runtime.account_id, prior.item_id, price_cents=price)
    digest = _hash(terms)
    same_terms = digest == prior.terms_hash
    if same_terms:
        # A fresh particular request can replace a provably never-wired card.
        # Restart alone cannot replay it; unknown/accepted attempts fail the
        # existing _never_dispatched proof above.
        old = s.get(OfEngineDecision, (runtime.account_id, proof['decisionId'])) if proof else None
        old_message = s.get(Message, (runtime.account_id, prior.fan_id, old.source_message_id)) if old else None
        if (fresh_message is None or old_message is None
                or fresh_message.message_id == old_message.message_id
                or (fresh_message.created_at, fresh_message.message_id) <= (old_message.created_at, old_message.message_id)):
            raise Refused('offer_successor_needs_fresh_input')
    _unowned(s, runtime.account_id, prior.fan_id, terms)
    from onlyfans_runtime.quote import request_exists
    if request_exists(s, runtime.account_id, prior.fan_id, terms):
        raise Refused('an existing offer needs review')
    new = _terms_stamp(terms)
    authority = {'kind': 'cancelled_unsent_successor' if same_terms else 'terms_successor', 'predecessor': prior.grant_id, 'oldTermsHash': prior.terms_hash,
                 'newTermsHash': digest, 'oldRevision': stamp.get('revision'), 'newRevision': new['revision'],
                 'oldVariant': stamp.get('variant'), 'newVariant': new['variant'], 'terms': new, 'neverDispatched': proof}
    successor = OfEngineOfferGrant(account_id=runtime.account_id, grant_id=str(uuid.uuid4()), fan_id=prior.fan_id,
        binding_id=runtime.binding_id, item_id=prior.item_id, terms_hash=digest, coverage_ref=prior.coverage_ref,
        expires_at=prior.expires_at, issuance=prior.issuance + 1, predecessor_grant_id=prior.grant_id,
        issuance_reason='cancelled_unsent' if same_terms else 'discount_variant' if variant == 'discount' else 'terms_revision',
        authorization_json=json.dumps(authority, sort_keys=True, separators=(',', ':')), created_at=utcnow())
    try:
        with s.begin_nested():
            s.add(successor)
            s.flush()
    except IntegrityError:
        existing = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == runtime.account_id,
            OfEngineOfferGrant.predecessor_grant_id == prior.grant_id))
        if existing is None:
            raise Refused('offer preparation conflict needs review') from None
        return existing
    return successor


def active_engine_offer(s, runtime, owner, fan_id, *, exclude_intent_id=None):
    """The ONE live engine offer of this chat, or None (rule: one active engine offer per chat).

    A card is live while its send is in flight (queued / dispatching / unknown), or it was accepted, its offer record is still
    open/unlocked, the provider has not confirmed access to it, it was not deleted, and no later decline/reopening of the sale
    closed it. Closed cards stay retained evidence (frozen terms, exact price, message id, unlock reference); they simply no
    longer count against a new offer. Returns {'itemId', 'intentId', 'sendState'} or None.
    """
    from .offers import _cards
    boundary = None
    if owner is not None and owner.sales_mode in ('paused', 'reopened_scoped') and owner.sales_source_message_id is not None:
        source = s.get(Message, (runtime.account_id, fan_id, owner.sales_source_message_id))
        boundary = source.created_at if source is not None else None
    rows = s.execute(select(OfTextIntent, OfEngineDecision).join(OfEngineDecision,
        (OfEngineDecision.account_id == OfTextIntent.account_id) & (OfEngineDecision.intent_id == OfTextIntent.intent_id)).where(
        OfTextIntent.account_id == runtime.account_id, OfTextIntent.fan_id == fan_id, OfTextIntent.binding_id == runtime.binding_id,
        OfEngineDecision.action == 'offer', OfTextIntent.status.in_(('queued', 'dispatching', 'accepted', 'unknown')))
        .order_by(OfTextIntent.created_at.desc()).limit(20)).all()
    for intent, decision in rows:
        if intent.intent_id == exclude_intent_id:
            continue
        if intent.status != 'accepted':
            return {'itemId': decision.catalog_item_id, 'intentId': intent.intent_id, 'sendState': intent.status}
        if boundary is not None and intent.created_at <= boundary:
            continue                                   # the sale was closed (declined / reopened) after this card
        offer = s.get(ContentOffer, intent.offer_id) if intent.offer_id is not None else None
        message = s.get(Message, (runtime.account_id, fan_id, intent.provider_message_id)) if intent.provider_message_id else None
        if offer is None or offer.status not in ('open', 'unlocked') or message is None or message.is_unsent:
            continue
        access = s.scalar(select(OfEngineAccessEvidence.status).where(
            OfEngineAccessEvidence.account_id == runtime.account_id, OfEngineAccessEvidence.intent_id == intent.intent_id,
            OfEngineAccessEvidence.applied.is_(True)).order_by(OfEngineAccessEvidence.occurred_at.desc()).limit(1))
        if access == 'confirmed':
            continue                                   # he opened it: that sale is resolved
        return {'itemId': decision.catalog_item_id, 'intentId': intent.intent_id, 'sendState': 'accepted'}
    return None


def prepare_commercial_offer(store, tenant, account, fan_id, message_id, *, coverage_ref, valid_until):
    """Standing-permission preparation for a commercial account: the product comes from the fan-scoped shortlist, the amount from
    application rules, the grant from the same authority as every other card. No per-fan grant is injected.

    `coverage_ref`/`valid_until` are the account-level approved-catalogue evidence supplied by the scope contract (Step 2 hands it
    over from commercial.v1; the offline suites pass the same shape the pilot uses). Returns None for a non-commercial account.
    """
    from .catalogue import search_context
    from .commercial_scope import commercial_scope
    from .offers import match
    from .price_intent import variant_for
    from onlyfans_runtime.catalogue_terms import live_terms
    _id(fan_id); _id(message_id)
    if (type(coverage_ref) is not str or len(coverage_ref) > 128 or not isinstance(valid_until, datetime)
            or valid_until.tzinfo is not None or valid_until <= utcnow()):
        raise Refused('catalogue coverage is incomplete')
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        if not commercial_scope(s, runtime):
            return None
        owner = s.get(OfEngineOwner, (account, fan_id))
        policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
        message = s.get(Message, (account, fan_id, message_id))
        if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id or policy is None
                or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or not policy.media_enabled
                or not coverage_ref.startswith(_COVERAGE_PREFIX[policy.transport_kind])
                or message is None or message.direction != 'in' or message.is_unsent):
            return {'state': 'unavailable', 'reason': 'offer_not_authorized'}
        view = search_context(s, runtime, fan_id, message)
        permitted = [int(i['itemId']) for i in view.get('items', []) if _sales_permits(owner, int(i['itemId']))]
        item_id = permitted[0] if permitted else None
        if view['state'] != 'matches' or item_id is None:
            return {'state': 'none', 'reason': view['state']}
        from .catalogue_select import names_a_product
        if view.get('selectionRequired') or len(permitted) > 1:
            # The owner requires a particular product selection, including generic requests.
            # Never let model choice or item ordering choose what card this fan receives.
            return {'state': 'ambiguous', 'itemIds': [str(i) for i in permitted]}
        live_offer = active_engine_offer(s, runtime, owner, fan_id)
        if live_offer is not None and live_offer['itemId'] != item_id:
            return {'state': 'active_offer', 'itemId': str(live_offer['itemId'])}
        cards = match(s, runtime, fan_id, message.body, include_cards=True).get('cards', [])
        if any(card['itemId'] == str(item_id) and card['sendState'] in ('queued', 'dispatching', 'accepted', 'unknown') for card in cards):
            return {'state': 'existing_card', 'itemId': str(item_id)}
        variant = variant_for(s, runtime, fan_id, item_id, message.body)
        live = live_terms(s, account, item_id)
        price = live.discount_price_cents if variant == 'discount' else live.standard_price_cents
        resolve = _real_catalogue_terms if policy.transport_kind == 'fansapi' else catalog_terms
        try:
            terms = resolve(s, account, item_id, price_cents=price)
            _unowned(s, account, fan_id, terms)
            digest = _hash(terms)
            prior = s.scalar(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == account,
                OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.item_id == item_id)
                .order_by(OfEngineOfferGrant.created_at.desc(), OfEngineOfferGrant.issuance.desc()).limit(1))
            if prior is None:
                stamp = _terms_stamp(terms)
                grant = OfEngineOfferGrant(account_id=account, grant_id=str(uuid.uuid4()), fan_id=fan_id,
                    binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest, coverage_ref=coverage_ref,
                    expires_at=valid_until, issuance=0, issuance_reason='initial',
                    authorization_json=json.dumps({'kind': 'commercial_initial', 'coverageRef': coverage_ref, 'terms': stamp},
                                                  sort_keys=True, separators=(',', ':')), created_at=utcnow())
                s.add(grant)
                s.flush()
            elif prior.terms_hash == digest and prior.used_intent_id is None and prior.expires_at > utcnow():
                grant = prior
            elif prior.terms_hash == digest:
                if prior.used_intent_id is None:
                    return {'state': 'held', 'reason': 'offer_authorization_needs_review', 'itemId': str(item_id)}
                grant = terms_successor(s, runtime, owner, prior, policy, variant=variant, fresh_message=message)
            else:
                grant = terms_successor(s, runtime, owner, prior, policy, variant=variant)
            verified_terms(s, runtime, owner, grant)
        except Refused as exc:
            if str(exc) in {'catalogue media already owned', 'bundle overlaps owned media'}:
                return {'state': 'owned', 'itemId': str(item_id)}
            return {'state': 'held', 'reason': str(exc)[:64], 'itemId': str(item_id)}
        return {'state': 'prepared', 'grantId': grant.grant_id, 'itemId': str(item_id), 'variant': variant, 'priceCents': price}


def commercial_coverage(s, runtime, config):
    """(evidence_ref, valid_until) from the commercial scope's own durable catalogue approval, or None with a reason.

    The approval block is the agency's: this account, this binding, an explicit UTC expiry. Nothing is inferred; a missing,
    foreign or expired block yields no coverage and therefore no new grant (ordinary chat is unaffected).
    """
    from datetime import datetime as _dt
    block = config.get('catalogue')
    if block is None:
        return None, 'offer_coverage_missing'
    if block['account_id'] != runtime.account_id or block['binding_id'] != runtime.binding_id:
        return None, 'offer_coverage_scope_changed'
    expiry = _dt.fromisoformat(block['valid_until'])
    if expiry <= utcnow():
        return None, 'offer_coverage_expired'
    return (block['evidence_ref'], expiry), None


def prepare_scoped_offer(store, tenant, account, fan_id, message_id, engine):
    """The one preparation entry of the real worker, routed by the account's own scope contract.

    legacy pilot (scope_id 1)  -> `prepare_pilot_offer`, unchanged
    commercial.v1 scope        -> `prepare_commercial_offer` with the catalogue reference and expiry of the scope's durable
                                  approval (standing agency permission; no per-fan grant exists before this call)
    preparation / no scope     -> None (no offer authority of any kind)

    A commercial result carries `commercial: True`. It is never a reason to stop the conversation: when the paid action is
    unavailable (no coverage, held, owned, ambiguous, nothing suitable) the writer simply gets no approved offer and chats.
    """
    from .pilot_scope import current
    from . import scope_contract as contracts
    with Session(store.engine) as s:
        runtime = store._runtime(s, tenant, account, lock=False)
        pilot, config = current(s, runtime)
        if pilot is None:
            return None
        if not contracts.is_commercial(config):
            coverage = reason = None
        else:
            coverage, reason = commercial_coverage(s, runtime, config)
            audience = config['fans']
    if coverage is None and reason is None:
        return prepare_pilot_offer(store, tenant, account, fan_id, message_id, engine)
    if reason is not None or fan_id not in audience or pilot.status != 'active':
        return {'state': 'unavailable', 'reason': reason or 'offer_not_authorized', 'commercial': True}
    result = prepare_commercial_offer(store, tenant, account, fan_id, message_id, coverage_ref=coverage[0], valid_until=coverage[1])
    return None if result is None else {**result, 'commercial': True}


def _real_catalogue_terms(s, account, item_id, *, lock=True, price_cents=None):
    """Shared preparation/availability/send provenance checks; read-only when lock=False."""
    from db.models import VaultItem
    terms = catalog_terms(s, account, item_id, lock=lock, price_cents=price_cents)
    for saved in terms['files']:
        media = s.get(VaultItem, (account, saved['media_id']))
        if media is None or media.removed_at is not None or media.content_hash_kind != 'vault-metadata-v1':
            raise Refused('media is not a synced real vault item')
    return terms


def verified_terms(s, runtime, owner, grant, *, intent_id=None, lock=True):
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or grant is None or type(getattr(grant, 'grant_id', None)) is not str
            or grant.binding_id != runtime.binding_id or grant.fan_id != owner.fan_id
            or not grant.coverage_ref.startswith(('synthetic:', 'vault:')) or grant.expires_at <= utcnow()
            or grant.used_intent_id not in (None, intent_id) or not _sales_permits(owner, grant.item_id)):
        raise Refused('offer authorization is stale or unavailable')
    if s.scalar(select(OfEngineOfferGrant.grant_id).where(OfEngineOfferGrant.account_id == runtime.account_id,
            OfEngineOfferGrant.predecessor_grant_id == grant.grant_id).limit(1)) is not None:
        raise Refused('offer authorization has a successor')
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
    terms = (_real_catalogue_terms(s, runtime.account_id, grant.item_id, lock=lock, price_cents=_grant_price(grant))
             if policy.transport_kind == 'fansapi' else
             catalog_terms(s, runtime.account_id, grant.item_id, lock=lock, price_cents=_grant_price(grant)))
    if _hash(terms) != grant.terms_hash:
        raise Refused('approved offer terms changed')
    _unowned(s, runtime.account_id, owner.fan_id, terms)
    from onlyfans_runtime.quote import request_exists
    if request_exists(s, runtime.account_id, owner.fan_id, terms):
        raise Refused('an existing offer needs review')
    if terms.get('commercial') is not None:
        live_offer = active_engine_offer(s, runtime, owner, owner.fan_id, exclude_intent_id=intent_id)
        if live_offer is not None and live_offer['itemId'] != grant.item_id:
            raise Refused('another offer is active in this chat')
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


def _approve_catalogue_offer(s, runtime, fan_id, item_id, coverage, *, price_cents=None):
    """Shared transaction path for explicit setup and standing-policy preparation."""
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
    policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or policy is None or policy.transport_kind != 'fansapi' or not policy.media_enabled
            or not _sales_permits(owner, item_id)):
        raise Refused('catalogue offer is not authorized')
    terms = _real_catalogue_terms(s, account, item_id, price_cents=price_cents)
    _unowned(s, account, fan_id, terms)
    digest = _hash(terms)
    prior = _latest_grant(s, account, fan_id, item_id, digest)
    if prior is None:
        try:
            with s.begin_nested():
                authority = _grant_authority(s, runtime, coverage, policy)
                if _terms_stamp(terms) is not None:
                    authority['terms'] = _terms_stamp(terms)
                prior = OfEngineOfferGrant(account_id=account, grant_id=str(uuid.uuid4()), fan_id=fan_id,
                    binding_id=runtime.binding_id, item_id=item_id, terms_hash=digest,
                    coverage_ref=coverage.evidence_ref, expires_at=coverage.valid_until,
                    issuance=0, issuance_reason='initial',
                    authorization_json=json.dumps(authority, sort_keys=True, separators=(',', ':')),
                    created_at=utcnow())
                s.add(prior)
                s.flush()
        except IntegrityError:
            # Another legitimate preparer won the existing unique terms slot.
            # Validate that record; never delete uniqueness or mint another key.
            prior = _latest_grant(s, account, fan_id, item_id, digest)
            if prior is None:
                raise Refused('offer preparation conflict needs review') from None
    if prior.binding_id != runtime.binding_id:
        raise Refused('existing offer authorization needs review')
    if prior.used_intent_id is not None or prior.expires_at <= utcnow():
        prior = _successor(s, runtime, owner, prior, coverage, policy)
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
    evidence remains visible on the same request. Expiry can issue a successor
    only from retained explicit stopped renewal evidence; old grants never change.
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
        if pilot is None or 'product' not in config:
            return None     # no pilot, or a commercial scope: no standing pilot offer before the catalogue contract exists
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
            prior = _latest_grant(s, account, fan_id, item_id, _hash(terms))
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
                    or unlock.amount_cents is not None and unlock.amount_cents != intent.price_cents or unlock.occurred_at != stamp
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
