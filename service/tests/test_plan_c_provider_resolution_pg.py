"""Authenticated, exact-item classification of retained benign evidence; material uncertainty stays held."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from conversation_engine import operator_v2, pilot_scope
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from plan_c_worker import _work_counts
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, PROVIDER
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_scope_pg import ACTOR, OTHER
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT
from test_engine_approval_api_pg import api, HEADERS, url
from test_plan_c_offline_operations_pg import http_event


def retained_event(pilot, monkeypatch, key, event='messages.unread_count_updated', payload=None):
    # Reproduce the old classifier through actual signed HTTP intake; no authority is seeded.
    with monkeypatch.context() as legacy:
        legacy.setattr('model_transport.pilot_supervised._known_irrelevant', lambda raw: False)
        if event == 'messages.deleted':
            # Earlier releases classified deletion as unknown; preserve that
            # historical digest path even though current intake recognizes it.
            from onlyfans_runtime.store import Refused
            def legacy_unknown(raw):
                raise Refused('unsupported legacy event')
            legacy.setattr('model_transport.pilot_supervised._event_fan', legacy_unknown)
        response, raw = http_event(pilot, {'event':event, 'account_id':PROVIDER,
            'payload': {'count':0} if payload is None else payload}, key)
        assert response.status_code == 202
    return raw.decode('utf-8')


def items(api):
    response = api.client.get(url('unresolved'), headers={**HEADERS,'X-Plan-C-Actor':ACTOR})
    assert response.status_code == 200, response.text
    return [item for item in response.json()['items'] if item['kind']=='provider_event']


def command(item, raw, n, **patch):
    return {'commandKey':f'{n:064x}','actor':ACTOR,'evidenceRef':'operator:fansapi:exact retained delivery reviewed',
        'scopeRevision':item['scopeRevision'],'eventEpoch':item['eventEpoch'],'eventKey':item['eventKey'],
        'payloadSha256':item['payloadSha256'],'rawBody':raw,**patch}


def resolve(api, item, body, headers=HEADERS, account=ACCOUNT):
    return api.client.post(url('unresolved/'+item['id']+'/reconcile',account=account),headers=headers,json=body)


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_exact_provider_resolution_is_audited_idempotent_scoped_and_wakes_waiting_work(supervised, api, monkeypatch):
    world, env, model, seen, pilot = supervised
    original = {}
    for key in ['benign-one','benign-two']:original[key]=retained_event(pilot,monkeypatch,key)
    pending = items(api)
    assert len(pending)==2 and _work_counts(pilot)['malformedHolds']==2
    first, second = pending
    body = command(first, original[first['eventKey']], 18400)
    assert resolve(api,first,body,headers={}).status_code==401
    assert resolve(api,first,{**body,'actor':OTHER}).status_code==409
    assert resolve(api,first,{**body,'scopeRevision':body['scopeRevision']-1}).status_code in (400,409)
    assert resolve(api,first,{**body,'rawBody':body['rawBody']+' '}).status_code==409
    assert resolve(api,first,body,account='other-account').status_code==404
    assert _work_counts(pilot)['malformedHolds']==2
    stage(pilot,18401,'hello')
    assert world.fans.posts==[]
    result=resolve(api,first,body)
    assert result.status_code==200 and result.json()['resolved'], result.text
    replay=resolve(api,first,body)
    assert replay.status_code==200 and replay.json()['replayed']
    assert resolve(api,first,{**body,'evidenceRef':'operator:fansapi:changed proof'}).status_code==409
    assert _work_counts(pilot)['malformedHolds']==1
    assert [row['id'] for row in items(api)]==[second['id']]
    pilot.tick();assert world.fans.posts==[]
    body2=command(second,original[second['eventKey']],18402)
    # Two independently keyed reviewers converge on one canonical resolution audit.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda n:operator_v2.reconcile(world.store,TENANT,ACCOUNT,second['id'],
            {**body2,'commandKey':f'{n:064x}'}),[18402,18403]))
    assert all(row['resolved'] for row in results)
    assert _work_counts(pilot)['malformedHolds']==0
    with Session(world.database[1]) as s:
        evidence=list(s.scalars(select(OfEnginePilotWebhookDigest)))
        assert len(evidence)==2 and all(row.safety_hold for row in evidence), 'original holds must remain immutable evidence'
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.kind=='plan_c_webhook_resolution'))==2
        audits=list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=='plan_c_webhook_resolution')))
        assert all(row.actor==ACTOR and json.loads(row.before_json)['payloadSha256'] for row in audits)
    restarted=build_supervised_pilot(world.store,env,model_transport=model,fans_transport=world.fans.transport)
    for _ in range(4):restarted.tick()
    assert len(world.fans.posts)==1 and len(seen)==1


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
@pytest.mark.parametrize('event,payload',[('posts.new',{}),('messages.deleted',{'id':'123'}),
    ('messages.unread_count_updated',{'count':True})])
def test_material_unknown_or_malformed_digest_cannot_be_cleared_as_benign(supervised,api,monkeypatch,event,payload):
    world, _, _, _, pilot=supervised
    raw=retained_event(pilot,monkeypatch,'cannot-clear',event,payload)
    [item]=items(api)
    result=resolve(api,item,command(item,raw,18410))
    assert result.status_code==409 and _work_counts(pilot)['malformedHolds']==1
    stage(pilot,18411,'hello');pilot.tick()
    assert world.fans.posts==[]


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_a_new_conflicting_payload_reholds_a_previously_classified_event(supervised,api,monkeypatch):
    world, _, _, _, pilot=supervised
    raw=retained_event(pilot,monkeypatch,'later-conflict')
    [item]=items(api)
    assert resolve(api,item,command(item,raw,18420)).json()['resolved']
    assert _work_counts(pilot)['malformedHolds']==0
    response,_=http_event(pilot,{'event':'messages.unread_count_updated','account_id':PROVIDER,
        'payload':{'count':1}},'later-conflict')
    assert response.status_code==202 and _work_counts(pilot)['malformedHolds']==2
    assert resolve(api,item,command(item,raw,18421)).status_code==409
    stage(pilot,18422,'hello');pilot.tick()
    assert world.fans.posts==[]
