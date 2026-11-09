"""Offline-only engine orchestration around canonical inbox and delivery records.

In-process fixture models are always accepted. A real model is accepted only as a
ModelRuntime (model_runtime.py): default OFF, allow-listed models and accounts,
reserved budget, calls made outside database locks, and every engine control
re-checked after the call. The model has no send or charge authority.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import ENGINE_TRANSPORT_KINDS, OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision, OfEngineOwner, OfEngineRequest, OfEngineOfferGrant, OfFansConnection
from db.onlyfans_models import OfIngress
from db.onlyfans_moxy_models import MoxyAccount
from onlyfans_runtime.store import Refused, _id, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from .history_scope import current_binding_message, has_unscoped_messages
from .webhook_conflict import conflicted_sources

MAX_GENERATIONS = 5  # lab bound on regenerations of one request, not an approved budget


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


def offer_body(lead, terms):
    """Draft lead-in plus the application-rendered card line (label and price from verified terms)."""
    label = terms['item'].get('label') or terms['item']['description_for_ai']
    if type(label) is not str or not label.strip() or len(label.encode()) > 2000:
        raise Refused('approved item label is unavailable')
    cents = terms['price_cents']
    body = f"{lead.strip()}\n\n{label.strip()}, USD {cents // 100}.{cents % 100:02d}."
    if len(body.encode('utf-8')) > 4000:
        raise Refused('answer plus offer exceeds the message limit')
    return body


@dataclass(frozen=True)
class SyntheticProposal:
    body: str
    sales_signal: str = 'none'
    item_scope: tuple[str, ...] = ()
    action: str = 'reply'
    offer_grant_id: str | None = None
    parts: tuple[str, ...] = ()
    # Model declarations about commercial content in `body`/`parts`. They are
    # validated in plan(); a declaration never grants permission to sell.
    pitch: bool = False
    price_claims: tuple[int, ...] = ()
    purchase_claim: bool = False


class _AlreadyPlanned(Exception):
    """Another worker finished this request generation while a model call was in flight."""
    def __init__(self, decision_id):
        super().__init__(decision_id)
        self.decision_id = decision_id


def _latest_generation(s, account, request_id):
    rows = list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
        OfEngineDecision.request_id == request_id).order_by(OfEngineDecision.generation.desc(),
        OfEngineDecision.part_index)))
    if not rows:
        return []
    return [row for row in rows if row.generation == rows[0].generation]


def _proved_unsent(s, account, parts):
    """True only when every part of a generation is cancelled/held with no send in doubt."""
    for part in parts:
        if part.status not in {'cancelled', 'held'}:
            return False
        if part.intent_id is None:
            continue
        intent = s.get(OfTextIntent, (account, part.intent_id))
        if intent is None:
            return False
        if intent.status == 'cancelled' and part.status == 'cancelled':
            continue
        attempts = list(s.scalars(select(OfEngineAttempt).where(OfEngineAttempt.account_id == account,
            OfEngineAttempt.intent_id == part.intent_id)))
        if (part.status == 'held' and intent.status == 'not_accepted' and attempts
                and all(a.outcome == 'not_accepted' and a.evidence_json for a in attempts)):
            continue
        return False
    return True



def _unchanged_offer_hold(s, runtime, existing, snapshot, memory_fingerprint, offer_fingerprint):
    """Reuse the existing dependency record; never spend another call on an unchanged unavailable offer."""
    from db.onlyfans_engine_models import OfEngineMemoryBinding
    head = existing[0] if existing else None
    if (head is None or head.status != 'held' or head.action != 'offer'
            or any(part.intent_id is not None for part in existing)
            or head.owner_epoch != snapshot['owner_epoch'] or head.connection_epoch != snapshot['connection_epoch']
            or head.input_revision != snapshot['input_revision'] or head.control_revision != snapshot['control_revision']):
        return False
    binding = s.get(OfEngineMemoryBinding, (runtime.account_id, head.decision_id))
    grant = s.get(OfEngineOfferGrant, (runtime.account_id, head.offer_grant_id)) if head.offer_grant_id else None
    if (binding is None or binding.binding_id != runtime.binding_id or grant is None
            or binding.fingerprint != memory_fingerprint or binding.offer_fingerprint != offer_fingerprint):
        return False
    # A repaired/replaced authority wakes this same request even if unrelated
    # catalogue/memory fingerprints did not change. No new store or timer.
    return not any(g['itemId'] == str(grant.item_id) for g in snapshot.get('offer_grants', []))


def _offer_grants(s, runtime, fan_id):
    """Approved, unexpired, unused offer grants for this fan whose frozen terms still match the catalogue."""
    from .commerce import verified_terms
    owner = s.get(OfEngineOwner, (runtime.account_id, fan_id))
    out = []
    grants = s.scalars(select(OfEngineOfferGrant).where(OfEngineOfferGrant.account_id == runtime.account_id,
        OfEngineOfferGrant.fan_id == fan_id, OfEngineOfferGrant.binding_id == runtime.binding_id,
        OfEngineOfferGrant.used_intent_id.is_(None), OfEngineOfferGrant.expires_at > utcnow())
        .order_by(OfEngineOfferGrant.grant_id).limit(6)).all()
    for grant in grants[:5]:
        try:
            terms = verified_terms(s, runtime, owner, grant, lock=False)
        except Refused:
            continue
        item = terms['item']
        kinds = [f.get('kind') for f in terms.get('files', [])]
        out.append({'grantId': grant.grant_id, 'itemId': str(grant.item_id),
                    'label': item.get('label') or item.get('description_for_ai'),
                    'priceCents': terms['price_cents'], 'currency': 'USD',
                    'description': item.get('description_for_ai'), 'mediaCount': len(terms['media_ids']),
                    # Payload facts: the only quantities any text may state about this card's contents.
                    'media': {'total': len(terms['media_ids']),
                              'pictures': sum(1 for k in kinds if k in {'image', 'photo'}),
                              'videos': sum(1 for k in kinds if k == 'video')}})
    return out



def _price_information(s, runtime):
    """Current approved listing fact only; never a grant or sale availability."""
    from .pilot_scope import current, current_offer_coverage
    from .writer_context import active_profile
    from .commerce import _real_catalogue_terms
    pilot, config = current(s, runtime)
    if pilot is None or pilot.status != 'active':
        return []
    product = config['product']
    profile = active_profile(runtime.account_id, session=s)
    if profile is None or not profile.may_offer(product['id']):
        return []
    try:
        current_offer_coverage(s, runtime, config)
        terms = _real_catalogue_terms(s, runtime.account_id, product['id'], lock=False)
    except Refused:
        return []
    if (terms['price_cents'] != product['price_cents'] or terms['media_ids'] != product['media_ids']
            or terms['preview_ids'] != product['preview_ids']):
        return []
    item = terms['item']
    return [{'itemId': str(product['id']), 'label': item.get('label') or item.get('description_for_ai'),
             'priceCents': terms['price_cents'], 'currency': 'USD'}]


def _verified_purchases(s, runtime, fan_id):
    """Catalogue items whose every media file the fan verifiably owns; None when ownership evidence is unavailable."""
    from db.models import CatalogItem
    from onlyfans_runtime.media_delivery import catalog_terms
    from ownership import owned_media_in_session
    try:
        owned = set(owned_media_in_session(s, runtime.account_id, fan_id, limit=10000))
        policy = s.get(OfDeliveryPolicy, runtime.account_id)
        if policy is not None and policy.transport_kind == 'fansapi':
            from db.onlyfans_fans_intake_models import OfFansEvidence
            unresolved_reversal = s.scalar(select(OfFansEvidence.evidence_id).where(
                OfFansEvidence.account_id == runtime.account_id, OfFansEvidence.fan_id == fan_id,
                OfFansEvidence.kind == 'chargeback', OfFansEvidence.status.in_(('pending', 'held'))).limit(1))
            if unresolved_reversal is not None:
                return None
            from .commerce import provider_unlocked_media
            reported_unlocks = provider_unlocked_media(s, runtime, fan_id)
            # An unlock is message-specific historical access, not proof of
            # current ownership or financial settlement. Keep it for duplicate
            # offer prevention, but do not promote it to a model purchase claim.
            if reported_unlocks - owned:
                return None
    except (ValueError, Refused):
        return None
    items = list(s.scalars(select(CatalogItem).where(CatalogItem.account_id == runtime.account_id,
        CatalogItem.enabled.is_(True), CatalogItem.script_id.is_(None)).order_by(CatalogItem.id).limit(201)))
    if len(items) > 200:
        return None
    out = []
    for item in items:
        try:
            terms = catalog_terms(s, runtime.account_id, item.id, lock=False)
        except Refused:
            continue
        media = set(terms['media_ids'])
        if media and media <= owned:
            out.append({'itemId': str(item.id), 'label': item.label or item.description_for_ai})
    return out


class SyntheticModel:
    """Deterministic fixture; no credentials, I/O, fallback or model inference."""
    def __init__(self, replies):
        self.replies = dict(replies)
        self.calls = []

    def generate(self, snapshot):
        self.calls.append(snapshot)
        proposal = self.replies.get(snapshot['current_message']['body'])
        if type(proposal) is not SyntheticProposal:
            raise Refused('synthetic model has no reviewed fixture for this message')
        return proposal


class EngineService:
    def __init__(self, store, model, *, draft_only=False):
        from .model_contract import StructuredFixtureModel
        from .model_runtime import ModelRuntime
        if type(model) not in (SyntheticModel, StructuredFixtureModel, ModelRuntime) or type(draft_only) is not bool:
            raise Refused('only fixture models or a configured ModelRuntime are available')
        self.store = store
        self.engine = store.engine
        self.model = model
        self.draft_only = draft_only

    def _planning_policy_ok(self, policy):
        if policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS:
            return False
        # Draft-only work is allowed only while the shared delivery policy is
        # durably disabled; no process-local switch can make it send.
        if self.draft_only:
            return policy.transport_kind == 'fansapi' and policy.enabled is False
        return policy.enabled is True

    def select_simulated(self, tenant, account, fan_id, *, source_message_id=None):
        """Select the existing engine owner, with a narrow proved-independent exception."""
        _id(fan_id)
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            from .pilot_scope import planning_scope
            planning_scope(s, runtime, fan_id, self.model, draft_only=self.draft_only)
            policy = s.get(OfDeliveryPolicy, account)
            state = s.get(OfConversationState, (account, fan_id))
            if (not self._planning_policy_ok(policy)
                    or state is None or state.mode != 'automated' or state.takeover_state != 'none'):
                raise Refused('synthetic conversation is not ready')
            if self.store._unresolved(s, account, fan_id, state):
                from .independence import can_prepare_identity
                if not can_prepare_identity(s, runtime, fan_id, source_message_id, state, policy):
                    raise Refused('synthetic conversation is not ready')
            if s.get(MoxyAccount, account) is not None:
                raise Refused('existing account ownership requires reconciliation')
            row = s.get(OfEngineOwner, (account, fan_id))
            if row is not None:
                if row.binding_id != runtime.binding_id or row.mode != 'active':
                    raise Refused('engine owner requires explicit review')
                return row.epoch
            row = OfEngineOwner(account_id=account, fan_id=fan_id, binding_id=runtime.binding_id,
                                mode='active', epoch=1, revision=0, sales_mode='open',
                                sales_scope_json='[]', updated_at=utcnow())
            s.add(row)
            return row.epoch

    def _respect_explicit_stop(self, tenant, account, fan_id, source_message_id):
        # Independently preserve a fan's direct opt-out without running the
        # legacy conversation policy or trusting a model's classification.
        from .controls import explicit_stop
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            message = s.get(Message, (account, fan_id, source_message_id))
            if message is None or not explicit_stop(message.body):
                return False
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            ingress = s.scalar(select(OfIngress).where(OfIngress.account_id == account,
                OfIngress.fan_id == fan_id, OfIngress.message_id == source_message_id,
                OfIngress.binding_id == runtime.binding_id, OfIngress.status == 'applied',
                OfIngress.source != 'import').limit(1))
            if (owner is None or owner.binding_id != runtime.binding_id or state is None or
                    policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS or ingress is None or
                    message.direction != 'in' or message.is_unsent or message.created_at < runtime.activated_at):
                raise Refused('live canonical incoming message required')
            request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.fan_id == fan_id, OfEngineRequest.source_message_id == source_message_id))
            if request is None:
                request = OfEngineRequest(account_id=account, request_id=str(uuid.uuid4()), fan_id=fan_id,
                    binding_id=runtime.binding_id, source_message_id=source_message_id, status='open',
                    owner='engine', next_step='review_contact_stop', revision=0, created_at=utcnow(), updated_at=utcnow())
                s.add(request); s.flush()
            request_id, expected_revision, already_stopped = request.request_id, state.revision, state.mode == 'no_contact'
        if not already_stopped:
            key = 'engine-stop:' + _digest([account, fan_id, source_message_id])[:52]
            for attempt in range(3):
                try:
                    self.store.control(tenant, account, fan_id, mode='no_contact', expected_revision=expected_revision,
                        command_key=key, actor='offline-engine', reason=f'Fan requested no contact in message {source_message_id}',
                        action='mark_no_contact', synthetic_engine_only=True)
                    break
                except Refused as exc:
                    if str(exc) != 'control revision conflict' or attempt == 2:
                        raise
                    with Session(self.engine) as s, s.begin():
                        runtime = self.store._runtime(s, tenant, account)
                        state = s.get(OfConversationState, (account, fan_id))
                        owner = s.get(OfEngineOwner, (account, fan_id))
                        policy = s.get(OfDeliveryPolicy, account)
                        if (state is None or owner is None or owner.binding_id != runtime.binding_id
                                or policy is None or policy.transport_kind not in ENGINE_TRANSPORT_KINDS):
                            raise Refused('synthetic engine control scope unavailable')
                        expected_revision = state.revision
                        if state.mode == 'no_contact':
                            break
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            request = s.get(OfEngineRequest, (account, request_id))
            state = s.get(OfConversationState, (account, fan_id))
            if request is None or request.binding_id != runtime.binding_id or state is None or state.mode != 'no_contact':
                raise Refused('contact stop needs review')
            if request.next_step != 'no_contact':
                request.status, request.next_step, request.updated_at = 'declined', 'no_contact', utcnow()
                request.revision += 1
        return True

    def plan(self, tenant, account, fan_id, source_message_id):
        _id(fan_id); _id(source_message_id)
        if self._respect_explicit_stop(tenant, account, fan_id, source_message_id):
            raise Refused('fan requested no contact')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            from .pilot_scope import planning_scope
            pilot_stamp = planning_scope(s, runtime, fan_id, self.model, draft_only=self.draft_only)
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                    or not self._planning_policy_ok(policy)
                    or state is None or state.mode != 'automated' or state.takeover_state != 'none'):
                raise Refused('engine owner is unavailable')
            message = s.get(Message, (account, fan_id, source_message_id))
            ingress = s.scalar(select(OfIngress).where(OfIngress.account_id == account,
                OfIngress.fan_id == fan_id, OfIngress.message_id == source_message_id,
                OfIngress.binding_id == runtime.binding_id, OfIngress.status == 'applied',
                OfIngress.source != 'import').limit(1))
            if message is None or message.direction != 'in' or message.is_unsent or ingress is None or message.created_at < runtime.activated_at:
                raise Refused('live canonical incoming message required')
            request = s.scalar(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.fan_id == fan_id, OfEngineRequest.source_message_id == source_message_id))
            if request is None:
                request = OfEngineRequest(account_id=account, request_id=str(uuid.uuid4()), fan_id=fan_id,
                    binding_id=runtime.binding_id, source_message_id=source_message_id, status='open',
                    owner='engine', next_step='prepare_reply', revision=0, created_at=utcnow(), updated_at=utcnow())
                s.add(request); s.flush()
            existing = _latest_generation(s, account, request.request_id)
            observed_generation = existing[0].generation if existing else None
            if existing and (request.status != 'open' or existing[0].generation + 1 >= MAX_GENERATIONS
                             or not _proved_unsent(s, account, existing)):
                return existing[0].decision_id
            if not existing and request.status in {'handover_pending', 'handover_accepted'}:
                raise Refused('request is with a human')
            if (fan_id, source_message_id) in conflicted_sources(s, account):
                raise Refused('source message has conflicting webhook evidence')
            latest = s.scalar(select(Message).where(Message.account_id == account, Message.fan_id == fan_id,
                Message.direction == 'in', Message.is_unsent.is_(False), current_binding_message(runtime.binding_id)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))
            stale = latest is None or latest.message_id != source_message_id
            if stale:
                # Newer input supersedes this request; close it so it cannot linger as open work.
                if request.status == 'open':
                    request.status, request.next_step = 'cancelled', 'superseded_by_newer_input'
                else:
                    request.next_step = 'superseded_by_newer_input'
                request.updated_at = utcnow()
                request.revision += 1
            else:
                history = list(s.scalars(select(Message).where(Message.account_id == account, Message.fan_id == fan_id,
                    Message.is_unsent.is_(False), current_binding_message(runtime.binding_id)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(101)))
                connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account, OfFansConnection.status == 'active'))
                connection_epoch = connection.epoch if connection else None
                pending_rows = s.execute(select(OfEngineDecision, OfTextIntent.status).outerjoin(OfTextIntent,
                    (OfTextIntent.account_id == OfEngineDecision.account_id) &
                    (OfTextIntent.intent_id == OfEngineDecision.intent_id)).where(
                    OfEngineDecision.account_id == account, OfEngineDecision.fan_id == fan_id,
                    OfEngineDecision.binding_id == runtime.binding_id,
                    OfEngineDecision.status.not_in(('completed', 'cancelled')))
                    .order_by(OfEngineDecision.created_at.desc(), OfEngineDecision.decision_id.desc()).limit(51)).all()
                open_rows = s.scalars(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                    OfEngineRequest.fan_id == fan_id, OfEngineRequest.binding_id == runtime.binding_id,
                    OfEngineRequest.status.not_in(('completed', 'declined', 'cancelled')))
                    .order_by(OfEngineRequest.created_at.desc(), OfEngineRequest.request_id.desc()).limit(51)).all()
                from .memory import projection as memory_projection
                memory_view, memory_fingerprint = memory_projection(s, runtime, fan_id, message)
                from .offers import match as match_offers
                offer_match = match_offers(s, runtime, fan_id, message.body, include_cards=True)
                from .catalogue import search_context
                catalogue_view = search_context(s, runtime, fan_id, message)
                from .memory import references_fingerprint
                offer_fingerprint = references_fingerprint(account, policy, offer_match, catalogue_view, s=s)
                request_text = dict(s.execute(select(Message.message_id, Message.body).where(
                    Message.account_id == account, Message.fan_id == fan_id,
                    Message.message_id.in_([r.source_message_id for r in open_rows]),
                    current_binding_message(runtime.binding_id))).all())
                snapshot = {'account_id': account, 'fan_id': fan_id, 'binding_id': runtime.binding_id,
                    'owner_epoch': owner.epoch, 'connection_epoch': connection_epoch, 'input_revision': state.input_revision, 'control_revision': state.revision,
                    'sales_mode': owner.sales_mode, 'sales_scope': json.loads(owner.sales_scope_json),
                    'current_message': {'id': source_message_id, 'body': message.body},
                    'history': [{'id': row.message_id, 'direction': row.direction, 'body': row.body,
                        'timestamp': row.created_at.isoformat() if row.created_at else None,
                        'price_cents': row.price_cents, 'media_count': row.media_count,
                        'paid': row.is_paid} for row in reversed(history[:100])],
                    'history_truncated': len(history) > 100,
                    'history_scope_incomplete': has_unscoped_messages(s, account, fan_id, runtime.binding_id),
                    'memory': memory_view,
                    'offer_reference': offer_match,
                    'catalogue_search': catalogue_view,
                    'pending_actions': [{'decision_id': d.decision_id, 'request_id': d.request_id,
                        'generation': d.generation, 'action': d.action, 'outcome': intent_status or d.status, 'body': d.body,
                        'terms': json.loads(d.terms_json) if d.terms_json else None}
                        for d, intent_status in pending_rows[:50]],
                    'pending_actions_truncated': len(pending_rows) > 50,
                    'open_requests': [{'request_id': r.request_id, 'source_message_id': r.source_message_id,
                        'source_body': request_text.get(r.source_message_id),
                        'status': r.status, 'owner': r.owner, 'next_step': r.next_step} for r in open_rows[:50]],
                    'open_requests_truncated': len(open_rows) > 50}
                if pilot_stamp is not None:
                    snapshot['pilot_scope'] = pilot_stamp
                if type(self.model).__name__ == 'ModelRuntime':
                    # Extra writer context, built only for a real model so fixture snapshots stay unchanged.
                    snapshot['offer_grants'] = _offer_grants(s, runtime, fan_id)
                    snapshot['price_information'] = _price_information(s, runtime)
                    snapshot['verified_purchases'] = _verified_purchases(s, runtime, fan_id)
                # JOURNEY-HOOK-1 (A7): deterministic journey facts for the model; no-op unless the account is 'fansapi'.
                from .journey import journey_context
                _journey = journey_context(s, runtime, owner, request, message)
                if _journey:
                    snapshot['journey'] = _journey
                if type(self.model).__name__ == 'ModelRuntime' and _unchanged_offer_hold(
                        s, runtime, existing, snapshot, memory_fingerprint, offer_fingerprint):
                    return existing[0].decision_id
                request_id, epoch, binding = request.request_id, owner.epoch, runtime.binding_id
        if stale:
            raise Refused('newer incoming message needs review')
        from .model_contract import StructuredFixtureModel
        from .model_runtime import ModelRuntime, ModelTurn
        if type(self.model) not in (SyntheticModel, StructuredFixtureModel, ModelRuntime):
            raise Refused('only fixture models or a configured ModelRuntime are available')
        if type(self.model) is ModelRuntime:
            turn = ModelTurn(account=account, fan_id=fan_id, request_id=request_id,
                generation=0 if observed_generation is None else observed_generation + 1, binding_id=binding,
                owner_epoch=epoch, snapshot=snapshot,
                snapshot_sha256=_digest({'snapshot': snapshot, 'model_config': self.model.config.version()})
                    if self.model.config.upstream_provider else _digest(snapshot),
                recheck=lambda: self._model_recheck(tenant, account, fan_id, source_message_id, request_id, epoch,
                    binding, snapshot, observed_generation, memory_fingerprint, offer_fingerprint),
                handover=lambda reason, detail=None: self._handover_model(tenant, account, request_id,
                    self.model.config.handover_target, reason, detail))
            try:
                proposal = self.model.propose(turn)  # reserve/call/settle each use their own short transaction
            except _AlreadyPlanned as done:
                return done.decision_id
        else:
            proposal = self.model.generate(snapshot)  # no DB transaction across model work
        if (type(proposal) is not SyntheticProposal or type(proposal.body) is not str or not proposal.body.strip()
                or len(proposal.body.encode('utf-8')) > 4000 or proposal.sales_signal not in {'none', 'decline', 'reopen'}
                or type(proposal.item_scope) is not tuple or type(proposal.parts) is not tuple or len(proposal.parts) > 3
                or any(type(part) is not str or not part.strip() or len(part.encode('utf-8')) > 4000 for part in proposal.parts)
                or (proposal.parts and proposal.action != 'reply') or proposal.action not in {'reply', 'offer'}
                or (proposal.action == 'reply' and proposal.offer_grant_id is not None)
                or (proposal.action == 'offer' and (type(proposal.offer_grant_id) is not str or len(proposal.offer_grant_id) != 36))
                or any(type(x) is not str or not x.strip() for x in proposal.item_scope)
                or type(proposal.pitch) is not bool or type(proposal.purchase_claim) is not bool
                or type(proposal.price_claims) is not tuple
                or any(type(x) is not int or isinstance(x, bool) or x < 0 for x in proposal.price_claims)):
            raise Refused('invalid synthetic proposal')
        # JOURNEY-HOOK-2 (A7): the journey rules are enforced on the proposal, not just described to the model.
        from .journey import enforce_journey
        enforce_journey(snapshot.get('journey'), proposal)
        if proposal.sales_signal == 'reopen' and not proposal.item_scope:
            raise Refused('explicit buying scope required')
        if proposal.sales_signal != 'reopen' and proposal.item_scope:
            raise Refused('unexpected buying scope')
        if (proposal.sales_signal == 'reopen' and snapshot.get('pilot_scope') is not None
                and not set(proposal.item_scope) <= set((snapshot.get('journey') or {}).get('explicit_purchase_scope', []))):
            raise Refused('clear current buying scope required')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
            existing = _latest_generation(s, account, request_id)
            current_connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account, OfFansConnection.status == 'active'))
            current_epoch = current_connection.epoch if current_connection else None
            if existing:
                # Another regenerator won, or the generation we saw is no longer supersedable.
                if (existing[0].generation != observed_generation or request is None or request.status != 'open'
                        or not _proved_unsent(s, account, existing)):
                    return existing[0].decision_id
            elif observed_generation is not None:
                raise Refused('conversation changed during generation')
            if (not self._planning_policy_ok(policy)
                    or owner is None or owner.mode != 'active' or owner.epoch != epoch or runtime.binding_id != binding
                    or owner.binding_id != binding or current_epoch != snapshot['connection_epoch'] or state is None or state.mode != 'automated'
                    or state.takeover_state != 'none' or state.input_revision != snapshot['input_revision']
                    or state.revision != snapshot['control_revision'] or request is None or request.status != 'open'):
                raise Refused('conversation changed during generation')
            if (fan_id, source_message_id) in conflicted_sources(s, account):
                raise Refused('source message has conflicting webhook evidence')
            effective_sales_mode = ('paused' if proposal.sales_signal == 'decline'
                                    else 'reopened_scoped' if proposal.sales_signal == 'reopen' and owner.sales_mode != 'open'
                                    else owner.sales_mode)
            from .claims import check_plain_text, grounding_from_snapshot
            # Backstop with the same grounding the judge used: payload quantities and verified purchases are facts,
            # not invented claims. Fixture snapshots carry neither, so synthetic behaviour is unchanged (strict).
            check_plain_text([proposal.body, *proposal.parts], proposal, effective_sales_mode,
                {offer['priceCents'] for offer in snapshot['offer_reference']['offers']},
                grounding=grounding_from_snapshot(snapshot, proposal))
            if proposal.sales_signal == 'decline':
                owner.sales_mode, owner.sales_scope_json, owner.sales_source_message_id = 'paused', '[]', source_message_id
                owner.revision += 1; owner.updated_at = utcnow()
            elif proposal.sales_signal == 'reopen' and owner.sales_mode != 'open':
                # From paused or an earlier scoped reopening the newest explicit
                # scope replaces the old one. While selling is already open the
                # signal changes nothing (it must not narrow or raise).
                owner.sales_mode, owner.sales_scope_json, owner.sales_source_message_id = 'reopened_scoped', json.dumps(sorted(set(proposal.item_scope))), source_message_id
                owner.revision += 1; owner.updated_at = utcnow()
            decision_id = str(uuid.uuid4())
            terms = None
            grant = None
            status = 'ready'
            body = proposal.body
            if proposal.action == 'offer':
                from .commerce import verified_terms
                grant = s.get(OfEngineOfferGrant, (account, proposal.offer_grant_id))
                try:
                    terms = verified_terms(s, runtime, owner, grant)
                except Refused:
                    # Preserve the request and any refusal/reopening state; do not
                    # turn an unverified selection into a sendable decision.
                    status = 'held'
                    request.next_step, request.updated_at = 'review_offer_evidence', utcnow()
                    request.revision += 1
                if terms is not None:
                    # The drafted answer stays; the commercial section is rendered
                    # by the application from the frozen terms, never from model text.
                    body = offer_body(proposal.body, terms)
            payload_hash = TextAuthority._wire_hash(account, fan_id, body,
                terms['media_ids'] if terms else (), terms['preview_ids'] if terms else (), terms['price_cents'] if terms else 0)
            source = s.get(Message, (account, fan_id, source_message_id))
            if source is None or source.is_unsent or source.body != snapshot['current_message']['body']:
                raise Refused('source message changed during generation')
            from .memory import projection as memory_projection
            current_memory, current_fingerprint = memory_projection(s, runtime, fan_id, source)
            if current_fingerprint != memory_fingerprint:
                raise Refused('fan memory changed during generation')
            from .offers import match as match_offers
            from .catalogue import search_context
            from .memory import references_fingerprint
            if references_fingerprint(account, policy, match_offers(s, runtime, fan_id, source.body, include_cards=True),
                                      search_context(s, runtime, fan_id, source), s=s) != offer_fingerprint:
                raise Refused('catalogue, offer or creator profile changed during generation')
            generation = 0 if observed_generation is None else observed_generation + 1
            if observed_generation is not None:
                # Supersede proved-unsent work; keep it as append-only evidence and
                # make sure a held decision can no longer be retried.
                for old in existing:
                    old.status, old.updated_at = 'cancelled', utcnow()
                from .approval import invalidate_generation_in_session
                invalidate_generation_in_session(s, account, request_id, existing[0].generation, 'superseded_by_regeneration')
                request.next_step, request.updated_at = 'prepare_reply', utcnow()
                request.revision += 1
            decision = OfEngineDecision(account_id=account, decision_id=decision_id, request_id=request_id, generation=generation, part_index=0, fan_id=fan_id,
                binding_id=binding, owner_epoch=epoch, connection_epoch=connection_epoch, input_revision=state.input_revision, control_revision=state.revision,
                action=proposal.action, offer_grant_id=grant.grant_id if grant else None,
                catalog_item_id=terms['item']['id'] if terms else None,
                terms_json=json.dumps(terms, sort_keys=True, separators=(',', ':'), ensure_ascii=False) if terms else None,
                body=body, payload_hash=payload_hash, snapshot_hash=_digest(snapshot), status=status,
                created_at=utcnow(), updated_at=utcnow())
            s.add(decision)
            s.flush()
            from .memory import bind as bind_memory
            bind_memory(s, runtime, decision, source, current_memory, current_fingerprint, offer_fingerprint)
            from .independence import create_proof
            create_proof(s, runtime, decision, source, state, policy, extra_parts=bool(proposal.parts))
            for index, part in enumerate(proposal.parts, start=1):
                additional = OfEngineDecision(account_id=account, decision_id=str(uuid.uuid4()), request_id=request_id,
                    generation=generation, part_index=index, fan_id=fan_id, binding_id=binding, owner_epoch=epoch,
                    connection_epoch=connection_epoch, input_revision=state.input_revision,
                    control_revision=state.revision, action='reply', body=part,
                    payload_hash=TextAuthority._wire_hash(account, fan_id, part, (), (), 0),
                    snapshot_hash=_digest(snapshot), status='ready', created_at=utcnow(), updated_at=utcnow())
                s.add(additional); s.flush()
                bind_memory(s, runtime, additional, source, current_memory, current_fingerprint, offer_fingerprint)
            return decision_id

    def _model_recheck(self, tenant, account, fan_id, source_message_id, request_id, epoch, binding, snapshot,
                       observed_generation, memory_fingerprint, offer_fingerprint):
        """Re-validate the world a model answer depends on. Short read-only transaction; raises when anything moved."""
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            from .pilot_scope import planning_scope
            if planning_scope(s, runtime, fan_id, self.model, draft_only=self.draft_only) != snapshot.get('pilot_scope'):
                raise Refused('pilot scope changed during generation')
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            request = s.get(OfEngineRequest, (account, request_id))
            existing = _latest_generation(s, account, request_id)
            connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account, OfFansConnection.status == 'active'))
            if existing:
                if (existing[0].generation != observed_generation or request is None or request.status != 'open'
                        or not _proved_unsent(s, account, existing)):
                    raise _AlreadyPlanned(existing[0].decision_id)
            elif observed_generation is not None:
                raise Refused('conversation changed during generation')
            if (not self._planning_policy_ok(policy) or owner is None or owner.mode != 'active' or owner.epoch != epoch
                    or runtime.binding_id != binding or owner.binding_id != binding
                    or (connection.epoch if connection else None) != snapshot['connection_epoch'] or state is None
                    or state.mode != 'automated' or state.takeover_state != 'none'
                    or state.input_revision != snapshot['input_revision'] or state.revision != snapshot['control_revision']
                    or request is None or request.status != 'open'):
                raise Refused('conversation changed during generation')
            if (fan_id, source_message_id) in conflicted_sources(s, account):
                raise Refused('source message has conflicting webhook evidence')
            source = s.get(Message, (account, fan_id, source_message_id))
            if source is None or source.is_unsent or source.body != snapshot['current_message']['body']:
                raise Refused('source message changed during generation')
            from .memory import projection as memory_projection
            if memory_projection(s, runtime, fan_id, source)[1] != memory_fingerprint:
                raise Refused('fan memory changed during generation')
            from .offers import match as match_offers
            from .catalogue import search_context
            from .memory import references_fingerprint
            if references_fingerprint(account, policy, match_offers(s, runtime, fan_id, source.body, include_cards=True),
                                      search_context(s, runtime, fan_id, source), s=s) != offer_fingerprint:
                raise Refused('catalogue, offer or creator profile changed during generation')

    def _handover_model(self, tenant, account, request_id, target, reason='model_handover', detail=None):
        """The writer could not produce an acceptable reply: a human takes the request. No canned line is created.

        The handover is visible work: the request moves to `handover_pending` and an operator case records the reason,
        the call outcomes and the rejection reasons (never fan text), so a rejected or unusable draft is never lost.
        """
        from onlyfans_runtime.operator_cases import open_case
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
            if request is None or request.binding_id != runtime.binding_id or request.status != 'open':
                return
            request.status, request.handover_target = 'handover_pending', target
            request.next_step, request.updated_at = 'await_handover_acceptance', utcnow()
            request.revision += 1
            open_case(s, account, request.fan_id, 'model_handover', reason=str(reason)[:64],
                      evidence={'rule': str(reason)[:64], 'request_id': request_id, **(detail or {})},
                      source_message_id=request.source_message_id, origin='detector', scope='all')

    def queue(self, tenant, account, decision_id):
        with Session(self.engine) as s:
            decision = s.get(OfEngineDecision, (account, decision_id))
            if decision is None:
                raise Refused('engine decision unavailable')
            fan, body, revision = decision.fan_id, decision.body, decision.control_revision
            terms = json.loads(decision.terms_json) if decision.terms_json else None
            item_id = decision.catalog_item_id
            from .approval import is_gated
            gated, fresh = is_gated(s, account), decision.status == 'ready'
        if gated and fresh:
            # Supervised pilot: a new outgoing action needs a current, matching, approved version of its draft.
            # (An already queued/settled part only returns its existing intent below; nothing new is created.)
            from .approval import authorize_send
            authorize_send(self.store, tenant, account, decision_id)
        try:
            return TextAuthority(self.store).prepare(tenant, account, fan, command_key='engine:' + decision_id,
                expected_revision=revision, body=body, origin='automated', actor='offline-engine',
                engine_decision_id=decision_id, catalog_item_id=item_id,
                quoted_price_cents=terms['price_cents'] if terms else None)
        except Refused as exc:
            # Keep blocked work visible without altering or clearing the earlier
            # uncertain action. This is not an independence classification.
            with Session(self.engine) as s, s.begin():
                self.store._runtime(s, tenant, account)
                current = s.get(OfEngineDecision, (account, decision_id))
                state = s.get(OfConversationState, (account, fan))
                request = s.get(OfEngineRequest, (account, current.request_id)) if current else None
                if current is not None and request is not None and str(exc) in {'sequence conversation changed', 'source webhook conflict'}:
                    parts = s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                        OfEngineDecision.request_id == current.request_id, OfEngineDecision.generation == current.generation,
                        OfEngineDecision.part_index >= current.part_index,
                        OfEngineDecision.status == 'ready')).all()
                    for part in parts:
                        part.status, part.updated_at = 'cancelled', utcnow()
                    from .approval import invalidate_generation_in_session
                    invalidate_generation_in_session(s, account, current.request_id, current.generation, str(exc))
                    request.next_step = 'review_webhook_conflict' if str(exc) == 'source webhook conflict' else 'review_changed_conversation'
                    request.updated_at = utcnow()
                    request.revision += 1
                if (state is not None and request is not None and request.status == 'open'
                        and self.store._unresolved(s, account, fan, state)):
                    request.next_step, request.updated_at = 'review_delivery_dependency', utcnow()
                    request.revision += 1
            raise


    def fence_conflicted_sources(self, tenant, account):
        """Cancel unsent work derived from a source whose webhook later conflicted.

        Ready decisions are cancelled; a queued, never-dispatched intent is
        cancelled with its queue accounting. Anything dispatching, unknown or
        accepted is left alone: an ambiguous or finished send is never replayed.
        Final dispatch also refuses such a source (delivery.current).
        """
        fenced = []
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
            sources = conflicted_sources(s, account)
            if not sources:
                return fenced
            requests = s.scalars(select(OfEngineRequest).where(OfEngineRequest.account_id == account,
                OfEngineRequest.binding_id == runtime.binding_id,
                OfEngineRequest.status.not_in(('completed', 'declined', 'cancelled')))).all()
            for request in requests:
                if (request.fan_id, request.source_message_id) not in sources:
                    continue
                changed = False
                for item in _latest_generation(s, account, request.request_id):
                    if item.status not in {'ready', 'queued'}:
                        continue
                    if item.intent_id:
                        intent = s.get(OfTextIntent, (account, item.intent_id), with_for_update=True)
                        if intent is None or intent.status != 'queued':
                            continue  # dispatch fence decides; never touch an in-flight send
                        if policy is None or policy.queued_count < 1:
                            raise Refused('delivery queue accounting is inconsistent')
                        intent.status, intent.reason, intent.updated_at = 'cancelled', 'webhook_conflict', utcnow()
                        policy.queued_count -= 1
                    item.status, item.updated_at = 'cancelled', utcnow()
                    changed = True
                    from .approval import invalidate_generation_in_session
                    invalidate_generation_in_session(s, account, item.request_id, item.generation, 'webhook_conflict')
                if changed:
                    request.next_step, request.updated_at = 'review_webhook_conflict', utcnow()
                    request.revision += 1
                    fenced.append(request.request_id)
        return fenced

    def sequence(self, tenant, account, decision_id):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            decision = s.get(OfEngineDecision, (account, decision_id))
            if decision is None or decision.binding_id != runtime.binding_id:
                raise Refused('engine decision unavailable')
            return list(s.scalars(select(OfEngineDecision.decision_id).where(
                OfEngineDecision.account_id == account, OfEngineDecision.request_id == decision.request_id,
                OfEngineDecision.generation == decision.generation)
                .order_by(OfEngineDecision.part_index)))


    def retry_not_sent(self, tenant, account, decision_id, *, max_attempts):
        """Retry the same synthetic intent only after all attempts prove not sent.

        The supplied limit is a lab fixture, not an approved operational budget.
        Unknown or incomplete histories stay held; no new action/key is minted.
        """
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise Refused('invalid synthetic retry limit')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            decision = s.get(OfEngineDecision, (account, decision_id), with_for_update=True)
            if decision is None or decision.intent_id is None or decision.status != 'held':
                raise Refused('same-action retry is not eligible')
            intent = s.get(OfTextIntent, (account, decision.intent_id), with_for_update=True)
            owner = s.get(OfEngineOwner, (account, decision.fan_id))
            state = s.get(OfConversationState, (account, decision.fan_id))
            policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
            request = s.get(OfEngineRequest, (account, decision.request_id))
            attempts = list(s.scalars(select(OfEngineAttempt).where(OfEngineAttempt.account_id == account,
                OfEngineAttempt.intent_id == decision.intent_id).order_by(OfEngineAttempt.started_at)))
            connection = s.get(OfFansConnection, (account, decision.connection_epoch)) if decision.connection_epoch is not None else None
            connection_valid = decision.connection_epoch is None or (connection is not None and connection.status == 'active' and connection.binding_id == runtime.binding_id)
            from .memory import current as memory_current
            from .sequence import prior_state
            memory_valid = memory_current(s, runtime, decision)
            sequence_valid = prior_state(s, runtime, decision, state) is None
            if (not memory_valid or not sequence_valid or not connection_valid or intent is None or intent.status != 'not_accepted' or not intent.receipt_json
                    or owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                    or owner.epoch != decision.owner_epoch or state is None or state.mode != 'automated'
                    or state.takeover_state != 'none' or state.revision != decision.control_revision
                    or state.unresolved_attempt_id is not None
                    or policy is None or not policy.enabled or policy.transport_kind not in ENGINE_TRANSPORT_KINDS
                    or policy.revision != intent.policy_revision or intent.binding_id != runtime.binding_id
                    or intent.expires_at <= utcnow() or intent.payload_hash != TextAuthority(self.store)._intent_hash(intent)
                    or request is None or request.status != 'open' or not attempts or len(attempts) >= max_attempts
                    or any(a.outcome != 'not_accepted' or not a.evidence_json or a.payload_hash != intent.payload_hash for a in attempts)
                    or not any(a.attempt_id == intent.attempt_id and a.evidence_json == intent.receipt_json for a in attempts)):
                raise Refused('same-action retry lacks complete not-sent evidence')
            from onlyfans_runtime.text_delivery import MAX_QUEUED
            if policy.queued_count >= MAX_QUEUED:
                raise Refused('delivery queue full')
            intent.status, intent.reason, intent.updated_at = 'queued', 'retry_proven_not_sent', utcnow()
            intent.attempt_id = intent.dispatch_token = intent.dispatched_at = intent.deadline = None
            intent.wire_started_at = intent.receipt_json = intent.evidence_ref = None
            intent.reservation_held = False
            policy.queued_count += 1
            decision.status, decision.updated_at = 'queued', utcnow()
            request.next_step, request.updated_at = 'await_delivery', utcnow()
            request.revision += 1
            return intent.intent_id


    def request_handover(self, tenant, account, request_id, *, target):
        if type(target) is not str or not target.strip() or len(target) > 128:
            raise Refused('handover target is invalid')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
            if (request is None or request.binding_id != runtime.binding_id
                    or request.status in {'completed', 'declined', 'cancelled', 'handover_accepted'}):
                raise Refused('request cannot be handed over')
            if request.status == 'handover_pending':
                if request.handover_target != target:
                    raise Refused('handover target changed')
                return request.status
            request.status, request.handover_target = 'handover_pending', target
            request.next_step, request.updated_at = 'await_handover_acceptance', utcnow()
            request.revision += 1
            return request.status

    def accept_handover(self, tenant, account, request_id, *, actor):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
            if request is None or request.binding_id != runtime.binding_id:
                raise Refused('request unavailable')
            if request.status == 'handover_accepted' and request.accepted_by == actor:
                return request.status
            state = s.get(OfConversationState, (account, request.fan_id))
            from onlyfans_runtime.handover import staff_owns_takeover
            if (request.status != 'handover_pending' or request.handover_target != actor
                    or state is None or state.mode != 'human_controlled'
                    or state.takeover_state not in {'requested', 'effective'}
                    or not staff_owns_takeover(s, account, request.fan_id, state, actor)):
                raise Refused('handover acceptance is not authorized')
            request.status, request.owner, request.accepted_by = 'handover_accepted', actor, actor
            request.next_step, request.updated_at = 'human_review', utcnow()
            request.revision += 1
            return request.status
