"""Supervised-pilot approval gate for 'fansapi' accounts (Plan C, handoff 5.C).

    draft -> validated, awaiting approval -> explicit approved immutable version -> revalidated delivery authorization -> dispatch

Nothing here sends. Every function takes the `store` (Store) plus tenant/account and raises `Refused` on refusal.
Synthetic accounts never use this module (`is_gated`). No function approves or hands back automatically; the only
way a draft becomes sendable is `approve` called with a named human actor (automated actor names are refused).

One row of `of_engine_approval` is one immutable VERSION of one draft sequence (account, request_id, generation).
An edit inserts version+1 (on generation+1, whose decision rows carry the new text) and invalidates the prior version, so an approved version is never rewritten.
`draft_hash` is sha256 over the ordered parts (index, action, body, payload hash, catalogue item, offer grant, full
verified terms incl. price/media/product version, currency) plus account, creator binding, fan, source message,
generation, input base, owner epoch, control revision and connection epoch. It is recomputed from CURRENT canonical
state every time `authorize_send` runs, so approved text can never carry a different card or a changed price/media.

API for the operator HTTP layer (all return plain dicts, `requestId`/`generation`/`version` identify an approval):
    pending(store, tenant, account, fan_id=None)                              list live (awaiting/approved) versions
    describe(store, tenant, account, request_id, generation)                  latest version with exact parts
    approve(store, tenant, account, request_id, generation, *, expected_version, actor)      idempotent
    reject(store, tenant, account, request_id, generation, *, expected_version, actor, reason='')
    edit(store, tenant, account, request_id, generation, *, expected_version, actor, parts)   new version, awaiting
          (decision rows are immutable, so an edit supersedes the draft with generation+1; use the returned generation)
    current(store, tenant, account, request_id)                               newest version of a request (any generation)
          `parts` are plain texts, same count as the draft; for an offer part the text is the lead-in, the application
          re-renders the card line from the frozen terms. Same claims/price/pitch/journey validation as the model output.
    invalidate_for_change(store, tenant, account, fan_id, reason)             drop live approvals of a fan
    expire_stale(store, tenant, account)                                      mark past-expiry versions expired
    authorize_send(store, tenant, account, decision_id)                       full revalidation; Refused(reason) unless sendable
    send is done by RealConversationWorker.send_approved(tenant, account, decision_id).
Takeover, resume, mark-complete and handback are NOT approvals: they only invalidate (owner epoch changes) and a
handback makes the conversation eligible for a fresh plan, never for replaying an old draft.
"""
import json
import os
import uuid
from dataclasses import asdict
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Fan, Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy
from db.onlyfans_engine_models import (OfEngineApproval, OfEngineDecision, OfEngineOfferGrant, OfEngineOwner,
                                       OfEngineRequest, OfFansConnection)
from db.onlyfans_models import OfIngress
from onlyfans_runtime.store import Refused, Store, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from .history_scope import current_binding_message
from .webhook_conflict import conflicted_sources

GATED_TRANSPORT = 'fansapi'
LIVE = ('awaiting', 'approved')
CURRENCY = 'USD'
# Reasons that only mean "this version is not approved (yet)": nothing is cancelled for them.
PASSIVE_REASONS = frozenset({'approval_required', 'approval_missing', 'approval_expired'})
MAX_EDIT_GENERATION = 50
_AUTOMATED_ACTORS = frozenset({'engine', 'offline-engine', 'system', 'worker', 'scheduler', 'bot', 'cron', 'ai', 'model'})


def approval_ttl_seconds():
    """Operator response window (default 30 min, 1 min .. 24 h). Applies to a waiting draft and, from the moment of
    approval, to the time allowed for delivery."""
    try:
        value = int(os.environ.get('TWIN_ENGINE_APPROVAL_TTL_SECONDS', '1800'))
    except ValueError:
        value = 1800
    return min(max(value, 60), 86400)


def is_gated(s, account):
    policy = s.get(OfDeliveryPolicy, account)
    return policy is not None and policy.transport_kind == GATED_TRANSPORT


def _hash(value):
    from .commerce import _hash as digest
    return digest(value)


def _actor(actor):
    if (type(actor) is not str or not actor.strip() or len(actor) > 128 or actor.strip().lower() in _AUTOMATED_ACTORS
            or actor.strip().lower().startswith(('auto', 'engine'))):
        raise Refused('approval needs a named human operator')
    return actor.strip()


def _generation_parts(s, account, request_id, generation):
    return list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
        OfEngineDecision.request_id == request_id, OfEngineDecision.generation == generation)
        .order_by(OfEngineDecision.part_index)))


def _latest(s, account, request_id, generation, *, lock=False):
    query = select(OfEngineApproval).where(OfEngineApproval.account_id == account,
        OfEngineApproval.request_id == request_id, OfEngineApproval.generation == generation
        ).order_by(OfEngineApproval.version.desc()).limit(1)
    return s.scalar(query.with_for_update(read=True) if lock == 'read' else query.with_for_update() if lock else query)


