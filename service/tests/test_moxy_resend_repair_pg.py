"""Full-path resend repair using disposable data and synthetic delivery."""
import json

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from db.models import Message
from db.onlyfans_delivery_models import OfTextIntent
from test_moxy_integrated_pg import setup, decision, model
from test_moxy_reply_recovery_pg import add, correction, row, send
from test_onlyfans_foundation_pg import database, event
from test_onlyfans_text_delivery_pg import ACCOUNT, TENANT


def prior_card(engine, service, authority):
    identity = add(service, key='initial-card')
    service.prepare(TENANT, ACCOUNT, identity, model(decision(
        'offer', 'Here is the outfit photo.', 'first', sequence='fixture_level')))
    assert row(engine, identity).status == 'ready'
    result, wire, _ = send(service, authority, engine, identity, mid=9001)
    assert result == 'accepted' and wire.calls[0].media_ids == (7101,)
    assert wire.calls[0].price_cents == 7500


def incoming(store, text):
    store.record(TENANT, ACCOUNT, event(mid=2, body=text))
    store.drain(TENANT, ACCOUNT)


def test_actual_resend_request_repairs_a_duplicate_offer_without_changing_terms(database, monkeypatch):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        prior_card(engine, service, authority)
        text = 'Send it again.'
        incoming(store, text)
        identity = add(service, key='current-request', mid=2)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            'offer', 'Here is the photo.', 'first', sequence='fixture_level',
            request_kind='direct', request_quote=text)))
        assert row(engine, identity).reason == 'media_previously_sent_requires_explicit_resend'
        retry = correction(engine)
        brief = json.loads(row(engine, retry).payload_json)['rejected']
        assert brief['kind'] == 'factual' and brief['keep']['action'] == 'resend'
        assert brief['keep_terms']['price_cents'] == 7500
        service.prepare(TENANT, ACCOUNT, retry, model(decision(
            'resend', 'Here is the photo.', 'first', sequence='fixture_level',
            request_kind='resend', request_quote=text)))
        assert row(engine, retry).status == 'ready', row(engine, retry).reason
        result, wire, _ = send(service, authority, engine, retry, mid=9002)
        assert result == 'accepted' and len(wire.calls) == 1
        assert wire.calls[0].media_ids == (7101,) and wire.calls[0].price_cents == 7500
        with Session(engine) as session:
            assert session.get(Message, (ACCOUNT, 101, 9002)).is_paid is False
            assert session.scalar(select(func.count()).select_from(OfTextIntent)) == 2
    finally:
        store.close()


@pytest.mark.parametrize('text', ['How was your day?', 'Send it again tomorrow.'])
def test_unrequested_or_deferred_resend_is_held_without_a_delivery(database, monkeypatch, text):
    engine, store, service, authority, _, _ = setup(database, monkeypatch)
    try:
        prior_card(engine, service, authority)
        incoming(store, text)
        identity = add(service, key='not-a-resend', mid=2)
        service.prepare(TENANT, ACCOUNT, identity, model(decision(
            'resend', 'Here is the photo.', 'first', sequence='fixture_level',
            request_kind='resend', request_quote=text)))
        assert row(engine, identity).status == 'held'
        assert row(engine, identity).reason == 'current_request_authority_unverified'
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(OfTextIntent)) == 1
    finally:
        store.close()
