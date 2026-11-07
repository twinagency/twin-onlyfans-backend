"""The real-path conversation worker: signed Fans API webhook -> plan -> queue -> dispatch with the real transport.

    FansEventIntake.accept  (raw evidence first, projection, payment/takeover evidence)
      -> quarantined?         EngineService.fence_conflicted_sources
      -> payment evidence?    JourneyService.after_evidence (whale hold / flag)
      -> inbound message?     drain -> select owner -> JourneyService.before_plan (handover / pause / flag / welcome grant)
                              -> EngineService.plan -> for every part: re-check -> queue -> re-check -> TextAuthority.dispatch

Default OFF. The worker refuses to run unless TWIN_ENGINE_FANSAPI_REAL=1 AND the account's delivery policy is
transport_kind 'fansapi' and enabled. `SyntheticConversationWorker` is unchanged and still accepts only simulators.
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
                or type(transport) is not FansApiRealTransport
                or engine.store is not intake.store or engine.store is not transport.store):
            raise Refused('invalid real worker dependencies')
        if backfill is not None and (type(backfill) is not FansBackfill or backfill.intake is not intake):
            raise Refused('invalid real worker backfill')
        self.engine, self.intake, self.transport = engine, intake, transport
        self.journey = journey or JourneyService(engine.store, engine)
        self.backfill = backfill
        self.sleep, self.max_pacing_wait = sleep, max_pacing_wait
        self.authority = TextAuthority(engine.store)

    # ------------------------------------------------------------------ gate
    def _gate(self, tenant, account):
        if not real_transport_enabled():
            raise Refused('real Fans API transport is not enabled')
        if tenant != self.transport.tenant_id or account != self.transport.account_id:
            raise Refused('real worker scope mismatch')
        with Session(self.engine.engine) as s:
            policy = s.get(OfDeliveryPolicy, account)
            if policy is None or policy.transport_kind != 'fansapi' or not policy.enabled:
                raise Refused('real worker needs an enabled fansapi account policy')

    # ------------------------------------------------------------------ webhook
    def handle(self, tenant, account, *, raw_body, signature, secret, event_key):
        self._gate(tenant, account)
        accepted = self.intake.accept(raw_body, signature, secret, event_key)
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
            if fan_id is not None and event in {'messages.ppv.unlocked', 'transactions.new', 'chargebacks.new'}:
                result['journey'] = self.journey.after_evidence(tenant, account, fan_id)
            return result
        result = self.turn(tenant, account, fan_id, message_id)
        result['intake'] = outcome
        return result

    # ------------------------------------------------------------------ one fan turn
    def turn(self, tenant, account, fan_id, message_id):
        """Reply to one canonical inbound message. Safe to repeat: plan/queue/dispatch reuse durable records."""
        self._gate(tenant, account)
        store = self.engine.store
        store.drain(tenant, account)
        try:
            self.engine.select_simulated(tenant, account, fan_id)  # 'simulated' by name only: any engine kind
        except Refused as exc:
            return {'outcome': 'not_ready', 'detail': str(exc)}
        config = journey_config()
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
        try:
            decision = self.engine.plan(tenant, account, fan_id, message_id)
        except Refused as exc:
            return {'outcome': 'refused', 'detail': str(exc), 'journey': verdict}
        return self._send(tenant, account, decision)

    def resume(self, tenant, account, fan_id, message_id):
        """Plan again for the same inbound message (for example after its offer waited for a backfill)."""
        return self.turn(tenant, account, fan_id, message_id)

    def _send(self, tenant, account, decision):
        sequence = self.engine.sequence(tenant, account, decision)
        results = []
        for part in sequence:
            reason = self.recheck(tenant, account, part)
            if reason:
                results.append({'decisionId': part, 'intentId': None, 'outcome': 'superseded:' + reason})
                self._supersede(tenant, account, sequence[sequence.index(part):], reason)
                break
            try:
                intent = self.engine.queue(tenant, account, part)
            except Refused as exc:
                results.append({'decisionId': part, 'intentId': None, 'outcome': 'not_queued:' + str(exc)})
                break
            reason = self.recheck(tenant, account, part)  # immediately before submission
            if reason:
                self._cancel_queued(tenant, account, intent, reason)
                results.append({'decisionId': part, 'intentId': intent, 'outcome': 'superseded:' + reason})
                self._supersede(tenant, account, sequence[sequence.index(part) + 1:], reason)
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
        result = {'decisionId': decision, 'intentId': results[0]['intentId'] if results else None,
                  'outcome': last['outcome']}
        if len(sequence) > 1 or last['outcome'] != 'accepted':
            result['parts'] = results
        return result

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
            reason = self.recheck(tenant, account, part)
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
            pending = s.scalar(select(OfIngress.event_key).where(OfIngress.account_id == account,
                OfIngress.fan_id == decision.fan_id, OfIngress.status == 'pending').limit(1))
            if pending is not None:
                return 'new_inbound_pending'
            latest = s.scalar(select(Message.message_id).where(Message.account_id == account,
                Message.fan_id == decision.fan_id, Message.direction == 'in', Message.is_unsent.is_(False),
                current_binding_message(runtime.binding_id))
                .order_by(Message.created_at.desc(), Message.message_id.desc()).limit(1))
            if latest != request.source_message_id:
                return 'newer_inbound'
        return None

    def _supersede(self, tenant, account, decision_ids, reason):
        """Cancel parts that were never queued; a part that reached the wire is never touched."""
        if not decision_ids:
            return
        with Session(self.engine.engine) as s, s.begin():
            self.engine.store._runtime(s, tenant, account)
            request = None
            for decision_id in decision_ids:
                decision = s.get(OfEngineDecision, (account, decision_id), with_for_update=True)
                if decision is None:
                    continue
                request = s.get(OfEngineRequest, (account, decision.request_id), with_for_update=True)
                if decision.status == 'ready' and decision.intent_id is None:
                    decision.status, decision.updated_at = 'cancelled', utcnow()
            if request is not None and request.status in {'open', 'waiting_fan'}:
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