def _view(row):
    return {'requestId': row.request_id, 'generation': row.generation, 'version': row.version, 'status': row.status,
            'fanId': row.fan_id, 'sourceMessageId': row.source_message_id, 'draftHash': row.draft_hash,
            'createdBy': row.created_by, 'actor': row.actor, 'authorizationMode':row.authorization_mode,
            'decidedAt': row.decided_at.isoformat() if row.decided_at else None,
            'expiresAt': row.expires_at.isoformat(), 'invalidationReason': row.invalidation_reason,
            'parts': json.loads(row.parts_json)}


# ------------------------------------------------------------------ the world an approval depends on
def newer_input_reason(s, runtime, decision, request):
    """None when nothing newer from the fan exists, else the reason (shared with RealConversationWorker.recheck).

    Pending (accepted, not yet applied) webhook messages count, and the latest inbound message must still be this
    request's source. Callers outside a transaction drain the inbox first; the in-transaction guard cannot."""
    pending = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == runtime.account_id,
        OfIngress.fan_id == decision.fan_id, OfIngress.status == 'pending').limit(1))
    if pending is not None:
        return 'new_inbound_pending'
    latest = _latest_inbound(s, runtime, decision.fan_id)
    if latest is None or latest.message_id != request.source_message_id:
        return 'newer_inbound'
    return None


def _latest_inbound(s, runtime, fan_id):
    return s.scalar(select(Message).where(Message.account_id == runtime.account_id, Message.fan_id == fan_id,
        Message.direction == 'in', Message.is_unsent.is_(False), current_binding_message(runtime.binding_id))
        .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))


def _build(s, runtime, parts, request, owner, state):
    """(digest, shown_parts, reason): the draft hash recomputed from CURRENT canonical state."""
    from .commerce import verified_terms
    account = runtime.account_id
    connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account, OfFansConnection.status == 'active'))
    completed = sum(1 for p in parts if p.status == 'completed')
    canon_parts, shown = [], []
    for p in parts:
        terms = None
        if p.action == 'offer':
            grant = s.get(OfEngineOfferGrant, (account, p.offer_grant_id)) if p.offer_grant_id else None
            try:
                terms = verified_terms(s, runtime, owner, grant, intent_id=p.intent_id)
            except Refused as exc:
                return None, None, 'offer_terms: ' + str(exc)
            if p.terms_json is None or json.loads(p.terms_json) != terms or p.catalog_item_id != terms['item']['id']:
                return None, None, 'offer_terms: changed since the draft'
        elif p.terms_json is not None or p.catalog_item_id is not None or p.offer_grant_id is not None:
            return None, None, 'reply carries commercial terms'
        wire = TextAuthority._wire_hash(account, p.fan_id, p.body, terms['media_ids'] if terms else (),
                                        terms['preview_ids'] if terms else (), terms['price_cents'] if terms else 0)
        if wire != p.payload_hash:
            return None, None, 'payload_changed'
        canon_parts.append({'i': p.part_index, 'action': p.action, 'body': p.body, 'payload_hash': wire,
                            'catalog_item_id': p.catalog_item_id, 'offer_grant_id': p.offer_grant_id, 'terms': terms,
                            'currency': CURRENCY if terms else None})
        shown.append({'decision_id': p.decision_id, 'part_index': p.part_index, 'action': p.action, 'body': p.body, 'payload_hash': wire,
                      'catalog_item_id': p.catalog_item_id, 'offer_grant_id': p.offer_grant_id,
                      'price_cents': terms['price_cents'] if terms else None, 'currency': CURRENCY if terms else None,
                      'media_ids': terms['media_ids'] if terms else [], 'preview_ids': terms['preview_ids'] if terms else [],
                      'terms_hash': _hash(terms) if terms else None})
    policy = s.get(OfDeliveryPolicy, account)
    if policy is None:
        return None, None, 'delivery_policy_unavailable'
    from .pilot_scope import current as current_pilot
    try:
        pilot, pilot_config = current_pilot(s, runtime)
    except Refused:
        return None, None, 'pilot_scope_unavailable'
    if pilot is not None:
        reason = offer_pin_reason(pilot_config, canon_parts)
        if reason:
            return None, None, reason
    from .writer_context import CreatorProfile, active_profile
    profile = active_profile(account, session=s)
    if profile is not None and type(profile) is not CreatorProfile:
        return None, None, 'creator_profile_unavailable'
    profile_hash = _hash(asdict(profile)) if profile is not None else None
    canon = {'v': 3, 'account': account, 'binding': runtime.binding_id, 'fan': parts[0].fan_id,
             'creator_profile_hash': profile_hash,
             'delivery_policy_revision': policy.revision, 'delivery_enabled': policy.enabled,
             'request': request.request_id, 'source_message_id': request.source_message_id,
             'generation': parts[0].generation, 'owner_epoch': owner.epoch, 'control_revision': state.revision,
             'connection_epoch': connection.epoch if connection else None,
             'input_base': state.input_revision - completed, 'parts': canon_parts}
    if pilot is not None:
        canon['pilot_scope'] = {'revision': pilot.revision, 'fingerprint': pilot.fingerprint, 'status': pilot.status}
    return _hash(canon), shown, None


