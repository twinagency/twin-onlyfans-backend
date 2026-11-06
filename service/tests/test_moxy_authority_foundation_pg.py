"""Actual Moxy lifecycle qualification for scoped authority and money wording."""
from copy import deepcopy
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyAccount, MoxyEvent, MoxyFan
from moxy_replacement.configuration import load
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, correction, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


def test_delivered_requested_item_does_not_clear_saved_global_sales_preferences(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    with Session(engine) as session, session.begin():
        fan = session.get(MoxyFan, (ACCOUNT, 101))
        saved = json.loads(fan.state_json)
        saved.update(ordinary_chat_only=True, sales_refused=True)
        fan.state_json = json.dumps(saved)
    text = 'Please send me the first photo.'
    store.record(TENANT, ACCOUNT, event(mid=2, body=text)); store.drain(TENANT, ACCOUNT)
    identity = add(service, key='one-item-exception', mid=2)
    service.prepare(TENANT, ACCOUNT, identity, model(decision(
        'offer', 'Here is the first photo.', 'first', sequence='fixture_level',
        request_kind='direct', request_quote=text)))
    assert row(engine, identity).status == 'ready'
    outcome, transport, _ = send(service, authority, engine, identity)
    assert outcome == 'accepted'
    assert transport.calls[0].media_ids == (7101,)
    assert transport.calls[0].price_cents == 7500
    with Session(engine) as session:
        saved = json.loads(session.get(MoxyFan, (ACCOUNT, 101)).state_json)
        assert saved['ordinary_chat_only'] is True
        assert saved['sales_refused'] is True
    store.close()


def test_price_wording_correction_preserves_exact_valid_offer_and_sends_once(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    identity = add(service, key='wrong-caption-price')
    first = model(decision('offer', 'This photo is $5.', 'first', sequence='fixture_level'))
    service.prepare(TENANT, ACCOUNT, identity, first)
    assert row(engine, identity).reason == 'price_claim_mismatch'
    retry = correction(engine)
    retained = json.loads(row(engine, retry).payload_json)['rejected']
    assert retained['kind'] == 'factual'
    assert retained['keep']['product_id'] == 'first'
    assert retained['keep_terms']['price_cents'] == 7500
    second = model(decision('offer', 'This photo is $75.', 'first', sequence='fixture_level'))
    service.prepare(TENANT, ACCOUNT, retry, second)
    assert row(engine, retry).status == 'ready'
    outcome, transport, _ = send(service, authority, engine, retry)
    assert outcome == 'accepted' and len(transport.calls) == 1
    assert transport.calls[0].price_cents == 7500
    assert len(first.calls) == len(second.calls) == 1
    with Session(engine) as session:
        assert len(session.scalars(select(OfTextIntent)).all()) == 1
        assert len([e for e in session.scalars(select(MoxyEvent))
                    if json.loads(e.payload_json).get('correction_of')]) == 1
    store.close()


def test_unreviewed_runtime_only_rule_fails_before_model_reservation(database, monkeypatch):
    engine, store, service, _, _, settings = setup(database, monkeypatch)
    settings = deepcopy(settings)
    document = deepcopy(load('amelia').creator)
    next(rule for rule in document['rules'] if rule['key'] == 'talker_timing')['instruction'] = (
        'Unreviewed additional timing condition in this synthetic fixture.')
    settings['creator_document'] = document
    service.configure(TENANT, ACCOUNT, expected_revision=1, mode='on', settings=settings,
                      actor='fixture-operator')
    identity = add(service, key='unreviewed-timing-rule')
    candidate = model(decision())
    service.prepare(TENANT, ACCOUNT, identity, candidate)
    result = row(engine, identity)
    assert result.status == 'held'
    assert result.reason == 'writer_rule_review_required:talker_timing'
    assert result.model_call_id is None and result.reserved_millicents is None
    assert candidate.calls == []
    with Session(engine) as session:
        assert not session.scalar(select(OfTextIntent.intent_id))
    store.close()
