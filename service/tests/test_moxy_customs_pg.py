"""Custom-content lifecycle: quote, payment route, owner, deadline, fulfilment."""
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup, decision, run
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.onlyfans_case_models import OfOperatorCase
from db.onlyfans_moxy_models import MoxyFan
from moxy_replacement import customs
from moxy_replacement.controls import view
from onlyfans_runtime.store import Refused


def custom_request(service, engine, key='custom-1'):
    return run(service, engine, decision('reply', 'ooo tell me what you had in mind and ill check if its available', permission='customs'), key=key)


def obligations(engine):
    with Session(engine) as s:
        return json.loads(s.get(MoxyFan, (ACCOUNT, 101)).state_json).get('obligations', [])


def step_to(store, stage, **kw):
    return customs.advance(store, TENANT, ACCOUNT, 101, obligations_id(store), stage=stage, actor='fixture-operator',
        evidence_ref='fixture:evidence', **kw)


def obligations_id(store):
    with Session(store.engine) as s:
        return next(o['id'] for o in json.loads(s.get(MoxyFan, (ACCOUNT, 101)).state_json)['obligations'] if o.get('kind') == 'custom')


def test_vip_request_is_recorded_owned_and_walks_the_whole_lifecycle(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    identity, m = custom_request(service, engine)
    item = obligations(engine)[0]
    assert item['kind'] == 'custom' and item['stage'] == 'requested' and item['owner'] == 'fixture-operator'
    assert item['minimum_cents'] == 50000 and item['quote_cents'] is None and item['deadline'] is None
    assert 'never promise, price or accept a custom' in json.dumps(m.calls[0])
    with Session(engine) as s:
        case = s.scalar(select(OfOperatorCase).where(OfOperatorCase.category == 'custom_request'))
        assert case.owner == 'fixture-operator' and case.scope == 'offer' and case.status == 'open'
    # A second request while one is open does not duplicate the obligation.
    custom_request(service, engine, key='custom-2')
    assert len([o for o in obligations(engine) if o['kind'] == 'custom']) == 1
    assert view(store, TENANT, ACCOUNT)['customs'][0]['waitingOn'] == 'owner_must_confirm_availability'

    with pytest.raises(Refused, match='out_of_order'):step_to(store, 'quoted', quote_cents=60000)
    assert step_to(store, 'confirmed')['authorised_by'] == 'fixture-operator'
    with pytest.raises(Refused, match='below_creator_minimum'):step_to(store, 'quoted', quote_cents=49999)
    with pytest.raises(Refused, match='invalid_custom_deadline'):step_to(store, 'quoted', quote_cents=60000, deadline='soon')
    step_to(store, 'quoted', quote_cents=60000)
    row = view(store, TENANT, ACCOUNT)['customs'][0]
    assert row['stage'] == 'quoted' and row['waitingOn'] == 'awaiting_fan_payment;deadline_unset' and row['overdue'] is False
    with pytest.raises(Refused, match='on_platform'):step_to(store, 'paid', payment_route='venmo')
    step_to(store, 'paid', payment_route='on_platform')
    with pytest.raises(Refused, match='declined'):step_to(store, 'declined')
    with pytest.raises(Refused, match='fulfilment_evidence'):step_to(store, 'fulfilled')
    done = step_to(store, 'fulfilled', fulfilment_ref='fixture:delivered-message')
    assert done['status'] == 'closed' and done['stage'] == 'fulfilled'
    assert view(store, TENANT, ACCOUNT)['customs'] == []
    with pytest.raises(Refused, match='open_custom_required'):step_to(store, 'fulfilled', fulfilment_ref='again')
    store.close()


def test_deadline_is_operator_supplied_and_overdue_is_surfaced(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    custom_request(service, engine)
    step_to(store, 'confirmed')
    step_to(store, 'quoted', quote_cents=70000, deadline='2020-01-01T00:00:00+10:00')
    row = view(store, TENANT, ACCOUNT)['customs'][0]
    assert row['overdue'] is True and row['deadline'] == '2019-12-31T14:00:00'
    store.close()


def test_declined_before_payment_closes_without_charge(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    custom_request(service, engine)
    assert step_to(store, 'declined')['status'] == 'closed'
    store.close()


def test_non_vip_custom_request_creates_no_obligation(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    service.fan_facts(TENANT, ACCOUNT, 101, {'ownership_verified': True, 'purchase_links_verified': True, 'vip': False,
        'spender': True}, actor='fixture-operator', evidence_ref='fixture:fan-authority')
    custom_request(service, engine)
    assert [o for o in obligations(engine) if o.get('kind') == 'custom'] == []
    store.close()


def test_every_step_needs_an_actor_and_evidence(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    custom_request(service, engine)
    with pytest.raises(Refused, match='verified_operator_state_required'):
        customs.advance(store, TENANT, ACCOUNT, 101, obligations_id(store), stage='confirmed', actor='', evidence_ref='x')
    store.close()


def test_cristina_confirmation_needs_the_assigned_operator(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch, creator='cristina')
    custom_request(service, engine)
    item = obligations(engine)[0]
    assert item['kind'] == 'custom' and item['minimum_cents'] == 100000
    with pytest.raises(Refused, match='custom_not_authorised'):
        customs.advance(store, TENANT, ACCOUNT, 101, item['id'], stage='confirmed', actor='someone-else', evidence_ref='x')
    assert customs.advance(store, TENANT, ACCOUNT, 101, item['id'], stage='confirmed', actor='fixture-operator',
        evidence_ref='fixture:aa-check')['stage'] == 'confirmed'
    store.close()