def offer_scope_current(s, runtime, pilot_config):
    """Is the scope's sale authority still current for a card? Legacy: the one pinned product. Commercial: the agency's durable
    catalogue approval (this binding, unexpired). Each card's own frozen terms are re-verified separately by `_build`."""
    from .pilot_scope import _product_current
    if 'product' in pilot_config:
        return _product_current(s, runtime.account_id, pilot_config['product'])
    from .commerce import commercial_coverage
    return commercial_coverage(s, runtime, pilot_config)[0] is not None


def offer_pin_reason(pilot_config, canon_parts):
    """Why a draft's offers are outside the scope's authority, or None.

    Legacy: outside the one pinned product. Commercial: a card must carry frozen approved terms (the `commercial` block) for the
    catalogue item it names; a card without them (a legacy-shaped quote) has no authority in a commercial scope."""
    offers = [part for part in canon_parts if part['action'] == 'offer']
    if not offers:
        return None
    if 'product' not in pilot_config:
        for part in offers:
            terms = part['terms']
            if (not isinstance(terms.get('commercial'), dict) or terms.get('item', {}).get('id') != part['catalog_item_id']):
                return 'commercial_product_contract_unavailable'
        return None
    expected = pilot_config['product']
    for part in offers:
        terms = part['terms']
        if (part['catalog_item_id'] != expected['id'] or terms['price_cents'] != expected['price_cents']
                or tuple(map(str, terms['media_ids'])) != tuple(map(str, expected['media_ids']))
                or tuple(map(str, terms['preview_ids'])) != tuple(map(str, expected['preview_ids']))):
            return 'pilot_product_scope_changed'
    return None


def evaluate(s, runtime, decision, *, now=None, phase='send', need_approval=True, controls=False, lock=False):
    """(reason | None, approval row | None, digest, shown). The single judgement used by every gate.

    phase 'draft': every part must still be an unqueued ready draft. phase 'send': `decision` must be ready/queued,
    earlier parts completed. controls=True adds the delivery holds (unresolved sends, restrictions, cases) that
    TextAuthority applies itself inside its own transaction."""
    now = now or utcnow()
    account, fan_id = runtime.account_id, decision.fan_id
    policy = s.get(OfDeliveryPolicy, account)
    if policy is None or policy.transport_kind != GATED_TRANSPORT:
        return 'not_gated', None, None, None
    from .pilot_scope import current as current_pilot, policy_matches, _product_current
    try:
        pilot, pilot_config = current_pilot(s, runtime)
    except Refused:
        return 'pilot_scope_unavailable', None, None, None
    if pilot is not None:
        if (fan_id not in pilot_config['fans'] or pilot.status == 'stopped'
                or phase == 'send' and pilot.status != 'active'
                or not policy_matches(policy, pilot_config, enabled=pilot.status == 'active')
                or decision.action == 'offer' and not offer_scope_current(s, runtime, pilot_config)):
            return 'pilot_scope_unavailable', None, None, None
    from fans_api_adapter.real_transport import real_transport_enabled
    if phase == 'send':   # a draft may be prepared while sending is stopped; nothing can go out until these hold
        if not policy.enabled:
            return 'delivery_disabled', None, None, None
        if not runtime.enabled:
            return 'account_disabled', None, None, None
        if not real_transport_enabled():
            return 'kill_switch', None, None, None
    request = s.get(OfEngineRequest, (account, decision.request_id))
    if request is None or request.fan_id != fan_id or request.binding_id != runtime.binding_id or request.status != 'open':
        return 'request_unavailable', None, None, None
    owner = s.get(OfEngineOwner, (account, fan_id))
    state = s.get(OfConversationState, (account, fan_id))
    if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
            or decision.binding_id != runtime.binding_id):
        return 'owner_changed', None, None, None
    if owner.epoch != decision.owner_epoch:
        return 'owner_epoch_changed', None, None, None
    if state is None or state.mode != 'automated' or state.takeover_state != 'none':
        return 'conversation_not_automated', None, None, None
    if state.revision != decision.control_revision:
        return 'control_changed', None, None, None
    from .pilot_scope import current as current_pilot
    pilot, config = current_pilot(s, runtime)
    if pilot is not None:
        from .pilot_ingress import review_pending
        if review_pending(s, runtime, pilot, config, fan_id):
            return 'pilot_contact_review_pending', None, None, None
    if (fan_id, request.source_message_id) in conflicted_sources(s, account):
        return 'webhook_conflict', None, None, None
    latest = _latest_inbound(s, runtime, fan_id)
    from .controls import explicit_stop
    if latest is not None and explicit_stop(latest.body):
        return 'opt_out', None, None, None
    reason = newer_input_reason(s, runtime, decision, request)
    if reason:
        return reason, None, None, None
    parts = _generation_parts(s, account, decision.request_id, decision.generation)
    if [p.part_index for p in parts] != list(range(len(parts))) or decision.decision_id not in {p.decision_id for p in parts}:
        return 'parts_incomplete', None, None, None
    if phase == 'draft':
        if any(p.status != 'ready' or p.intent_id is not None for p in parts):
            return 'draft_not_pending', None, None, None
    else:
        for p in parts:
            allowed = {'ready', 'queued'} if p.decision_id == decision.decision_id else {'ready', 'queued', 'completed'}
            if p.status not in allowed:
                return 'part_not_sendable:' + p.status, None, None, None
    from .sequence import prior_state
    sequence_reason = prior_state(s, runtime, decision, state) if phase == 'send' else (
        None if state.input_revision == parts[0].input_revision else 'conversation_changed')
    if sequence_reason:
        return 'sequence_' + sequence_reason, None, None, None
    from .memory import current as memory_current
    if not memory_current(s, runtime, decision):
        return 'memory_changed', None, None, None
    if decision.connection_epoch is not None:
        connection = s.get(OfFansConnection, (account, decision.connection_epoch))
        if connection is None or connection.status != 'active' or connection.binding_id != runtime.binding_id:
            return 'connection_changed', None, None, None
    if controls:
        from .controls import reason as hold_reason
        fan = s.get(Fan, (account, fan_id))
        from .independence import certified_anchor
        independent = certified_anchor(s, runtime, decision, state, policy) is not None
        hold = hold_reason(s, account, fan_id, state, fan,
                           unresolved=Store._unresolved(s, account, fan_id, state) and not independent,
                           commercial=decision.action == 'offer')
        if hold:
            return hold, None, None, None
    digest, shown, reason = _build(s, runtime, parts, request, owner, state)
    if reason:
        return reason, None, None, None
    if not need_approval:
        return None, None, digest, shown
    row = _latest(s, account, decision.request_id, decision.generation, lock='read' if lock else False)
    if row is None:
        return 'approval_missing', None, digest, shown
    if row.status == 'awaiting':
        return 'approval_required', row, digest, shown
    if row.status != 'approved':
        return 'approval_' + row.status, row, digest, shown
    if row.expires_at <= now:
        return 'approval_expired', row, digest, shown
    if (row.draft_hash != digest or row.fan_id != fan_id or row.binding_id != runtime.binding_id
            or row.source_message_id != request.source_message_id or row.owner_epoch != owner.epoch
            or row.control_revision != state.revision):
        return 'approval_mismatch', row, digest, shown
    if pilot is not None:
        from .pilot_scope import authorize
        if row.authorization_mode != pilot_config['authorization_mode']:
            return 'pilot_authorization_mode_changed', row, digest, shown
        if row.authorization_mode == 'autonomous':
            if row.actor != 'autonomous-policy:' + pilot.fingerprint:
                return 'pilot_automatic_authorization_invalid', row, digest, shown
        else:
            try:
                authorize(pilot_config, row.actor, 'approve')
            except Refused:
                return 'pilot_operator_unauthorized', row, digest, shown
    return None, row, digest, shown


