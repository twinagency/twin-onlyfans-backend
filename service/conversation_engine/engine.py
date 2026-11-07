"""Offline-only engine orchestration around canonical inbox and delivery records.

Only in-process fixture models are accepted. This is deliberately
not a production writer: provider/model approval and creator operating rules
are still required before a real model can be admitted.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Message, OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineAttempt, OfEngineDecision, OfEngineOwner, OfEngineRequest, OfEngineOfferGrant, OfFansConnection
from db.onlyfans_models import OfIngress
from db.onlyfans_moxy_models import MoxyAccount
from onlyfans_runtime.store import Refused, _id, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from .history_scope import current_binding_message, has_unscoped_messages


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class SyntheticProposal:
    body: str
    sales_signal: str = 'none'
    item_scope: tuple[str, ...] = ()
    action: str = 'reply'
    offer_grant_id: str | None = None
    parts: tuple[str, ...] = ()


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
    def __init__(self, store, model):
        from .model_contract import StructuredFixtureModel
        if type(model) not in (SyntheticModel, StructuredFixtureModel):
            raise Refused('only offline fixture models are available')
        self.store = store
        self.engine = store.engine
        self.model = model

    def select_simulated(self, tenant, account, fan_id):
        """Select a conversation only while the existing authority is synthetic."""
        _id(fan_id)
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            policy = s.get(OfDeliveryPolicy, account)
            state = s.get(OfConversationState, (account, fan_id))
            if (policy is None or policy.transport_kind != 'synthetic' or not policy.enabled
                    or state is None or state.mode != 'automated' or state.takeover_state != 'none'
                    or self.store._unresolved(s, account, fan_id, state)):
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
                    policy is None or policy.transport_kind != 'synthetic' or ingress is None or
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
                                or policy is None or policy.transport_kind != 'synthetic'):
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
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            if (owner is None or owner.mode != 'active' or owner.binding_id != runtime.binding_id
                    or policy is None or policy.transport_kind != 'synthetic'
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
            prior = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                                                             OfEngineDecision.request_id == request.request_id).order_by(OfEngineDecision.part_index).limit(1))
            if prior is not None:
                return prior.decision_id
            latest = s.scalar(select(Message).where(Message.account_id == account, Message.fan_id == fan_id,
                Message.direction == 'in', Message.is_unsent.is_(False), current_binding_message(runtime.binding_id)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))
            stale = latest is None or latest.message_id != source_message_id
            if stale:
                request.next_step, request.updated_at = 'review_newer_input', utcnow()
                request.revision += 1
            else:
                history = list(s.scalars(select(Message).where(Message.account_id == account, Message.fan_id == fan_id,
                    current_binding_message(runtime.binding_id)).order_by(Message.created_at.desc(), Message.message_id.desc()).limit(101)))
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
                offer_match = match_offers(s, runtime, fan_id, message.body)
                from .catalogue import search_context
                catalogue_view = search_context(s, runtime, fan_id, message)
                offer_fingerprint = _digest({'offers':offer_match, 'catalogue':catalogue_view})
                snapshot = {'account_id': account, 'fan_id': fan_id, 'binding_id': runtime.binding_id,
                    'owner_epoch': owner.epoch, 'connection_epoch': connection_epoch, 'input_revision': state.input_revision, 'control_revision': state.revision,
                    'sales_mode': owner.sales_mode, 'sales_scope': json.loads(owner.sales_scope_json),
                    'current_message': {'id': source_message_id, 'body': message.body},
                    'history': [{'id': row.message_id, 'direction': row.direction, 'body': row.body} for row in reversed(history[:100])],
                    'history_truncated': len(history) > 100,
                    'history_scope_incomplete': has_unscoped_messages(s, account, fan_id, runtime.binding_id),
                    'memory': memory_view,
                    'offer_reference': offer_match,
                    'catalogue_search': catalogue_view,
                    'pending_actions': [{'decision_id': d.decision_id, 'request_id': d.request_id,
                        'action': d.action, 'outcome': intent_status or d.status, 'body': d.body,
                        'terms': json.loads(d.terms_json) if d.terms_json else None}
                        for d, intent_status in pending_rows[:50]],
                    'pending_actions_truncated': len(pending_rows) > 50,
                    'open_requests': [{'request_id': r.request_id, 'source_message_id': r.source_message_id,
                        'status': r.status, 'owner': r.owner, 'next_step': r.next_step} for r in open_rows[:50]],
                    'open_requests_truncated': len(open_rows) > 50}
                request_id, epoch, binding = request.request_id, owner.epoch, runtime.binding_id
        if stale:
            raise Refused('newer incoming message needs review')
        from .model_contract import StructuredFixtureModel
        if type(self.model) not in (SyntheticModel, StructuredFixtureModel):
            raise Refused('only offline fixture models are available')
        proposal = self.model.generate(snapshot)  # no DB transaction across model work
        if (type(proposal) is not SyntheticProposal or type(proposal.body) is not str or not proposal.body.strip()
                or len(proposal.body.encode('utf-8')) > 4000 or proposal.sales_signal not in {'none', 'decline', 'reopen'}
                or type(proposal.item_scope) is not tuple or type(proposal.parts) is not tuple or len(proposal.parts) > 3
                or any(type(part) is not str or not part.strip() or len(part.encode('utf-8')) > 4000 for part in proposal.parts)
                or (proposal.parts and proposal.action != 'reply') or proposal.action not in {'reply', 'offer'}
                or (proposal.action == 'reply' and proposal.offer_grant_id is not None)
                or (proposal.action == 'offer' and (type(proposal.offer_grant_id) is not str or len(proposal.offer_grant_id) != 36))
                or any(type(x) is not str or not x.strip() for x in proposal.item_scope)):
            raise Refused('invalid synthetic proposal')
        if proposal.sales_signal == 'reopen' and not proposal.item_scope:
            raise Refused('explicit buying scope required')
        if proposal.sales_signal != 'reopen' and proposal.item_scope:
            raise Refused('unexpected buying scope')
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            owner = s.get(OfEngineOwner, (account, fan_id))
            state = s.get(OfConversationState, (account, fan_id))
            policy = s.get(OfDeliveryPolicy, account)
            request = s.get(OfEngineRequest, (account, request_id))
            prior = s.scalar(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                                                             OfEngineDecision.request_id == request_id).order_by(OfEngineDecision.part_index).limit(1))
            current_connection = s.scalar(select(OfFansConnection).where(OfFansConnection.account_id == account, OfFansConnection.status == 'active'))
            current_epoch = current_connection.epoch if current_connection else None
            if prior is not None:
                return prior.decision_id
            if (policy is None or policy.transport_kind != 'synthetic' or not policy.enabled
                    or owner is None or owner.mode != 'active' or owner.epoch != epoch or runtime.binding_id != binding
                    or owner.binding_id != binding or current_epoch != snapshot['connection_epoch'] or state is None or state.mode != 'automated'
                    or state.takeover_state != 'none' or state.input_revision != snapshot['input_revision']
                    or state.revision != snapshot['control_revision'] or request is None or request.status != 'open'):
                raise Refused('conversation changed during generation')
            if proposal.sales_signal == 'decline':
                owner.sales_mode, owner.sales_scope_json, owner.sales_source_message_id = 'paused', '[]', source_message_id
                owner.revision += 1; owner.updated_at = utcnow()
            elif proposal.sales_signal == 'reopen':
                if owner.sales_mode != 'paused':
                    raise Refused('sales are not paused')
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
                    label = terms['item'].get('label') or terms['item']['description_for_ai']
                    if type(label) is not str or not label.strip() or len(label.encode()) > 2000:
                        raise Refused('approved item label is unavailable')
                    cents = terms['price_cents']
                    body = f"{label.strip()} — USD {cents // 100}.{cents % 100:02d}."
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
            if _digest({'offers':match_offers(s, runtime, fan_id, source.body),
                        'catalogue':search_context(s, runtime, fan_id, source)}) != offer_fingerprint:
                raise Refused('catalogue or offer references changed during generation')
            decision = OfEngineDecision(account_id=account, decision_id=decision_id, request_id=request_id, part_index=0, fan_id=fan_id,
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
            create_proof(s, runtime, decision, source, state, policy)
            for index, part in enumerate(proposal.parts, start=1):
                additional = OfEngineDecision(account_id=account, decision_id=str(uuid.uuid4()), request_id=request_id,
                    part_index=index, fan_id=fan_id, binding_id=binding, owner_epoch=epoch,
                    connection_epoch=connection_epoch, input_revision=state.input_revision,
                    control_revision=state.revision, action='reply', body=part,
                    payload_hash=TextAuthority._wire_hash(account, fan_id, part, (), (), 0),
                    snapshot_hash=_digest(snapshot), status='ready', created_at=utcnow(), updated_at=utcnow())
                s.add(additional); s.flush()
                bind_memory(s, runtime, additional, source, current_memory, current_fingerprint, offer_fingerprint)
            return decision_id

    def queue(self, tenant, account, decision_id):
        with Session(self.engine) as s:
            decision = s.get(OfEngineDecision, (account, decision_id))
            if decision is None:
                raise Refused('engine decision unavailable')
            fan, body, revision = decision.fan_id, decision.body, decision.control_revision
            terms = json.loads(decision.terms_json) if decision.terms_json else None
            item_id = decision.catalog_item_id
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
                if current is not None and request is not None and str(exc) == 'sequence conversation changed':
                    parts = s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                        OfEngineDecision.request_id == current.request_id, OfEngineDecision.part_index >= current.part_index,
                        OfEngineDecision.status == 'ready')).all()
                    for part in parts:
                        part.status, part.updated_at = 'cancelled', utcnow()
                    request.next_step, request.updated_at = 'review_changed_conversation', utcnow()
                    request.revision += 1
                if (state is not None and request is not None and request.status == 'open'
                        and self.store._unresolved(s, account, fan, state)):
                    request.next_step, request.updated_at = 'review_delivery_dependency', utcnow()
                    request.revision += 1
            raise


    def sequence(self, tenant, account, decision_id):
        with Session(self.engine) as s, s.begin():
            runtime = self.store._runtime(s, tenant, account)
            decision = s.get(OfEngineDecision, (account, decision_id))
            if decision is None or decision.binding_id != runtime.binding_id:
                raise Refused('engine decision unavailable')
            return list(s.scalars(select(OfEngineDecision.decision_id).where(
                OfEngineDecision.account_id == account, OfEngineDecision.request_id == decision.request_id)
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
                    or policy is None or not policy.enabled or policy.transport_kind != 'synthetic'
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
