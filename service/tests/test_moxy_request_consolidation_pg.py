"""Request/correction regressions through Moxy and synthetic delivery only.

The database fixture requires a disposable PostgreSQL server. Model responses
are scripted: these tests establish custody and authority, not model quality.
"""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ContentOffer, Message
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyEvent, MoxyFan
from onlyfans_runtime.native_planning import SyntheticNativeModel
from test_moxy_integrated_pg import decision, model, setup
from test_moxy_reply_recovery_pg import add, correction, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


def incoming(store, text, mid):
    store.record(TENANT, ACCOUNT, event(mid=mid, body=text))
    store.drain(TENANT, ACCOUNT)


def offer(quote, *, text='Here is the first outfit photo.', action='offer', kind='direct'):
    return decision(action, text, 'first', sequence='fixture_level',
                    request_kind=kind, request_quote=quote)


def retries(engine):
    with Session(engine) as session:
        return [e for e in session.scalars(select(MoxyEvent))
                if json.loads(e.payload_json).get('correction_of')]


@pytest.mark.parametrize('current', [
    'Please send me the first photo.',
    'Please send me the first photo, thanks babe.',
])
def test_current_explicit_request_survives_older_topic_and_saved_preferences(
        database, monkeypatch, current):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        with Session(engine) as session, session.begin():
            fan = session.get(MoxyFan, (ACCOUNT, 101))
            saved = json.loads(fan.state_json)
            saved.update(ordinary_chat_only=True, sales_refused=True)
            fan.state_json = json.dumps(saved)
        incoming(store, 'I like your feet.', 2)
        incoming(store, current, 3)
        identity = add(service, key='current-request', mid=3)
        generated = model(offer(current))
        service.prepare(TENANT, ACCOUNT, identity, generated)
        assert row(engine, identity).status == 'ready'
        assert len(generated.calls) == 1 and retries(engine) == []
        outcome, transport, _ = send(service, authority, engine, identity)
        assert outcome == 'accepted' and len(transport.calls) == 1
        assert transport.calls[0].media_ids == (7101,)
        assert transport.calls[0].price_cents == 7500
        with Session(engine) as session:
            saved = json.loads(session.get(MoxyFan, (ACCOUNT, 101)).state_json)
            assert saved['ordinary_chat_only'] is True
            assert saved['sales_refused'] is True
            assert session.scalar(select(ContentOffer)).status == 'open'
            assert session.get(Message, (ACCOUNT, 101, 9001)).is_paid is False
    finally:
        store.close()


@pytest.mark.parametrize('bad_quote, reason', [
    (None, 'request_quote_required'),
    ('A request that was never said.', 'current_request_evidence_missing'),
    ('I like your feet.', 'current_request_authority_unverified'),
])
def test_evidence_repair_retains_product_and_terms_and_delivers_exactly_once(
        database, monkeypatch, bad_quote, reason):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        with Session(engine) as session, session.begin():
            fan = session.get(MoxyFan, (ACCOUNT, 101))
            saved = json.loads(fan.state_json)
            saved['ordinary_chat_only'] = True
            fan.state_json = json.dumps(saved)
        incoming(store, 'I like your feet.', 2)
        current = 'Please send me the first photo.'
        incoming(store, current, 3)
        identity = add(service, key='repair-evidence', mid=3)
        first = model(offer(bad_quote))
        service.prepare(TENANT, ACCOUNT, identity, first)
        assert row(engine, identity).status == 'held'
        assert row(engine, identity).reason == reason
        retry = correction(engine)
        retained = json.loads(row(engine, retry).payload_json)['rejected']
        assert retained.get('product_id') is None
        assert retained['keep']['product_id'] == 'first'
        assert retained['keep_terms']['price_cents'] == 7500
        second = model(offer(current, text='The first outfit photograph is $75.'))
        service.prepare(TENANT, ACCOUNT, retry, second)
        assert row(engine, retry).status == 'ready'
        projected = json.loads(second.calls[0][1]['content'])
        assert 'first' in projected['products']
        outcome, transport, intent = send(service, authority, engine, retry)
        assert outcome == 'accepted' and len(transport.calls) == 1
        assert transport.calls[0].media_ids == (7101,)
        assert transport.calls[0].price_cents == 7500
        assert authority.dispatch(TENANT, ACCOUNT, intent, transport) == 'accepted'
        assert len(transport.calls) == 1
        unused = model(offer(current))
        service.prepare(TENANT, ACCOUNT, retry, unused)
        assert len(first.calls) == len(second.calls) == 1 and unused.calls == []
        assert len(retries(engine)) == 1
        with Session(engine) as session:
            assert len(session.scalars(select(OfTextIntent)).all()) == 1
            assert len(session.scalars(select(ContentOffer)).all()) == 1
    finally:
        store.close()