# ------------------------------------------------------------------ in-transaction gates used by delivery.py
def require_in_session(s, runtime, decision):
    """Called while TextAuthority creates the intent: no current matching approval, no intent."""
    reason, _, _, _ = evaluate(s, runtime, decision, phase='send', lock=True)
    if reason:
        raise Refused('engine approval: ' + reason)


def check_in_session(s, runtime, decision):
    """Called at admission and again immediately before the wire marker is written. None = still authorized."""
    return evaluate(s, runtime, decision, phase='send', lock=True)[0]


def invalidate_generation_in_session(s, account, request_id, generation, reason):
    count = 0
    for row in s.scalars(select(OfEngineApproval).where(OfEngineApproval.account_id == account,
            OfEngineApproval.request_id == request_id, OfEngineApproval.generation == generation,
            OfEngineApproval.status.in_(LIVE)).with_for_update()):
        row.status, row.invalidation_reason, row.updated_at = 'invalidated', reason[:64], utcnow()
        count += 1
    return count


def invalidate_fan_in_session(s, account, fan_id, reason, *, keep_source=None):
    count = 0
    for row in s.scalars(select(OfEngineApproval).where(OfEngineApproval.account_id == account,
            OfEngineApproval.fan_id == fan_id, OfEngineApproval.status.in_(LIVE)).with_for_update()):
        if keep_source is not None and row.source_message_id == keep_source:
            continue
        row.status, row.invalidation_reason, row.updated_at = 'invalidated', reason[:64], utcnow()
        count += 1
    return count


def consume_in_session(s, decision):
    """The last part of an approved sequence settled: the approval is used up (same transaction as the receipt)."""
    parts = _generation_parts(s, decision.account_id, decision.request_id, decision.generation)
    if not parts or any(p.status != 'completed' for p in parts):
        return False
    row = _latest(s, decision.account_id, decision.request_id, decision.generation, lock=True)
    if row is None or row.status != 'approved':
        return False
    row.status, row.updated_at = 'consumed', utcnow()
    return True


