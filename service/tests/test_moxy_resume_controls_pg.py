"""Resume regressions. Disposable PG and registered fixture seams only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from test_onlyfans_foundation_pg import database
from test_moxy_integrated_pg import setup, run, decision, model, status
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from db.models import Fan, OfConversationState
from db.onlyfans_delivery_models import OfTextIntent
from db.onlyfans_moxy_models import MoxyAccount, MoxyEvent
from moxy_replacement.runtime import MoxyService
from onlyfans_runtime.store import Refused, utcnow
from onlyfans_runtime.text_delivery import SyntheticTextTransport


def client_for(store, monkeypatch):
    from onlyfans_commands.configuration_api import router, HEADER, SCHEMA
    from onlyfans_read.agency_scope import AgencyScopeMiddleware
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TOKEN', 'fixture-moxy-config-token')
    monkeypatch.setenv('ONLYFANS_CONFIGURATION_TENANT', TENANT)
    app = FastAPI()
    app.state.control_store = store
    app.include_router(router)
    app.add_middleware(AgencyScopeMiddleware)
    return TestClient(app), {
        'Authorization': 'Bearer fixture-moxy-config-token', HEADER: SCHEMA,
    }, '/onlyfans/configuration/v1/accounts/' + ACCOUNT + '/moxy'


def test_authenticated_mode_save_and_revision_conflict(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    client, headers, url = client_for(store, monkeypatch)
    body = dict(expectedRevision=1, mode='off', settings=settings, actor='op-session:'+'a'*64)
    result = client.put(url, headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['moxy']['mode'] == 'off'
    result = client.put(url, headers=headers, json=body)
    assert result.status_code == 409 and result.json()['error'] == 'moxy_revision_conflict'
    assert client.get(url, headers=headers).json()['moxy']['revision'] == 2
    store.close()


@pytest.mark.parametrize('body', [[], None, {'unexpected': 1},
    {'expectedRevision': True, 'mode': 'off', 'settings': {}, 'actor': 'op-session:'+'a'*64}])
def test_malformed_configuration_is_a_client_error(database, monkeypatch, body):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    client, headers, url = client_for(store, monkeypatch)
    result = client.put(url, headers={**headers, 'Content-Type': 'application/json'}, content=json.dumps(body))
    assert result.status_code == 400
    store.close()


def test_api_rejects_forged_actor_and_oversized_body(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    client, headers, url = client_for(store, monkeypatch)
    body = dict(expectedRevision=1, mode='off', settings=settings, actor='fixture-operator')
    assert client.put(url, headers=headers, json=body).status_code == 403
    assert client.put(url, headers={**headers, 'Content-Type': 'application/json'},
        content='x'*131073).status_code == 413
    store.close()


def test_agency_put_exception_is_exact_and_manage_only(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    client, headers, url = client_for(store, monkeypatch)
    actor = 'agency-user:'+'a'*8+'-'+'a'*4+'-'+'a'*4+'-'+'a'*4+'-'+'a'*12+':'+'b'*8+'-'+'b'*4+'-'+'b'*4+'-'+'b'*4+'-'+'b'*12
    grant = dict(creatorIds=[], actor=actor, canManage=False, canChat=True)
    headers = {**headers, 'x-onlyfans-agency-scope': json.dumps(grant)}
    body = dict(expectedRevision=1, mode='off', settings=settings, actor=actor)
    assert client.put(url, headers=headers, json=body).status_code == 403
    grant['canManage'] = True
    headers['x-onlyfans-agency-scope'] = json.dumps(grant)
    # A management grant still cannot cross a creator assignment boundary.
    assert client.put(url, headers=headers, json=body).status_code == 404
    assert client.put(url+'/other', headers=headers, json=body).status_code == 403
    assert client.put(url.removesuffix('/moxy'), headers=headers, json=body).status_code == 403
    store.close()


def test_off_is_available_during_unknown_delivery_without_erasing_it(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    identity, _ = run(service, engine, decision())
    intent = service.queue(TENANT, ACCOUNT, identity)
    wire = SyntheticTextTransport(ACCOUNT, outcome='accepted_lost_response')
    assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    assert service.configure(TENANT, ACCOUNT, expected_revision=1, mode='off',
        settings=settings, actor='fixture-operator')['mode'] == 'off'
    with Session(engine) as s:
        assert s.get(OfTextIntent, (ACCOUNT, intent)).status == 'unknown'
        assert s.get(MoxyAccount, ACCOUNT).mode == 'off'
    with pytest.raises(Refused, match='uncertain_delivery'):
        service.configure(TENANT, ACCOUNT, expected_revision=2, mode='on',
            settings=settings, actor='fixture-operator')
    assert len(wire.calls) == 1
    store.close()


def test_off_event_resumes_with_fresh_validation_and_no_preview_replay(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch, mode='off')
    identity, m = run(service, engine, decision())
    assert status(engine, identity) == 'held' and m.calls == []
    service.configure(TENANT, ACCOUNT, expected_revision=1, mode='preview',
        settings=settings, actor='fixture-operator')
    service.prepare(TENANT, ACCOUNT, identity, model(decision()))
    assert status(engine, identity) == 'preview'
    service.configure(TENANT, ACCOUNT, expected_revision=2, mode='on',
        settings=settings, actor='fixture-operator')
    assert service.step(TENANT, ACCOUNT, model(decision())) == 0
    with Session(engine) as s:
        assert not s.scalars(select(OfTextIntent)).all()
    store.close()


def test_step_overlaps_distinct_fans_and_does_not_wait_for_future_events(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    with Session(engine) as s, s.begin():
        for fan in (201, 202, 203, 204):
            s.add(Fan(account_id=ACCOUNT, fan_id=fan))
            s.flush()
            s.add(OfConversationState(account_id=ACCOUNT, fan_id=fan, mode='automated',
                revision=0, input_revision=0, takeover_state='none'))
    future = service.add_event(TENANT, ACCOUNT, 101, 'turn', 'paced-future', {})
    with Session(engine) as s, s.begin():
        s.get(MoxyEvent, (ACCOUNT, future)).not_before = utcnow()+timedelta(minutes=5)
    ids = [service.add_event(TENANT, ACCOUNT, fan, 'scheduled', str(fan), {})
        for fan in (201, 202, 203, 204)]
    reached = threading.Barrier(4)
    m = model(decision(), on_generate=lambda: reached.wait(timeout=15))
    assert service.step(TENANT, ACCOUNT, m) == 4
    assert len(m.calls) == 4
    with Session(engine) as s:
        assert [s.get(MoxyEvent, (ACCOUNT, i)).status for i in ids] == ['queued']*4
        assert s.get(MoxyEvent, (ACCOUNT, future)).status == 'pending'
    store.close()


def test_distinct_events_for_one_fan_cannot_generate_together(database, monkeypatch):
    engine, store, service, authority, claim, settings = setup(database, monkeypatch)
    ids = [service.add_event(TENANT, ACCOUNT, 101, 'scheduled', str(i), {}) for i in range(2)]
    models = [model(decision(), delay_seconds=.2) for _ in ids]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda i: MoxyService(store).prepare(TENANT, ACCOUNT, ids[i], models[i]), range(2)))
    assert sum(len(m.calls) for m in models) == 1
    assert sorted(status(engine, i) for i in ids) == ['pending', 'ready']
    store.close()
