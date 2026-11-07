"""Scoped, replay-safe operator request ownership and closure."""
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_onlyfans_foundation_pg import database  # noqa: F401
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT, configured
from test_engine_simulated_journey_pg import incoming
from conversation_engine.engine import EngineService, SyntheticModel, SyntheticProposal
from conversation_engine.operator import execute
from db.onlyfans_engine_models import OfEngineDecision, OfEngineOperatorCommand, OfEngineRequest
from fans_api_adapter.intake import FansWebhookAdapter
from fans_api_adapter.synthetic_transport import FansApiSyntheticTransport, SyntheticFansProvider
from onlyfans_runtime.store import Refused

ACTOR = 'op-session:' + 'a'*64
OTHER = 'op-session:' + 'b'*64


def setup(database, *, unknown=False):
    store, authority = configured(database, mode='automated')
    FansWebhookAdapter(store).bind_simulated(TENANT, ACCOUNT, 'acct_operator', 'creator-fixture')
    engine = EngineService(store, SyntheticModel({'hello':SyntheticProposal('Hello.')}))
    engine.select_simulated(TENANT, ACCOUNT, FAN)
    incoming(store, 1, 'hello')
    decision = engine.plan(TENANT, ACCOUNT, FAN, 1)
    if unknown:
        intent = engine.queue(TENANT, ACCOUNT, decision)
        wire = FansApiSyntheticTransport(store, TENANT, ACCOUNT, SyntheticFansProvider(('unknown',)))
        assert authority.dispatch(TENANT, ACCOUNT, intent, wire) == 'unknown'
    with Session(database[1]) as s:
        request = s.get(OfEngineRequest, (ACCOUNT, s.get(OfEngineDecision, (ACCOUNT, decision)).request_id))
        request_id, revision = request.request_id, request.revision
    return store, authority, engine, decision, request_id, revision


def command(store, request_id, revision, action, key, *, actor=ACTOR, target=None, evidence=None):
    return execute(store, TENANT, ACCOUNT, request_id, action=action, expected_revision=revision,
        command_key=key*64, actor=actor, target=target, evidence_ref=evidence, expected_fan_id=FAN)


def test_handover_requires_acceptance_and_audited_resolution(database):
    store, authority, engine, decision, request_id, revision = setup(database)
    sent = command(store, request_id, revision, 'request_handover', '1', target=ACTOR)
    assert sent['status'] == 'handover_pending' and sent['owner'] == 'engine'
    assert command(store, request_id, revision, 'request_handover', '1', target=ACTOR)['replayed'] is True
    with pytest.raises(Refused, match='idempotency'):
        command(store, request_id, revision, 'request_handover', '1', target=OTHER)
    with pytest.raises(Refused, match='acceptance'):
        command(store, request_id, sent['recordedRevision'], 'accept_handover', '2')
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0,
        command_key='human-takeover', actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = command(store, request_id, sent['recordedRevision'], 'accept_handover', '2')
    assert accepted['status'] == 'handover_accepted' and accepted['owner'] == ACTOR
    with pytest.raises(Refused):
        command(store, request_id, accepted['recordedRevision'], 'complete', '3', actor=OTHER, evidence='synthetic:review')
    done = command(store, request_id, accepted['recordedRevision'], 'complete', '3', evidence='synthetic:review')
    assert done['status'] == 'completed' and done['nextStep'] == 'none'
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'cancelled'
        assert s.get(OfEngineOperatorCommand, (ACCOUNT, '3'*64)).evidence_ref == 'synthetic:review'
    with pytest.raises(DBAPIError, match='append-only'):
        with database[1].begin() as c:
            c.execute(text('DELETE FROM of_engine_operator_command WHERE command_key=:key'), {'key':'3'*64})
    store.close()


def test_human_acceptance_does_not_clear_or_complete_unknown_delivery(database):
    store, authority, engine, decision, request_id, revision = setup(database, unknown=True)
    sent = command(store, request_id, revision, 'request_handover', '4', target=ACTOR)
    store.control(TENANT, ACCOUNT, FAN, mode='human_controlled', expected_revision=0,
        command_key='human-unknown', actor=ACTOR, reason='Take responsibility', action='request_takeover')
    accepted = command(store, request_id, sent['recordedRevision'], 'accept_handover', '5')
    with pytest.raises(Refused, match='unresolved delivery'):
        command(store, request_id, accepted['recordedRevision'], 'complete', '6', evidence='synthetic:review')
    with Session(database[1]) as s:
        assert s.get(OfEngineDecision, (ACCOUNT, decision)).status == 'unknown'
        assert s.get(OfEngineRequest, (ACCOUNT, request_id)).status == 'handover_accepted'
    store.close()