# ------------------------------------------------------------------ operations
def create_draft_approval(store, tenant, account, decision_id, *, created_by='engine'):
    """Stage a freshly planned draft (all parts) as version 1 awaiting approval. Idempotent: an existing version of
    this generation is returned as it is, never revived. Raises Refused when the world already moved."""
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        decision = s.get(OfEngineDecision, (account, decision_id))
        if decision is None or decision.binding_id != runtime.binding_id:
            raise Refused('engine decision unavailable')
        if not is_gated(s, account):
            raise Refused('approval gate applies to fansapi accounts only')
        s.get(OfEngineRequest, (account, decision.request_id), with_for_update=True)
        row = _latest(s, account, decision.request_id, decision.generation, lock=True)
        if row is not None:
            return _view(row)
        parts = _generation_parts(s, account, decision.request_id, decision.generation)
        if any(p.status == 'held' for p in parts):
            return {'requestId': decision.request_id, 'generation': decision.generation, 'status': 'held', 'version': 0}
        reason, _, digest, shown = evaluate(s, runtime, decision, phase='draft', need_approval=False)
        if reason:
            raise Refused(reason)
        state = s.get(OfConversationState, (account, decision.fan_id))
        now = utcnow()
        from .pilot_scope import current as current_pilot
        pilot, config = current_pilot(s, runtime)
        mode = config['authorization_mode'] if pilot is not None else 'supervised'
        row = OfEngineApproval(authorization_mode=mode, account_id=account, request_id=decision.request_id, generation=decision.generation, version=1,
            fan_id=decision.fan_id, binding_id=runtime.binding_id,
            source_message_id=s.get(OfEngineRequest, (account, decision.request_id)).source_message_id,
            owner_epoch=decision.owner_epoch, control_revision=state.revision, input_revision=parts[0].input_revision,
            connection_epoch=decision.connection_epoch, status='awaiting',
            parts_json=json.dumps(shown, sort_keys=True, separators=(',', ':'), ensure_ascii=False), draft_hash=digest,
            created_by=created_by, expires_at=now + timedelta(seconds=approval_ttl_seconds()), created_at=now, updated_at=now)
        s.add(row)
        s.flush()
        return _view(row)


def _locked_version(s, runtime, account, request_id, generation, expected_version):
    s.get(OfEngineRequest, (account, request_id), with_for_update=True)
    row = _latest(s, account, request_id, generation, lock=True)
    if row is None:
        raise Refused('approval not found')
    from .pilot_scope import current as current_pilot
    pilot, config = current_pilot(s, runtime)
    if row.binding_id != runtime.binding_id or pilot is not None and row.fan_id not in config['fans']:
        raise Refused('approval not found')
    if type(expected_version) is not int or row.version != expected_version:
        raise Refused('approval version changed')
    return row


def approve(store, tenant, account, request_id, generation, *, expected_version, actor):
    actor = _actor(actor)
    failure = None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .pilot_scope import current as current_pilot, authorize
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            authorize(pilot_config, actor, 'approve')
        row = _locked_version(s, runtime, account, request_id, generation, expected_version)
        if row.authorization_mode != 'supervised':
            raise Refused('this draft uses autonomous authorization')
        if row.status == 'approved':
            return dict(_view(row), duplicate=True)
        if row.status != 'awaiting':
            raise Refused('approval is ' + row.status)
        now = utcnow()
        if row.expires_at <= now:
            row.status, row.invalidation_reason, row.updated_at = 'expired', 'ttl', now
            failure = 'approval expired'
        else:
            parts = _generation_parts(s, account, request_id, generation)
            reason, _, digest, _ = evaluate(s, runtime, parts[0], phase='draft', need_approval=False) if parts else ('parts_incomplete', None, None, None)
            if reason is None and digest != row.draft_hash:
                reason = 'draft_changed'
            if reason:
                row.status, row.invalidation_reason, row.updated_at = 'invalidated', reason[:64], now
                failure = 'draft is stale: ' + reason
            else:
                row.status, row.actor, row.decided_at, row.updated_at = 'approved', actor, now, now
                row.expires_at = now + timedelta(seconds=approval_ttl_seconds())
                return dict(_view(row), duplicate=False)
    raise Refused(failure)


def authorize_automatic(store, tenant, account, request_id, generation, *, expected_version):
    """Record explicit machine policy authorization; never impersonate a human approver."""
    failure = None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .pilot_scope import require
        pilot, config = require(s, runtime, active=True)
        if config['authorization_mode'] != 'autonomous':
            raise Refused('autonomous authorization is not configured')
        row = _locked_version(s, runtime, account, request_id, generation, expected_version)
        if row.authorization_mode != 'autonomous':
            raise Refused('draft authorization mode changed')
        if row.status == 'approved':
            return dict(_view(row), duplicate=True)
        if row.status != 'awaiting':
            raise Refused('authorization is ' + row.status)
        now = utcnow()
        if row.expires_at <= now:
            row.status, row.invalidation_reason, row.updated_at = 'expired', 'ttl', now
            failure = 'authorization expired'
        else:
            parts = _generation_parts(s, account, request_id, generation)
            reason, _, digest, _ = evaluate(s, runtime, parts[0], phase='draft', need_approval=False) if parts else ('parts_incomplete', None, None, None)
            if reason is None and digest != row.draft_hash:
                reason = 'draft_changed'
            if reason:
                row.status, row.invalidation_reason, row.updated_at = 'invalidated', reason[:64], now
                failure = 'draft is stale: ' + reason
            else:
                row.status, row.actor, row.decided_at, row.updated_at = 'approved', 'autonomous-policy:' + pilot.fingerprint, now, now
                row.expires_at = now + timedelta(seconds=approval_ttl_seconds())
                return dict(_view(row), duplicate=False)
    raise Refused(failure)


