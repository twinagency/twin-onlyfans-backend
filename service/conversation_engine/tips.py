"""Step 5.2: voluntary tips for a COMMERCIAL account, through the existing owners.

* intake: `fans_api_adapter.events` projects a validated `tips.received` into the existing payment-evidence owner
  (`of_fans_evidence`, kind 'tip') on the stable provider tip identity, so a changed event key, a redelivery or a replay never creates
  a second fact. Evidence that does not establish a tip (missing or inconsistent amount, another creator, not a tip, a fan outside the
  audience) is never accounted; it stays a visible, held digest that blocks only that fan.
* reply: an established, unacknowledged tip is shown to the writer as a fact of the NEXT real reply the engine plans for that fan
  (the existing request -> decision -> approval -> delivery path). A tip never creates a request, a fan message, a draft or a send by
  itself, and there is no tip-sending worker. Acknowledgement is recorded when that decision's send is accepted.
* limits: a tip is not a purchase, access, a grant, a discount or a promise. The writer may thank; it may not ask for tips or promise
  anything for one (`model_output.tip_problems`). A tip older than ACK_WINDOW is no longer thanked: no stale backlog is revived.

Money evidence is retained exactly as received; the amount is the provider's gross amount in whole cents (never net, never guessed).
"""
from collections import namedtuple
from datetime import timedelta
import re

from sqlalchemy import select

from fans_api_adapter import payment_evidence as ev
from onlyfans_runtime.store import Refused, utcnow

KIND = 'tip'
ACK_WINDOW = timedelta(hours=24)
MAX_SHOWN = 3

Tip = namedtuple('Tip', 'tip_id fan_id amount_cents net_cents occurred')


def validate(payload, *, creator_id):
    """The tip a `tips.received` payload establishes for THIS creator, or Refused. Nothing is inferred or defaulted."""
    if type(payload) is not dict or type(creator_id) is not str:
        raise Refused('tip payload is not an object')
    tip_id = ev.transaction_id(payload.get('id'))
    if payload.get('subType') != 'tip':
        raise Refused('event is not a tip')
    if str(payload.get('user_id')) != creator_id or payload.get('user_id') is None:
        raise Refused('tip belongs to another creator')
    user = payload.get('user')
    if user is not None and (type(user) is not dict or str(user.get('id')) != creator_id):
        raise Refused('tip creator identity conflicts')
    fan = payload.get('fan')
    if type(fan) is not dict:
        raise Refused('tip fan is missing')
    fan_id = ev.numeric(fan.get('id'))
    if str(fan_id) == creator_id:
        raise Refused('tip names the creator as the fan')
    if payload.get('currency') not in (None, 'USD'):
        raise Refused('unsupported currency')
    gross = ev.cents(payload.get('amountGross'))
    net = None
    if payload.get('amountNet') is not None:
        net = ev.cents(payload['amountNet'])
        if net > gross:
            raise Refused('tip net amount exceeds the gross amount')
    return Tip(tip_id, fan_id, gross, net, ev.stamp(payload.get('createdAt')))


def commercial(s, runtime):
    from .scope_contract import is_commercial
    from .pilot_scope import current
    try:
        row, config = current(s, runtime)
    except Refused:
        return False
    return row is not None and row.scope_id != 1 and is_commercial(config)


def audience(s, runtime):
    from .pilot_scope import current
    try:
        _, config = current(s, runtime)
    except Refused:
        return frozenset()
    return frozenset(config['fans']) if config else frozenset()


def _available(s, account, fan_id, request_id, now, *, lock=False):
    """Established tips that still need their one acknowledgement: unpaired, or paired to a draft that is provably unsent."""
    from db.onlyfans_engine_models import OfEngineDecision
    from db.onlyfans_fans_intake_models import OfFansEvidence
    query = select(OfFansEvidence).where(OfFansEvidence.account_id == account, OfFansEvidence.fan_id == fan_id,
        OfFansEvidence.kind == KIND, OfFansEvidence.status.in_(('pending', 'paired')),
        OfFansEvidence.occurred_at >= now - ACK_WINDOW).order_by(OfFansEvidence.occurred_at, OfFansEvidence.evidence_id)
    rows = s.scalars(query.with_for_update() if lock else query).all()
    out = []
    for row in rows:
        if row.status == 'paired':
            paired = s.get(OfEngineDecision, (account, row.peer_id)) if row.peer_id else None
            # Only a draft that never reached delivery gives the tip back: cancelled, or still an unqueued 'ready' draft of the
            # request being re-planned (the regeneration cancels it in the same transaction). Anything that may have been sent
            # keeps the tip acknowledged-or-unknown, never thanked twice.
            if paired is None or not (paired.intent_id is None and (
                    paired.status == 'cancelled' or paired.status == 'ready' and paired.request_id == request_id)):
                continue
        out.append(row)
    return out[:MAX_SHOWN]


def snapshot_facts(s, runtime, fan_id, request_id):
    """The writer's tip facts for this plan: [] unless a commercial account has an established, unacknowledged tip of this fan.
    Facts are (provider tip id, whole cents); the amount is for the application's checks, the writer is told not to state it."""
    if not commercial(s, runtime):
        return []
    rows = _available(s, runtime.account_id, fan_id, request_id, utcnow())
    return [{'tip_id': row.provider_transaction_id, 'amount_cents': row.amount_cents} for row in rows]


def pair(s, runtime, fan_id, request_id, parts):
    """The plan carries the thanks: pair every available tip with the first part that really mentions a tip.
    `parts` is [(decision_id, text)]. A draft that ignores the tip leaves it unpaired for the next reply."""
    if not commercial(s, runtime):
        return 0
    thanks = next((decision_id for decision_id, text in parts if THANKS.search(text or '')), None)
    if thanks is None:
        return 0
    count = 0
    for row in _available(s, runtime.account_id, fan_id, request_id, utcnow(), lock=True):
        row.status, row.peer_id, row.reason, row.updated_at = 'paired', thanks, 'acknowledgement_drafted', utcnow()
        count += 1
    return count


def finalize(s, decision):
    """The decision that carried the thanks was accepted by the provider and projected: the tip is acknowledged, once."""
    from db.onlyfans_fans_intake_models import OfFansEvidence
    count = 0
    for row in s.scalars(select(OfFansEvidence).where(OfFansEvidence.account_id == decision.account_id,
            OfFansEvidence.kind == KIND, OfFansEvidence.status == 'paired', OfFansEvidence.peer_id == decision.decision_id)
            .with_for_update()):
        row.status, row.intent_id, row.reason, row.updated_at = 'applied', decision.intent_id, 'acknowledged', utcnow()
        count += 1
    return count


# A draft "mentions a tip" when it names it: this is what pairs the thanks with the evidence, not a claim made on the model's behalf.
THANKS = re.compile(r"\b(?:tip|tips|tipped|tipping)\b", re.I)
