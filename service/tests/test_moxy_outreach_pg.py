"""Moxy-initiated fulfilment comes only from owed obligations with an approved remedy."""
import json
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup, decision, model, status
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.onlyfans_moxy_models import MoxyEvent, MoxyFan
from moxy_replacement import outreach
from moxy_replacement.controls import update_fan
from moxy_replacement.runtime import MoxyService


def owed(product='first', ident='owed-1'):
    return {'id': ident, 'status': 'open', 'owner': 'moxy', 'source_ref': 'fixture:accepted-promise', 'product_id': product}


def events(engine, kind):
    with Session(engine) as s:
        return s.scalars(select(MoxyEvent).where(MoxyEvent.kind == kind)).all()


def test_owed_obligation_with_approved_remedy_enqueues_fulfilment_once(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    update_fan(store, TENANT, ACCOUNT, 101, state={'obligations': [owed()]}, actor='fixture-operator', evidence_ref='fixture:records')
    m = model(decision('fulfil', 'Here is the image that was still owed.', 'first', resolve_obligation='owed-1'))
    assert service.step(TENANT, ACCOUNT, m) == 1
    rows = events(engine, 'fulfilment')
    assert len(rows) == 1 and json.loads(rows[0].payload_json)['obligation_id'] == 'owed-1'
    assert status(engine, rows[0].event_id) == 'queued'
    # An unchanged obligation never creates a second event on later ticks.
    service.step(TENANT, ACCOUNT, model(decision('none', '')))
    assert len(events(engine, 'fulfilment')) == 1
    store.close()


def test_unapproved_or_unmapped_obligation_produces_nothing_and_is_reported(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    update_fan(store, TENANT, ACCOUNT, 101, state={'obligations': [owed('second', 'no-remedy'), owed('ghost', 'no-product')]},
        actor='fixture-operator', evidence_ref='fixture:records')
    with Session(engine) as s, s.begin():
        result = outreach.produce(s, ACCOUNT, settings)
    assert result['enqueued'] == []
    assert {u['reason'] for u in result['unresolved']} == {'remedy_authority_missing', 'product_mapping_missing'}
    assert events(engine, 'fulfilment') == []
    store.close()


def test_buying_question_and_closed_obligations_are_not_fulfilment(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    closed = {**owed(), 'status': 'fulfilled', 'id': 'closed'}
    question = {'id': 'q', 'kind': 'buying_question', 'status': 'open', 'owner': 'moxy', 'source_ref': 'x', 'question': 'how much?'}
    update_fan(store, TENANT, ACCOUNT, 101, state={'obligations': [closed, question]}, actor='fixture-operator', evidence_ref='fixture:records')
    with Session(engine) as s, s.begin():
        result = outreach.produce(s, ACCOUNT, settings)
    assert result == {'enqueued': [], 'unresolved': []}
    store.close()


def test_preview_mode_produces_nothing(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch, mode='preview')
    update_fan(store, TENANT, ACCOUNT, 101, state={'obligations': [owed()]}, actor='fixture-operator', evidence_ref='fixture:records')
    service.step(TENANT, ACCOUNT, model(decision('none', '')))
    assert events(engine, 'fulfilment') == []
    store.close()


def test_pilot_scope_defers_fulfilment_producer(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    update_fan(store, TENANT, ACCOUNT, 101, state={'obligations': [owed()]}, actor='fixture-operator', evidence_ref='fixture:records')
    pilot = {**settings, 'pilot': {'fan_ids': [101], 'product_ids': ['first', 'second', 'third', 'fourth'], 'supervisor': 'fixture-operator'}}
    with Session(engine) as s, s.begin():
        result = outreach.produce(s, ACCOUNT, pilot)
    assert result['enqueued'] == [] and result['unresolved'][0]['reason'] == 'pilot_automation_deferred'
    store.close()


def test_undefined_producers_are_explicitly_unresolved():
    assert set(outreach.unresolved()) == {'invitation', 'scheduled', 'welcome_offer', 'promotion'}