def reject(store, tenant, account, request_id, generation, *, expected_version, actor, reason=''):
    actor = _actor(actor)
    note = ('rejected: ' + reason.strip())[:64] if type(reason) is str and reason.strip() else 'rejected'
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .pilot_scope import current as current_pilot, authorize
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            authorize(pilot_config, actor, 'reject')
        row = _locked_version(s, runtime, account, request_id, generation, expected_version)
        if row.status in ('rejected',) or (row.status == 'invalidated' and row.invalidation_reason.startswith('revoked')):
            return dict(_view(row), duplicate=True)
        if row.status not in LIVE:
            raise Refused('approval is ' + row.status)
        now = utcnow()
        if row.status == 'awaiting':
            row.status, row.actor, row.decided_at, row.invalidation_reason, row.updated_at = 'rejected', actor, now, note, now
        else:  # an approved version is revoked (kept as evidence of the approval), not rewritten
            row.status, row.invalidation_reason, row.updated_at = 'invalidated', ('revoked: ' + actor)[:64], now
        parts = _generation_parts(s, account, request_id, generation)
        for p in parts:
            if p.status == 'ready' and p.intent_id is None:
                p.status, p.updated_at = 'cancelled', now
        request = s.get(OfEngineRequest, (account, request_id))
        if request.status == 'open':
            request.status, request.next_step, request.updated_at = 'cancelled', 'draft_rejected', now
            request.revision += 1
        return _view(row)