def test_operator_http_boundary_enforces_auth_scope_version_and_replay(database, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from onlyfans_commands.engine_operator_api import router, SCHEMA, HEADER
    store, authority, engine, decision, request_id, revision = setup(database)
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONTROL_TOKEN', 'fixture-control-token')
    monkeypatch.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
    for key in ('ONLYFANS_API_TOKEN','ONLYFANS_CONFIGURATION_TOKEN','ONLYFANS_CAMPAIGN_TOKEN','ONLYFANS_CONTENT_TOKEN','ONLYFANS_CONNECTION_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    app = FastAPI(); app.state.control_store = store; app.include_router(router)
    route = f'/onlyfans/control/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}/requests/{request_id}'
    headers = {'Authorization':'Bearer fixture-control-token', HEADER:SCHEMA}
    body = {'action':'request_handover','expectedRevision':revision,'commandKey':'7'*64,'actor':ACTOR,'target':ACTOR}
    with TestClient(app) as client:
        assert client.post(route, json=body, headers={**headers,'Authorization':'Bearer wrong'}).status_code == 401
        assert client.post(route, json=body, headers={**headers,HEADER:'old'}).status_code == 400
        assert client.post(route.replace(f':{FAN}/', ':999/'), json=body, headers=headers).status_code == 409
        first = client.post(route, json=body, headers=headers)
        assert first.status_code == 200 and first.json()['status'] == 'handover_pending'
        assert client.post(route, json=body, headers=headers).json()['replayed'] is True
        assert client.post(route.replace(f':{FAN}/', ':999/'), json=body, headers=headers).status_code == 409
    store.close()


def test_agency_chat_operator_route_is_scoped_and_denies_other_actors(database, monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from db.models import OfCreatorBinding
    from onlyfans_read.agency_scope import AgencyScopeMiddleware
    from onlyfans_commands.engine_operator_api import router, SCHEMA, HEADER
    store, authority, engine, decision, request_id, revision = setup(database)
    creator = '11111111-1111-1111-1111-111111111111'
    actor = 'agency-user:22222222-2222-2222-2222-222222222222:33333333-3333-3333-3333-333333333333'
    with Session(database[1]) as s, s.begin():
        s.get(OfCreatorBinding, 'binding-' + ACCOUNT).creator_id = creator
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE', 'api')
    monkeypatch.setenv('ONLYFANS_CONTROL_TOKEN', 'fixture-control-token')
    monkeypatch.setenv('ONLYFANS_CONTROL_TENANT', TENANT)
    for key in ('ONLYFANS_API_TOKEN','ONLYFANS_CONFIGURATION_TOKEN','ONLYFANS_CAMPAIGN_TOKEN','ONLYFANS_CONTENT_TOKEN','ONLYFANS_CONNECTION_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    app = FastAPI(); app.state.control_store = store; app.include_router(router); app.add_middleware(AgencyScopeMiddleware)
    route = f'/onlyfans/agency/v1/control/v1/accounts/{ACCOUNT}/conversations/{ACCOUNT}:{FAN}/requests/{request_id}'
    grant = {'creatorIds':[creator], 'actor':actor, 'canManage':False, 'canChat':True}
    headers = {'Authorization':'Bearer fixture-control-token', HEADER:SCHEMA, 'X-OnlyFans-Agency-Scope':json.dumps(grant)}
    body = {'action':'request_handover','expectedRevision':revision,'commandKey':'8'*64,'actor':actor,'target':actor}
    with TestClient(app) as client:
        assert client.post(route, json={**body, 'actor':ACTOR}, headers=headers).status_code == 400
        wrong = {**grant, 'creatorIds':[]}
        assert client.post(route, json=body, headers={**headers, 'X-OnlyFans-Agency-Scope':json.dumps(wrong)}).status_code == 409
        result = client.post(route, json=body, headers=headers)
        assert result.status_code == 200 and result.headers['X-OnlyFans-Access-Version'] == 'of-agency.v2'
        assert result.json()['action'] == 'request_handover' and result.json()['status'] == 'handover_pending'
    store.close()
