"""Exact old card and cheaper eligible offer through the normal correction path."""
import json
from datetime import timedelta

from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_moxy_models import MoxyEvent
from moxy_replacement.runtime import snapshot
from moxy_replacement.price_assertions import price_problem
from onlyfans_runtime.store import utcnow
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, correction, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


def test_false_cheapest_claim_gets_one_correction_without_inventing_a_discount(database, monkeypatch):
    engine, store, service, authority, _, settings = setup(database, monkeypatch)
    try:
        # Keep the fourth item outside the ordered sequence so it really is
        # eligible, rather than treating a blocked product as a cheaper option.
        service.configure(TENANT, ACCOUNT, expected_revision=1, mode='on', actor='fixture-operator',
                          settings=dict(settings, sequences={'fixture_level': ['first', 'second', 'third']},
                                        sequence_levels={'fixture_level': [['first', 'second', 'third']]}))
        with Session(engine) as session, session.begin():
            session.add(Message(account_id=ACCOUNT, fan_id=101, message_id=8001, direction='out',
                                body='Earlier outfit card.', media_ids=json.dumps([7101]), media_count=1,
                                price_cents=9000, is_paid=False, created_at=utcnow()-timedelta(minutes=1)))
        store.record(TENANT, ACCOUNT, event(mid=2, body='cheaper?')); store.drain(TENANT, ACCOUNT)
        identity = add(service, key='cheapest-card-question', mid=2)
        with Session(engine) as session:
            snap = snapshot(session, ACCOUNT, 101, session.get(MoxyEvent, (ACCOUNT, identity)))
        issue = price_problem(snap, decision(text='The one in the chat is already my cheapest.'))
        assert snap['offer_eligibility']['fourth'] == 'ok'
        assert issue == 'price_claim_mismatch'
        first = model(decision(text='The one in the chat is already my cheapest.'))
        service.prepare(TENANT, ACCOUNT, identity, first)
        assert row(engine, identity).reason == 'price_claim_mismatch'
        retry = correction(engine)
        brief = json.loads(row(engine, retry).payload_json)['rejected']
        assert 7500 in brief['verified_offerable_prices'].values()
        fixed = model(decision(text='Yes, there is another option for less.'))
        service.prepare(TENANT, ACCOUNT, retry, fixed)
        assert row(engine, retry).status == 'ready', row(engine, retry).reason
        result, wire, _ = send(service, authority, engine, retry, mid=9103)
        assert result == 'accepted' and len(wire.calls) == 1
        assert wire.calls[0].price_cents == 0 and not wire.calls[0].media_ids
        assert len(first.calls) == len(fixed.calls) == 1
    finally:
        store.close()