def edit(store, tenant, account, request_id, generation, *, expected_version, actor, parts):
    from .claims import check_plain_text
    from .engine import SyntheticProposal, offer_body
    actor = _actor(actor)
    if (type(parts) not in (list, tuple) or not 1 <= len(parts) <= 4
            or any(type(t) is not str or not t.strip() or len(t.encode('utf-8')) > 4000 for t in parts)):
        raise Refused('invalid edited text')
    if any(ch in t for t in parts for ch in ('\u2014', '\u2013')):
        raise Refused('edited text must not contain dashes used as punctuation marks')
    failure = None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .pilot_scope import current as current_pilot, authorize
        pilot, pilot_config = current_pilot(s, runtime)
        if pilot is not None:
            authorize(pilot_config, actor, 'edit')
        row = _locked_version(s, runtime, account, request_id, generation, expected_version)
        if row.status not in LIVE:
            raise Refused('approval is ' + row.status)
        decisions = _generation_parts(s, account, request_id, generation)
        if not decisions or any(p.status != 'ready' or p.intent_id is not None for p in decisions):
            raise Refused('draft is already in delivery')
        if len(parts) != len(decisions):
            raise Refused('an edit keeps the number of parts')
        now = utcnow()
        reason, _, _, _ = evaluate(s, runtime, decisions[0], phase='draft', need_approval=False)
        if reason:
            row.status, row.invalidation_reason, row.updated_at = 'invalidated', reason[:64], now
            failure = 'draft is stale: ' + reason
        else:
            owner = s.get(OfEngineOwner, (account, decisions[0].fan_id))
            request = s.get(OfEngineRequest, (account, request_id))
            source = s.get(Message, (account, request.fan_id, request.source_message_id))
            head = decisions[0]
            from .writer_context import CreatorProfile, active_profile
            from .model_output import JudgeContext, edit_problems
            profile = active_profile(account, session=s)
            if type(profile) is not CreatorProfile or profile.missing_inputs():
                raise Refused('creator profile is unavailable for edit validation')
            from .commercial_scope import terms_mode_view
            from .writer_context import may_offer_item
            if head.action == 'offer' and not may_offer_item(profile, head.catalog_item_id, terms_mode_view(s, runtime)):
                raise Refused('creator has not approved this offer')
            from .engine import _offer_grants, _price_information
            from .offers import match as match_offers
            reference = match_offers(s, runtime, request.fan_id, source.body, include_cards=True)
            from .commercial_scope import terms_mode_view
            context = JudgeContext(snapshot={'current_message': {'body': source.body},
                'offer_reference': reference,
                'price_information': _price_information(s, runtime, source.body),
                'offer_grants': _offer_grants(s, runtime, request.fan_id), **terms_mode_view(s, runtime)}, profile=profile)
            problems = edit_problems([part.strip() for part in parts], context)
            if problems:
                raise Refused('edited draft violates creator or pilot rules: ' + problems[0])
            proposal = SyntheticProposal(body=parts[0].strip(), action=head.action,
                                         parts=tuple(t.strip() for t in parts[1:]) if head.action == 'reply' else (),
                                         offer_grant_id=head.offer_grant_id)
            if head.action == 'offer' and len(parts) != 1:
                raise Refused('an offer draft has one part')
            prices = {offer['priceCents'] for offer in reference['offers']}
            from .claims import grounding_from_snapshot
            check_plain_text([proposal.body, *proposal.parts], proposal, owner.sales_mode, prices,
                             grounding=grounding_from_snapshot(context.snapshot, proposal))
            from .journey import enforce_journey, journey_context
            enforce_journey(journey_context(s, runtime, owner, request, source), proposal)
            generation_next = generation + 1
            if s.scalar(select(OfEngineDecision.decision_id).where(OfEngineDecision.account_id == account,
                    OfEngineDecision.request_id == request_id, OfEngineDecision.generation >= generation_next).limit(1)) is not None:
                raise Refused('only the latest draft can be edited')
            if generation_next >= MAX_EDIT_GENERATION:
                raise Refused('too many edits of one request')
            # Decision rows are immutable evidence: an edit supersedes them with a new generation carrying the new text.
            from db.onlyfans_engine_models import OfEngineMemoryBinding
            fresh_parts = []
            for p, text in zip(decisions, parts):
                terms = json.loads(p.terms_json) if p.terms_json else None
                body = offer_body(text, terms) if p.action == 'offer' else text.strip()
                fresh = OfEngineDecision(account_id=account, decision_id=str(uuid.uuid4()), request_id=request_id,
                    generation=generation_next, part_index=p.part_index, fan_id=p.fan_id, binding_id=p.binding_id,
                    owner_epoch=p.owner_epoch, connection_epoch=p.connection_epoch, input_revision=p.input_revision,
                    control_revision=p.control_revision, action=p.action, offer_grant_id=p.offer_grant_id,
                    catalog_item_id=p.catalog_item_id, terms_json=p.terms_json, body=body,
                    payload_hash=TextAuthority._wire_hash(account, p.fan_id, body, terms['media_ids'] if terms else (),
                        terms['preview_ids'] if terms else (), terms['price_cents'] if terms else 0),
                    snapshot_hash=p.snapshot_hash, status='ready', created_at=now, updated_at=now)
                s.add(fresh)
                s.flush()
                binding = s.get(OfEngineMemoryBinding, (account, p.decision_id))
                if binding is None:
                    raise Refused('fan memory binding is unavailable')
                s.add(OfEngineMemoryBinding(account_id=account, decision_id=fresh.decision_id, fan_id=binding.fan_id,
                    source_message_id=binding.source_message_id, binding_id=binding.binding_id, source_sha256=binding.source_sha256,
                    status=binding.status, fingerprint=binding.fingerprint, offer_fingerprint=binding.offer_fingerprint, created_at=now))
                p.status, p.updated_at = 'cancelled', now
                fresh_parts.append(fresh)
            row.status, row.invalidation_reason, row.updated_at = 'invalidated', 'edited', now
            s.flush()
            reason, _, digest, shown = evaluate(s, runtime, fresh_parts[0], phase='draft', need_approval=False)
            if reason:
                raise Refused('edited draft is not valid: ' + reason)
            state = s.get(OfConversationState, (account, head.fan_id))
            fresh_row = OfEngineApproval(authorization_mode=row.authorization_mode, account_id=account, request_id=request_id, generation=generation_next,
                version=row.version + 1, fan_id=row.fan_id, binding_id=row.binding_id, source_message_id=row.source_message_id,
                owner_epoch=row.owner_epoch, control_revision=state.revision, input_revision=row.input_revision,
                connection_epoch=row.connection_epoch, status='awaiting',
                parts_json=json.dumps(shown, sort_keys=True, separators=(',', ':'), ensure_ascii=False),
                draft_hash=digest, created_by=actor, expires_at=now + timedelta(seconds=approval_ttl_seconds()),
                created_at=now, updated_at=now)
            s.add(fresh_row)
            s.flush()
            return _view(fresh_row)
    raise Refused(failure)


def retire_dead_draft(store, tenant, account, decision_id):
    """A draft whose approval was invalidated or expired while it never reached delivery is cancelled so that a fresh
    plan may be made (this is what a handback or an explicit new turn allows). Returns True when it did."""
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        decision = s.get(OfEngineDecision, (account, decision_id))
        if decision is None or decision.binding_id != runtime.binding_id:
            return False
        request = s.get(OfEngineRequest, (account, decision.request_id), with_for_update=True)
        parts = _generation_parts(s, account, decision.request_id, decision.generation)
        row = _latest(s, account, decision.request_id, decision.generation, lock=True)
        if (row is None or row.status not in ('invalidated', 'expired') or request is None or request.status != 'open'
                or any(p.status != 'ready' or p.intent_id is not None for p in parts)):
            return False
        for p in parts:
            p.status, p.updated_at = 'cancelled', utcnow()
        request.next_step, request.updated_at = 'prepare_reply', utcnow()
        request.revision += 1
        return True


