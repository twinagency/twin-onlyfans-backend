"""Audited synthetic-only request ownership and resolution commands."""
from hashlib import sha256
import json
import re
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from db.models import OfConversationState
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import (OfEngineAccessEvidence, OfEngineDecision,
    OfEngineOperatorCommand, OfEngineRequest)
from onlyfans_read.agency_scope import ACTOR_PATTERN, actor_allowed
from onlyfans_runtime.handover import staff_owns_takeover
from onlyfans_runtime.store import Refused, utcnow


def execute(store, tenant, account, request_id, *, action, expected_revision, command_key, actor,
            target=None, evidence_ref=None, expected_fan_id=None):
    if (type(request_id) is not str or not re.fullmatch(r'[0-9a-f-]{36}', request_id)
            or action not in {'request_handover','accept_handover','complete','decline'}
            or type(expected_revision) is not int or not 0 <= expected_revision < 2**31-1
            or type(command_key) is not str or not re.fullmatch(r'[a-f0-9]{64}', command_key)
            or not actor_allowed(actor, capability='chat')):
        raise Refused('invalid operator command')
    if action == 'request_handover':
        if type(target) is not str or not re.fullmatch(ACTOR_PATTERN, target) or evidence_ref is not None:
            raise Refused('invalid handover target')
    elif target is not None:
        raise Refused('unexpected handover target')
    if action in {'complete','decline'}:
        if type(evidence_ref) is not str or not evidence_ref.startswith('synthetic:') or len(evidence_ref) > 128:
            raise Refused('synthetic resolution evidence required')
    elif evidence_ref is not None:
        raise Refused('unexpected resolution evidence')
    payload = {'requestId':request_id, 'action':action, 'expectedRevision':expected_revision,
               'commandKey':command_key, 'actor':actor, 'target':target, 'evidenceRef':evidence_ref}
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with Session(store.engine) as s, s.begin():
        runtime = store._runtime(s, tenant, account)
        policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
        if policy is None or policy.transport_kind != 'synthetic':
            raise Refused('operator workflow is limited to the synthetic lab')
        request = s.get(OfEngineRequest, (account, request_id), with_for_update=True)
        if (request is None or request.binding_id != runtime.binding_id
                or (expected_fan_id is not None and request.fan_id != expected_fan_id)):
            raise Refused('request scope is unavailable')
        prior = s.get(OfEngineOperatorCommand, (account, command_key))
        if prior is not None:
            if prior.request_hash != digest:
                raise Refused('operator command idempotency conflict')
            return {**json.loads(prior.result_json), 'replayed':True}
        if request.revision != expected_revision:
            raise Refused('request revision conflict')
        if action == 'request_handover':
            if request.status in {'completed','declined','cancelled','handover_pending','handover_accepted'}:
                raise Refused('request cannot be handed over')
            request.status, request.handover_target = 'handover_pending', target
            request.next_step = 'await_handover_acceptance'
        elif action == 'accept_handover':
            state = s.get(OfConversationState, (account, request.fan_id))
            if (request.status != 'handover_pending' or request.handover_target != actor
                    or state is None or state.mode != 'human_controlled'
                    or state.takeover_state not in {'requested','effective'}
                    or not staff_owns_takeover(s, account, request.fan_id, state, actor)):
                raise Refused('handover acceptance is not authorized')
            request.status, request.owner, request.accepted_by = 'handover_accepted', actor, actor
            request.next_step = 'human_review'
        else:
            if request.status != 'handover_accepted' or request.accepted_by != actor or request.owner != actor:
                raise Refused('accepted human responsibility is required')
            latest_generation = s.scalar(select(func.max(OfEngineDecision.generation)).where(
                OfEngineDecision.account_id == account, OfEngineDecision.request_id == request_id))
            decisions = list(s.scalars(select(OfEngineDecision).where(OfEngineDecision.account_id == account,
                OfEngineDecision.request_id == request_id, OfEngineDecision.generation == latest_generation)
                .order_by(OfEngineDecision.part_index)))
            intents = [s.get(OfTextIntent, (account, item.intent_id)) for item in decisions if item.intent_id]
            if any(row is None or row.status in {'dispatching','unknown'} or
                   (row.status == 'accepted' and row.projected_at is None) for row in intents):
                raise Refused('unresolved delivery still needs reconciliation')
            if action == 'complete':
                for item in decisions:
                    if item.action == 'offer':
                        if not item.intent_id:
                            raise Refused('offer access is not confirmed')
                        evidence = s.scalar(select(OfEngineAccessEvidence).where(
                            OfEngineAccessEvidence.account_id == account,
                            OfEngineAccessEvidence.intent_id == item.intent_id,
                            OfEngineAccessEvidence.applied.is_(True))
                            .order_by(OfEngineAccessEvidence.occurred_at.desc()).limit(1))
                        if evidence is None or evidence.status != 'confirmed':
                            raise Refused('offer access is not confirmed')
            for item in decisions:
                if item.status in {'ready','queued','held'}:
                    if item.intent_id:
                        intent = s.get(OfTextIntent, (account, item.intent_id))
                        if intent.status == 'queued':
                            if policy.queued_count < 1:
                                raise Refused('delivery queue accounting is inconsistent')
                            intent.status, intent.reason, intent.updated_at = 'cancelled', 'operator_resolution', utcnow()
                            policy.queued_count -= 1
                    item.status, item.updated_at = 'cancelled', utcnow()
            request.status = 'completed' if action == 'complete' else 'declined'
            request.next_step = 'none'
        request.revision += 1
        request.updated_at = utcnow()
        result = {'requestId':request.request_id, 'status':request.status, 'owner':request.owner,
                  'nextStep':request.next_step, 'recordedRevision':request.revision}
        s.add(OfEngineOperatorCommand(account_id=account, command_key=command_key, request_id=request_id,
            action=action, actor=actor, expected_revision=expected_revision, target=target,
            evidence_ref=evidence_ref, request_hash=digest,
            result_json=json.dumps(result, sort_keys=True, separators=(',', ':')), created_at=utcnow()))
        return {**result, 'replayed':False}
