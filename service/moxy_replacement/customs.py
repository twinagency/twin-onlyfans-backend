"""Custom-content lifecycle on the existing obligation structure (MoxyFan.state_json).

Sources (Response Playbook 14, Protocol A4, both creator sheets): a custom is never promised
before availability and authorisation are confirmed, only VIP / authorised fans are eligible,
the creator's minimum applies, customs are sold as content, payment stays on platform.
The sources do NOT define a deadline length, so none is invented: the deadline is whatever
the owning operator records, and a missing one is surfaced, not defaulted. Fulfilment is
performed and evidenced by the owning human; Moxy never delivers or charges a custom.

obligation = {id, kind:'custom', stage, status:'open'|'closed', owner, source_ref, minimum_cents,
              quote_cents, payment_route, deadline, fulfilment_ref, authorised_by}
stage order: requested -> confirmed -> quoted -> paid -> fulfilled   (or declined before paid)
"""
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from db.onlyfans_moxy_models import MoxyAccount, MoxyFan, MoxyAudit
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.native_context import canonical
from .configuration import ActionBlocked

ORDER = ('requested', 'confirmed', 'quoted', 'paid', 'fulfilled')
PAYMENT_ROUTE = 'on_platform'   # foundation `payments`: on-platform only


def _naive_utc(value):
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Refused('invalid_custom_deadline') from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.isoformat()


def new_request(event_id, rule_value, owner):
    return {'id': 'custom:' + event_id, 'kind': 'custom', 'stage': 'requested', 'status': 'open',
            'owner': owner, 'source_ref': 'moxy-event:' + event_id, 'source_event': event_id,
            'minimum_cents': rule_value['minimum_cents'], 'quote_cents': None, 'payment_route': None,
            'deadline': None, 'fulfilment_ref': None, 'authorised_by': None}


def record_request(s, event, decision, cfg, facts, assigned):
    """Called when the writer identified a custom request. Returns the obligation or None.

    Not recorded (conversation continues, nothing promised) when the creator rule is absent,
    its conditions are unknown/unmet for this fan, or no owner is configured.
    """
    if decision.get('permission') != 'customs' or not assigned:
        return None
    try:
        rule = cfg.resolve('customs', facts).value
    except ActionBlocked as exc:
        # Operator authorisation is checked at confirmation by the actual actor, not assumed here.
        if str(exc) != 'condition_unknown:assigned_operator_authorised' or 'customs' not in cfg.overrides:
            return None
        rule = cfg.overrides['customs']['value']
    if type(rule) is not dict or type(rule.get('minimum_cents')) is not int:
        return None
    mf = s.get(MoxyFan, (event.account_id, event.fan_id))
    state = json.loads(mf.state_json)
    obligations = state.setdefault('obligations', [])
    if any(o.get('kind') == 'custom' and o.get('status') == 'open' for o in obligations):
        return None
    item = new_request(event.event_id, rule, assigned)
    obligations.append(item)
    mf.state_json = canonical(state); mf.revision += 1; mf.updated_at = utcnow()
    from onlyfans_runtime.operator_cases import open_case
    open_case(s, event.account_id, event.fan_id, 'custom_request', reason='custom_requires_owner',
        evidence={'event_id': event.event_id, 'obligation': item['id']}, origin='detector', scope='offer', owner=assigned)
    return item


def advance(store, tenant, account, fan, obligation_id, *, stage, actor, evidence_ref,
            quote_cents=None, payment_route=None, deadline=None, fulfilment_ref=None):
    """Operator-evidenced transition. Every step needs an actor and a source reference."""
    if not actor or not evidence_ref or type(evidence_ref) is not str:
        raise Refused('verified_operator_state_required')
    if stage not in ORDER[1:] + ('declined',):
        raise Refused('invalid_custom_stage')
    with Session(store.engine) as s, s.begin():
        store._runtime(s, tenant, account)
        owner = s.get(MoxyAccount, account)
        if owner is None:
            raise Refused('moxy_owner_required')
        settings = json.loads(owner.settings_json)
        mf = s.get(MoxyFan, (account, fan))
        state = json.loads(mf.state_json) if mf else {}
        item = next((o for o in state.get('obligations', []) if o.get('id') == obligation_id and o.get('kind') == 'custom'), None)
        if item is None or item['status'] != 'open':
            raise Refused('open_custom_required')
        before = canonical(state)
        if stage == 'declined':
            if item['stage'] in ('paid', 'fulfilled'):
                raise Refused('paid_custom_cannot_be_declined')
            item['stage'], item['status'] = 'declined', 'closed'
        else:
            if ORDER.index(stage) != ORDER.index(item['stage']) + 1:
                raise Refused('custom_stage_out_of_order')
            if stage == 'confirmed':
                from .runtime import configuration
                cfg, _ = configuration(settings)
                facts = {**json.loads(mf.facts_json), 'assigned_operator_authorised': actor == settings['operational'].get('assigned_operator')}
                try:
                    rule = cfg.resolve('customs', facts).value
                except ActionBlocked as exc:
                    raise Refused('custom_not_authorised:' + str(exc)) from exc
                item['minimum_cents'] = rule['minimum_cents']
                item['authorised_by'] = actor
            elif stage == 'quoted':
                if type(quote_cents) is not int or quote_cents < item['minimum_cents']:
                    raise Refused('quote_below_creator_minimum')
                item['quote_cents'] = quote_cents
                if deadline is not None:
                    if type(deadline) is not str:
                        raise Refused('invalid_custom_deadline')
                    item['deadline'] = _naive_utc(deadline)
            elif stage == 'paid':
                if payment_route != PAYMENT_ROUTE:
                    raise Refused('on_platform_payment_required')
                item['payment_route'] = payment_route
            elif stage == 'fulfilled':
                if not fulfilment_ref or type(fulfilment_ref) is not str:
                    raise Refused('fulfilment_evidence_required')
                item['fulfilment_ref'], item['status'] = fulfilment_ref, 'closed'
            item['stage'] = stage
        mf.state_json = canonical(state); mf.revision += 1; mf.updated_at = utcnow()
        s.add(MoxyAudit(audit_id=str(uuid.uuid4()), account_id=account, actor=actor,
            before_json=canonical({'fan': fan, 'state': json.loads(before)}),
            after_json=canonical({'fan': fan, 'state': state, 'evidence_ref': evidence_ref}), created_at=utcnow()))
        return dict(item)


def open_customs(s, account, now=None):
    """Account-wide stall view: each open custom with the actual next blocker and overdue state."""
    now = now or utcnow()
    result = []
    for row in s.scalars(select(MoxyFan).where(MoxyFan.account_id == account,
            MoxyFan.state_json.like('%"kind":"custom"%')).order_by(MoxyFan.fan_id)).all():
        for o in json.loads(row.state_json).get('obligations', []):
            if o.get('kind') != 'custom' or o.get('status') != 'open':
                continue
            waiting = {'requested': 'owner_must_confirm_availability', 'confirmed': 'owner_must_quote',
                       'quoted': 'awaiting_fan_payment', 'paid': 'owner_must_fulfil'}[o['stage']]
            if o['stage'] in ('quoted', 'paid') and not o.get('deadline'):
                waiting += ';deadline_unset'
            overdue = bool(o.get('deadline')) and datetime.fromisoformat(o['deadline']) < now
            result.append({'fanId': row.fan_id, 'id': o['id'], 'stage': o['stage'], 'owner': o['owner'],
                'quoteCents': o.get('quote_cents'), 'deadline': o.get('deadline'), 'overdue': overdue, 'waitingOn': waiting})
    return result