def test_missing_resend_quote_repairs_the_existing_unpurchased_card(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 'Please send me the first photo.', 2)
        first_id = add(service, key='original-card', mid=2)
        service.prepare(TENANT, ACCOUNT, first_id, model(offer('Please send me the first photo.')))
        assert send(service, authority, engine, first_id)[0] == 'accepted'
        current = 'Please send me the first photo again.'
        incoming(store, current, 3)
        identity = add(service, key='resend-card', mid=3)
        first = model(offer(None, text='Here is that outfit photograph again.',
                            action='resend', kind='resend'))
        service.prepare(TENANT, ACCOUNT, identity, first)
        assert row(engine, identity).reason == 'request_quote_required'
        retry = correction(engine)
        retained = json.loads(row(engine, retry).payload_json)['rejected']
        assert retained['keep']['action'] == 'resend'
        assert retained['keep']['product_id'] == 'first'
        second = model(offer(current, text='The requested outfit photograph, sent again.',
                             action='resend', kind='resend'))
        service.prepare(TENANT, ACCOUNT, retry, second)
        assert row(engine, retry).status == 'ready'
        outcome, transport, _ = send(service, authority, engine, retry, mid=9002)
        assert outcome == 'accepted' and transport.calls[0].media_ids == (7101,)
        assert transport.calls[0].price_cents == 7500
        assert len(first.calls) == len(second.calls) == len(transport.calls) == 1
        with Session(engine) as session:
            assert session.get(Message, (ACCOUNT, 101, 9001)).is_paid is False
            assert session.get(Message, (ACCOUNT, 101, 9002)).is_paid is False
    finally:
        store.close()


def test_failed_evidence_correction_never_gets_third_attempt_or_send(database, monkeypatch):
    engine, store, service, _, _, _ = setup(database, monkeypatch)
    try:
        incoming(store, 'Please send me the first photo.', 2)
        identity = add(service, key='failed-evidence', mid=2)
        first, second = model(offer(None)), model(offer(None))
        service.prepare(TENANT, ACCOUNT, identity, first)
        retry = correction(engine)
        service.prepare(TENANT, ACCOUNT, retry, second)
        assert row(engine, retry).status == 'held'
        assert row(engine, retry).reason == 'request_quote_required'
        unused = model(offer('Please send me the first photo.'))
        service.prepare(TENANT, ACCOUNT, retry, unused)
        assert unused.calls == [] and len(retries(engine)) == 1
        assert len(first.calls) == len(second.calls) == 1
        with Session(engine) as session:
            assert session.scalar(select(OfTextIntent.intent_id)) is None
        incoming(store, 'How was your day?', 3)
        next_id = add(service, key='ordinary-chat-after-failure', mid=3)
        service.prepare(TENANT, ACCOUNT, next_id, model(decision(text='Good to hear from you.')))
        assert row(engine, next_id).status == 'ready'
    finally:
        store.close()


def test_malformed_output_does_not_establish_a_product_to_preserve(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        identity = add(service, key='malformed-selection')
        first = SyntheticNativeModel(raw_output='{"action":"offer","product_id":"first",broken')
        service.prepare(TENANT, ACCOUNT, identity, first)
        assert row(engine, identity).decision_json is None
        retry = correction(engine)
        retained = json.loads(row(engine, retry).payload_json)['rejected']
        assert retained['kind'] == 'format' and retained.get('product_id') is None
        assert 'keep' not in retained and 'keep_terms' not in retained
        second = model(decision(text='Good to hear from you.'))
        service.prepare(TENANT, ACCOUNT, retry, second)
        assert row(engine, retry).status == 'ready'
        outcome, transport, _ = send(service, authority, engine, retry)
        assert outcome == 'accepted' and transport.calls[0].media_ids == ()
        with Session(engine) as session:
            assert session.scalar(select(ContentOffer)) is None
    finally:
        store.close()


def test_ordinary_question_cannot_persist_an_invented_sales_hold(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        current = 'How was your day?'
        incoming(store, current, 2)
        identity = add(service, key='ordinary-chat-observation', mid=2)
        annotation = dict(kind='ordinary_chat_only', active=True, message_id=2,
                          quote=current, product_id=None)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            text='Good to hear from you.', observations=[annotation])))
        assert row(engine, identity).status == 'ready'
        assert send(service, authority, engine, identity)[0] == 'accepted'
        with Session(engine) as session:
            saved = json.loads(session.get(MoxyFan, (ACCOUNT, 101)).state_json)
            assert not saved.get('ordinary_chat_only')
            assert 'ordinary_chat_only' not in saved.get('constraint_evidence', {})
    finally:
        store.close()
