"""The real-path conversation worker: signed Fans API webhook -> plan -> queue -> dispatch with the real transport.

    FansEventIntake.accept  (raw evidence first, projection, payment/takeover evidence)
      -> quarantined?         EngineService.fence_conflicted_sources
      -> payment evidence?    JourneyService.after_evidence (whale hold / flag)
      -> inbound message?     drain -> select owner -> JourneyService.before_plan (handover / pause / flag / welcome grant)
                              -> EngineService.plan -> approval.create_draft_approval -> outcome 'awaiting_approval' (NO SEND)

    A named operator then approves an exact version (approval.approve) and calls `send_approved`, which for every part
    does: re-check -> authorize_send -> queue (approval checked again atomically) -> re-check -> authorize_send ->
    TextAuthority.dispatch (approval checked again at admission and just before the wire). Nothing in this class
    approves a draft, hands a conversation back or retries a refusal into a send.

Default OFF. Active operation requires TWIN_ENGINE_FANSAPI_REAL=1 and an enabled fansapi policy.
Draft-only operation has no provider transport and requires the shared delivery policy to be disabled. `SyntheticConversationWorker` is unchanged and still accepts only simulators.
No model or provider call is made here except through the supplied transport (and an opt-in backfill that reads).
"""
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_delivery_models import OfDeliveryPolicy, OfTextIntent
from db.onlyfans_engine_models import OfEngineDecision, OfEngineRequest
from db.onlyfans_models import OfIngress
from fans_api_adapter.backfill import FansBackfill
from fans_api_adapter.events import FansEventIntake
from fans_api_adapter.real_transport import FansApiRealTransport, real_transport_enabled
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import TextAuthority
from . import approval
from .draft_only import DraftOnlyTransport
from .delivery import unsent_cancelled
from .engine import EngineService
from .history_scope import current_binding_message
from .journey import JourneyService, backfill_complete, journey_config


def _identify(raw_body):
    """(event, fan_id, message_id) from an already authenticated body; None parts when absent."""
    try:
        body = json.loads(raw_body.decode('utf-8'))
        event, payload = body.get('event'), body['payload']
        if event == 'messages.received':
            return event, int(payload['fromUser']['id']), int(payload['id'])
        if event == 'messages.ppv.unlocked':
            return event, int(payload['chat_id']), None
        if event == 'transactions.new':
            fan = payload.get('fan') if isinstance(payload.get('fan'), dict) else payload.get('user')
            return event, int(fan['id']), None
        if event == 'chargebacks.new':
            return event, int(payload['payment']['user']['id']), None
        if event == 'subscriptions.new':
            return event, int(payload['fan']['id']), None
        return event, None, None
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return None, None, None