def invalidate_for_change(store, tenant, account, fan_id, reason, *, keep_source=None):
    """Drop every live approval of one fan (control change, newer fan input, catalogue change...). Count returned."""
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        return invalidate_fan_in_session(s, account, fan_id, reason, keep_source=keep_source)


def retire_superseded(store, tenant, account, fan_id, keep_source_message_id):
    """A newer fan message arrived: older unsent drafts of that fan lose their approval and are cancelled."""
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        if not is_gated(s, account):
            return 0
        latest = _latest_inbound(s, runtime, fan_id)
        if latest is None or latest.message_id != keep_source_message_id:
            return 0
        count = invalidate_fan_in_session(s, account, fan_id, 'newer_inbound', keep_source=keep_source_message_id)
        for request in s.scalars(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.fan_id == fan_id, OfEngineRequest.binding_id == runtime.binding_id,
                OfEngineRequest.status == 'open', OfEngineRequest.source_message_id != keep_source_message_id
                ).with_for_update()):
            parts = list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                OfEngineDecision.request_id == request.request_id)))
            # Only entirely never-queued drafts can be superseded here. A sent,
            # queued, or uncertain part remains with the delivery/recovery owner.
            if not parts or any(p.intent_id is not None or p.status not in ('ready', 'cancelled') for p in parts):
                continue
            for p in parts:
                p.status, p.updated_at = 'cancelled', utcnow()
            request.status = 'cancelled'
            request.next_step = 'superseded_by_message:' + str(keep_source_message_id)
            request.updated_at = utcnow()
            request.revision += 1
        return count


def expire_stale(store, tenant, account):
    count = 0
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        from .pilot_scope import current as current_pilot
        pilot, config = current_pilot(s, runtime)
        query = select(OfEngineApproval).where(OfEngineApproval.account_id == account,
                OfEngineApproval.status.in_(LIVE), OfEngineApproval.expires_at <= utcnow())
        if pilot is not None:
            query = query.where(OfEngineApproval.fan_id.in_(config['fans']))
        for row in s.scalars(query.with_for_update()):
            row.status, row.invalidation_reason, row.updated_at = 'expired', 'ttl', utcnow()
            count += 1
    return count


def pending(store, tenant, account, fan_id=None):
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=False)
        from .pilot_scope import current as current_pilot
        pilot, config = current_pilot(s, runtime)
        query = select(OfEngineApproval).where(OfEngineApproval.account_id == account,
            OfEngineApproval.status.in_(LIVE)).order_by(OfEngineApproval.created_at).limit(200)
        if pilot is not None:
            query = query.where(OfEngineApproval.fan_id.in_(config['fans']))
        if fan_id is not None:
            query = query.where(OfEngineApproval.fan_id == fan_id)
        return [_view(row) for row in s.scalars(query)]


def current(store, tenant, account, request_id):
    """Newest approval version of one request, whichever generation it belongs to."""
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account, lock=False)
        row = s.scalar(select(OfEngineApproval).where(OfEngineApproval.account_id == account,
            OfEngineApproval.request_id == request_id).order_by(OfEngineApproval.generation.desc(),
            OfEngineApproval.version.desc()).limit(1))
        if row is None:
            raise Refused('approval not found')
        return _view(row)


def describe(store, tenant, account, request_id, generation):
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account, lock=False)
        row = _latest(s, account, request_id, generation)
        if row is None:
            raise Refused('approval not found')
        return _view(row)


def authorize_send(store, tenant, account, decision_id):
    """Full revalidation for ONE part. Returns the approval summary or raises Refused(reason).

    Recomputes the draft hash from current canonical state and requires: approved + unexpired + matching version;
    owner mode active and unchanged epoch; conversation automated with no takeover; no newer fan input (incl. pending
    webhooks); no opt-out; sales/offer terms, price, media and availability unchanged; delivery enabled; kill switch
    (TWIN_ENGINE_FANSAPI_REAL) on; delivery holds clear. TextAuthority repeats the approval check atomically when the
    intent is created and again immediately before the wire marker."""
    expired = None
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account, lock=False)
        decision = s.get(OfEngineDecision, (account, decision_id))
        if decision is None or decision.binding_id != runtime.binding_id:
            raise Refused('engine decision unavailable')
        reason, row, _, _ = evaluate(s, runtime, decision, phase='send', controls=True)
        if reason == 'approval_expired':
            expired = (decision.request_id, decision.generation)
        elif reason is None:
            return {'requestId': row.request_id, 'generation': row.generation, 'version': row.version,
                    'draftHash': row.draft_hash, 'actor': row.actor, 'expiresAt': row.expires_at.isoformat()}
    if expired is not None:
        with Session(store.engine) as s, s.begin():
            store._runtime(s, tenant, account)
            row = _latest(s, account, *expired, lock=True)
            if row is not None and row.status == 'approved' and row.expires_at <= utcnow():
                row.status, row.invalidation_reason, row.updated_at = 'expired', 'ttl', utcnow()
    raise Refused(reason)
