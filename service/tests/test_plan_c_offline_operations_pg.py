"""Offline event/retention boundaries; no live provider operations or hold bypasses."""
import hashlib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from db.onlyfans_engine_models import OfEnginePilotWebhookDigest, OfFansConnection, OfFansWebhook
from onlyfans_runtime.store import utcnow
from plan_c_worker import create_app
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world, inbound, PROVIDER
from test_plan_c_draft_app_pg import configured_draft, signed
from test_plan_c_supervised_pg import supervised, stage
from test_engine_approval_api_pg import api
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN


def http_event(pilot, event, key):
    raw, headers = signed(event, key=key)
    with TestClient(create_app(pilot, run_loop=False)) as client:
        return client.post('/onlyfans/webhooks/plan-c', content=raw, headers=headers), raw


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_documented_valid_account_counter_retains_digest_without_stopping_chat(supervised):
    world, _, _, seen, pilot = supervised
    response, raw = http_event(pilot, {'event':'messages.unread_count_updated', 'account_id':PROVIDER,
        'payload':{'count':0}}, 'documented-counter')
    assert response.status_code == 202
    with Session(world.database[1]) as s:
        digest = s.scalar(select(OfEnginePilotWebhookDigest))
        assert digest.payload_sha256 == hashlib.sha256(raw).hexdigest() and not digest.safety_hold
    stage(pilot, 18300, 'hello');pilot.tick()
    assert len(world.fans.posts) == 1


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('event,payload', [('messages.unread_count_updated', {'count':True}),
    ('messages.unread_count_updated', {'count':-1}), ('messages.unread_count_updated', {}),
    ('messages.deleted', {'id':'123'}), ('posts.new', {})])
def test_invalid_counter_material_or_unclassified_event_keeps_safety_hold(supervised, event, payload):
    world, _, _, seen, pilot = supervised
    response, _ = http_event(pilot, {'event':event, 'account_id':PROVIDER, 'payload':payload}, 'retained-hold')
    assert response.status_code == 202
    with Session(world.database[1]) as s:assert s.scalar(select(OfEnginePilotWebhookDigest)).safety_hold
    stage(pilot, 18310, 'hello');pilot.tick()
    assert world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_more_than_ten_thousand_digests_preserve_acknowledgement_and_conflict_detection(supervised):
    world, _, _, seen, pilot = supervised
    with Session(world.database[1]) as s, s.begin():
        epoch = s.scalar(select(OfFansConnection.epoch).where(OfFansConnection.account_id == ACCOUNT,
            OfFansConnection.status == 'active'))
        s.bulk_insert_mappings(OfEnginePilotWebhookDigest, [dict(account_id=ACCOUNT,epoch=epoch,
            event_key='retained-'+str(i), payload_sha256=hashlib.sha256(str(i).encode()).hexdigest(),
            safety_hold=False,received_at=utcnow()) for i in range(10000)])
    outside = inbound(18320, 'outside scope', fan=999)
    for _ in range(2):
        response, raw = http_event(pilot, outside, 'capacity-new')
        assert response.status_code == 202, 'durable digest intake is permanently blocked at a row-count threshold'
    with Session(world.database[1]) as s:
        assert s.scalar(select(func.count()).select_from(OfEnginePilotWebhookDigest)) == 10001
        assert s.scalar(select(OfEnginePilotWebhookDigest).where(
            OfEnginePilotWebhookDigest.event_key == 'capacity-new')).payload_sha256 == hashlib.sha256(raw).hexdigest()
    response, _ = http_event(pilot, inbound(18321, 'changed identity', fan=FAN), 'capacity-new')
    assert response.status_code == 202
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key == 'capacity-new')).status == 'quarantined'
        assert s.scalar(select(func.count()).select_from(OfEnginePilotWebhookDigest)) == 10001
    pilot.tick()
    assert world.fans.posts == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_conflicting_keys_between_two_digest_events_retain_a_hold(supervised):
    world, _, _, _, pilot = supervised
    for mid, body in [(18330,'outside first'),(18331,'outside changed')]:
        response, _ = http_event(pilot, inbound(mid, body, fan=999), 'outside-key-conflict')
        assert response.status_code == 202
    with Session(world.database[1]) as s:
        rows = list(s.scalars(select(OfEnginePilotWebhookDigest)))
        assert len(rows) == 2 and any(row.safety_hold for row in rows), 'digest-to-digest changed payload reuse was not fenced'
    stage(pilot,18332,'hello');pilot.tick()
    assert world.fans.posts == []


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_unprovable_model_cost_cannot_be_settled_by_administrator_attestation(supervised,api):
    import httpx
    from conversation_engine import operator_v2
    from db.onlyfans_engine_models import OfEngineModelCall
    from test_offline_completion_pg import normal_pilot
    from test_plan_c_scope_pg import ACTOR
    from test_onlyfans_text_delivery_pg import TENANT
    world, env, _, _, _=supervised
    def unknown():raise httpx.ReadTimeout('offline synthetic response lost after request')
    pilot,seen,_,_=normal_pilot(world,env,selecting=False,on_call=unknown)
    stage(pilot,18340,'hello')
    with Session(world.database[1]) as s:
        row=s.scalar(select(OfEngineModelCall))
        assert row.status=='unknown' and not row.usage_known and row.actual_microusd is None
        call_id,day,reserved=row.call_id,row.cost_day,row.reserved_microusd
    ledger=pilot.worker.engine.model.ledger
    before=ledger.committed(day=day,account=ACCOUNT)
    assert before>=reserved>0
    body={'commandKey':'c1'*32,'actor':ACTOR,'evidenceRef':'operator:fansapi:administrator guessed a zero bill'}
    with pytest.raises(operator_v2.NotFound):
        operator_v2.reconcile(world.store,TENANT,ACCOUNT,'model_call:'+call_id,body)
    with pytest.raises(operator_v2.BadRequest):
        operator_v2.reconcile(world.store,TENANT,ACCOUNT,'model_call:'+call_id,{**body,'actualMicrousd':0})
    for _ in range(4):pilot.tick()
    assert len(seen)==1 and world.fans.posts==[] and ledger.committed(day=day,account=ACCOUNT)==before
    with Session(world.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,call_id))
        assert row.status=='unknown' and not row.usage_known and row.actual_microusd is None