class RealConversationWorker:
    def __init__(self, engine, intake, transport, *, journey=None, backfill=None, sleep=time.sleep, max_pacing_wait=15.0):
        if (type(engine) is not EngineService or type(intake) is not FansEventIntake
                or type(transport) not in (FansApiRealTransport, DraftOnlyTransport)
                or engine.store is not intake.store or engine.store is not transport.store):
            raise Refused('invalid real worker dependencies')
        self.draft_only = type(transport) is DraftOnlyTransport
        if engine.draft_only != self.draft_only or self.draft_only and backfill is not None:
            raise Refused('draft-only worker scope mismatch')
        if backfill is not None and (type(backfill) is not FansBackfill or backfill.intake is not intake):
            raise Refused('invalid real worker backfill')
        self.engine, self.intake, self.transport = engine, intake, transport
        self.journey = journey or JourneyService(engine.store, engine)
        self.backfill = backfill
        self.sleep, self.max_pacing_wait = sleep, max_pacing_wait
        self.authority = TextAuthority(engine.store)

    # ------------------------------------------------------------------ gate
    def _gate(self, tenant, account):
        if not self.draft_only and not real_transport_enabled():
            raise Refused('real Fans API transport is not enabled')
        if tenant != self.transport.tenant_id or account != self.transport.account_id:
            raise Refused('real worker scope mismatch')
        with Session(self.engine.engine) as s:
            policy = s.get(OfDeliveryPolicy, account)
            if policy is None or policy.transport_kind != 'fansapi':
                raise Refused('real worker needs a fansapi account policy')
            if self.draft_only and policy.enabled is not False:
                raise Refused('draft-only requires the shared delivery policy to be disabled')
            if not self.draft_only and policy.enabled is not True:
                raise Refused('real worker needs an enabled fansapi account policy')

    # ------------------------------------------------------------------ webhook
    def handle(self, tenant, account, *, raw_body, signature, secret, event_key):
        self._gate(tenant, account)
        accepted = self.intake.accept(raw_body, signature, secret, event_key, expected_tenant=tenant, expected_account=account)
        outcome = accepted.get('outcome')
        if outcome == 'ignored':
            return {'intake': 'ignored', 'outcome': 'ignored', 'event': accepted.get('event')}
        if accepted.get('account') != account:
            raise Refused('webhook belongs to another account')  # evidence is kept; nothing is acted on
        if outcome == 'quarantined':
            # A conflicting duplicate makes unsent work from the first payload ineligible.
            fenced = self.engine.fence_conflicted_sources(tenant, account)
            return {'intake': outcome, 'outcome': 'quarantined', 'fenced': fenced}
        event, fan_id, message_id = _identify(raw_body)
        projection = accepted.get('projection')
        if projection != 'projected':
            return {'intake': outcome, 'outcome': 'projection_' + str(projection), 'event': event}
        if event != 'messages.received':
            result = {'intake': outcome, 'outcome': 'evidence', 'event': event}
            if (fan_id is not None and str(fan_id) in self.transport.allowed_chats
                    and event in {'messages.ppv.unlocked', 'transactions.new', 'chargebacks.new'}):
                result['journey'] = self.journey.after_evidence(tenant, account, fan_id)
            return result
        result = self.turn(tenant, account, fan_id, message_id)
        result['intake'] = outcome
        return result

    # ------------------------------------------------------------------ one fan turn
    def turn(self, tenant, account, fan_id, message_id):
        """Reply to one canonical inbound message. Safe to repeat: plan/queue/dispatch reuse durable records."""
        self._gate(tenant, account)
        if str(fan_id) not in self.transport.allowed_chats:
            return {'outcome': 'outside_pilot_scope'}
        store = self.engine.store
        store.drain(tenant, account)
        from .pilot_scope import current as current_pilot
        from .pilot_ingress import preserve_contact_stops, review_pending
        with Session(store.engine) as s:
            runtime = store._runtime(s, tenant, account, lock=False)
            pilot, _ = current_pilot(s, runtime)
        if pilot is not None:
            preserve_contact_stops(store, tenant, account)
            with Session(store.engine) as s:
                runtime = store._runtime(s, tenant, account, lock=False)
                pilot, config = current_pilot(s, runtime)
                if pilot is not None and review_pending(s, runtime, pilot, config, fan_id):
                    return {'outcome':'held', 'detail':'pilot_contact_review_pending'}
        approval.retire_superseded(store, tenant, account, fan_id, message_id)   # older unsent drafts lose their approval
        try:
            self.engine.select_simulated(tenant, account, fan_id, source_message_id=message_id)
        except Refused as exc:
            from .commerce import prepare_pilot_offer
            prepared = prepare_pilot_offer(store, tenant, account, fan_id, message_id, self.engine)
            if prepared is not None and prepared['state'] == 'held':
                return {'outcome': 'held', **prepared}
            return {'outcome': 'not_ready', 'detail': str(exc)}
        from .journey import scoped_config
        with Session(self.engine.engine) as s:
            config = scoped_config(s, account)
        if config.auto_backfill and self.backfill is not None:
            with Session(self.engine.engine) as s:
                waiting = not backfill_complete(s, account, fan_id)
            if waiting:
                self.backfill.run(tenant, account, self.transport.client, reason='operator',
                                  start_date=_parse_start(config.backfill_start), pages_budget=config.backfill_pages,
                                  history_chats=(fan_id,))
        verdict = self.journey.before_plan(tenant, account, fan_id, message_id)
        if verdict['action'] == 'handled':
            return {'outcome': 'handed_to_human', 'reason': verdict['reason']}
        from .commerce import prepare_pilot_offer
        prepared = prepare_pilot_offer(store, tenant, account, fan_id, message_id, self.engine)
        if prepared is not None and prepared['state'] == 'held':
            return {'outcome': 'held', **prepared}
        try:
            decision = self.engine.plan(tenant, account, fan_id, message_id)
            if approval.retire_dead_draft(store, tenant, account, decision):
                # Its approval was withdrawn (takeover, hand-back, expiry) and it never reached delivery: a hand-back
                # restores eligibility for a FRESH plan, never for replaying the old draft.
                decision = self.engine.plan(tenant, account, fan_id, message_id)
        except Refused as exc:
            return {'outcome': 'refused', 'detail': str(exc), 'journey': verdict}
        return self._send(tenant, account, decision)

    def resume(self, tenant, account, fan_id, message_id):
        """Plan again for the same inbound message (for example after its offer waited for a backfill)."""
        return self.turn(tenant, account, fan_id, message_id)

    _STAGED = {'awaiting': 'awaiting_approval', 'approved': 'approved_pending_send', 'held': 'held',
               'rejected': 'rejected', 'invalidated': 'approval_invalidated', 'expired': 'approval_expired',
               'consumed': 'already_sent'}

    def _send(self, tenant, account, decision):
        """Stage a planned draft for the operator. Never queues, dispatches or approves anything."""
        try:
            info = approval.create_draft_approval(self.engine.store, tenant, account, decision, created_by='engine')
        except Refused as exc:
            sequence = self.engine.sequence(tenant, account, decision)
            self._supersede(tenant, account, sequence, str(exc))
            return {'decisionId': decision, 'intentId': None, 'outcome': 'superseded:' + str(exc)}
        result = {'decisionId': decision, 'intentId': None, 'outcome': self._STAGED[info['status']]}
        if info['version']:
            result['approval'] = {key: info[key] for key in ('requestId', 'generation', 'version', 'status', 'expiresAt')}
        return result

    def _guard(self, tenant, account, part):
        """None when the part may still go out, else why not: nothing newer from the fan AND a current matching approval."""
        reason = self.recheck(tenant, account, part)
        if reason:
            return reason
        try:
            approval.authorize_send(self.engine.store, tenant, account, part)
        except Refused as exc:
            return str(exc)
        return None

    def _part_state(self, tenant, account, part):
        with Session(self.engine.engine) as s:
            decision = s.get(OfEngineDecision, (account, part))
            intent = s.get(OfTextIntent, (account, decision.intent_id)) if decision is not None and decision.intent_id else None
            return (decision.status if decision else None, intent.intent_id if intent else None,
                    intent.status if intent else None)

    def send_approved(self, tenant, account, decision_id):
        """Deliver an approved draft sequence, part by part. Safe to repeat after a crash or a duplicate click: queue
        reuses its command key and dispatch never resends a settled, unknown or in-flight part."""
        self._gate(tenant, account)
        if self.draft_only:
            return {'decisionId': decision_id, 'intentId': None, 'outcome': 'draft_only'}
        sequence = self.engine.sequence(tenant, account, decision_id)
        results = []
        for part in sequence:
            state, existing_intent, intent_status = self._part_state(tenant, account, part)
            if state == 'completed':
                results.append({'decisionId': part, 'intentId': existing_intent, 'outcome': 'accepted'})
                continue
            if state in {'cancelled', 'held', 'unknown'} or state is None:
                results.append({'decisionId': part, 'intentId': existing_intent, 'outcome': 'not_sent:' + str(state)})
                break
            if existing_intent is not None and intent_status != 'queued':
                # Already past admission (dispatching/unknown/accepted): report it, never create or resend anything.
                outcome = self.authority.dispatch(tenant, account, existing_intent, self.transport)
                results.append({'decisionId': part, 'intentId': existing_intent, 'outcome': outcome})
                if outcome != 'accepted':
                    break
                continue
            reason = self._guard(tenant, account, part)
            if reason:
                results.append(self._blocked(tenant, account, part, None, reason, sequence))
                break
            try:
                intent = self.engine.queue(tenant, account, part)
            except Refused as exc:
                results.append({'decisionId': part, 'intentId': None, 'outcome': 'not_queued:' + str(exc)})
                break
            reason = self._guard(tenant, account, part)  # immediately before submission
            if reason:
                results.append(self._blocked(tenant, account, part, intent, reason, sequence))
                break
            outcome = self._dispatch_paced(tenant, account, part, intent)
            if outcome.startswith('superseded:'):
                results.append({'decisionId': part, 'intentId': intent, 'outcome': outcome})
                self._supersede(tenant, account, sequence[sequence.index(part) + 1:], outcome.split(':', 1)[1])
                break
            results.append({'decisionId': part, 'intentId': intent, 'outcome': outcome})
            if outcome != 'accepted':
                break
        last = results[-1] if results else {'outcome': 'nothing_to_send', 'intentId': None}
        result = {'decisionId': decision_id, 'intentId': results[0]['intentId'] if results else None,
                  'outcome': last['outcome']}
        if len(sequence) > 1 or last['outcome'] != 'accepted':
            result['parts'] = results
        return result

    def _blocked(self, tenant, account, part, intent, reason, sequence):
        # A passive approval reason leaves an UNSTARTED draft for its operator. Once an earlier part has gone out the
        # approval can never be restored for the remainder (stop/correction/expiry after part one), so the unsent
        # rest is cancelled instead of being left 'ready' forever.
        started = sequence.index(part) > 0
        if (reason in approval.PASSIVE_REASONS or reason.startswith('approval_') and reason != 'approval_mismatch') and not started:
            if intent is not None:
                self._cancel_queued(tenant, account, intent, reason)
            return {'decisionId': part, 'intentId': intent, 'outcome': 'not_authorized:' + reason}
        if intent is not None:
            self._cancel_queued(tenant, account, intent, reason)
        self._supersede(tenant, account, sequence[sequence.index(part):], reason)
        return {'decisionId': part, 'intentId': intent, 'outcome': 'superseded:' + reason}

    def _dispatch_paced(self, tenant, account, part, intent):
        """Dispatch; when only the account's pacing gap holds the part back, wait for it (bounded) and re-check."""
        waited = 0.0
        while True:
            outcome = self.authority.dispatch(tenant, account, intent, self.transport)
            if outcome != 'queued':
                return outcome
            with Session(self.engine.engine) as s:
                row = s.get(OfTextIntent, (account, intent))
                policy = s.get(OfDeliveryPolicy, account)
                gap = None
                if row is not None and row.reason == 'account_throttled' and policy is not None and policy.next_dispatch_at:
                    gap = max(0.0, (policy.next_dispatch_at - utcnow()).total_seconds()) + 0.05
            if gap is None or waited + gap > self.max_pacing_wait:
                return outcome
            self.sleep(gap)
            waited += gap
            reason = self._guard(tenant, account, part)
            if reason:
                self._cancel_queued(tenant, account, intent, reason)
                return 'superseded:' + reason

    # ------------------------------------------------------------------ re-check before every part
    def recheck(self, tenant, account, decision_id):
        """None when nothing newer from the fan exists, else the reason. Run before queue and before dispatch.

        Pending (accepted but not yet applied) webhook messages count: they are drained first so the
        canonical state is current, then the latest inbound message must still be this request's source.
        """
        store = self.engine.store
        store.drain(tenant, account)
        with Session(self.engine.engine) as s:
            runtime = store._runtime(s, tenant, account)
            decision = s.get(OfEngineDecision, (account, decision_id))
            request = s.get(OfEngineRequest, (account, decision.request_id)) if decision else None
            if decision is None or request is None:
                return 'decision_unavailable'
            return approval.newer_input_reason(s, runtime, decision, request)

    def _supersede(self, tenant, account, decision_ids, reason):
        """Cancel parts that were never queued; a part that reached the wire is never touched."""
        if not decision_ids:
            return
        with Session(self.engine.engine) as s, s.begin():
            self.engine.store._runtime(s, tenant, account)
            request, changed = None, False
            for decision_id in decision_ids:
                decision = s.get(OfEngineDecision, (account, decision_id), with_for_update=True)
                if decision is None:
                    continue
                request = s.get(OfEngineRequest, (account, decision.request_id), with_for_update=True)
                if decision.status == 'ready' and decision.intent_id is None:
                    decision.status, decision.updated_at = 'cancelled', utcnow()
                    changed = True
                invalidated = approval.invalidate_generation_in_session(s, account, decision.request_id, decision.generation, reason)
                changed = bool(changed or invalidated)
            # A racing worker encountering an already retired generation must not
            # change request context underneath the worker generating its replacement.
            if changed and request is not None and request.status in {'open', 'waiting_fan'}:
                request.next_step, request.updated_at = 'review_changed_conversation', utcnow()
                request.revision += 1

    def _cancel_queued(self, tenant, account, intent_id, reason):
        """The part was queued but a newer fan message appeared before the wire: cancel it unsent."""
        with Session(self.engine.engine) as s, s.begin():
            self.engine.store._runtime(s, tenant, account)
            policy = s.get(OfDeliveryPolicy, account, with_for_update=True)
            intent = s.get(OfTextIntent, (account, intent_id), with_for_update=True)
            if intent is None or intent.status != 'queued':
                return False  # already admitted or settled: never recalled here
            if policy is None or policy.queued_count < 1:
                raise Refused('delivery queue accounting is inconsistent')
            intent.status, intent.reason, intent.updated_at = 'cancelled', reason, utcnow()
            policy.queued_count -= 1
            unsent_cancelled(s, intent)
            return True


def _parse_start(value):
    from datetime import datetime
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
