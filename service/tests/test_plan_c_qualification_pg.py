"""Qualification regressions exercise the real pilot/authority with offline HTTP transports."""
import json
from datetime import timedelta
from pathlib import Path
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from conversation_engine import approval, pilot_scope
from conversation_engine.writer_context import CreatorProfile, StaticCreatorProfiles, set_active_profiles, active_profile
from db.models import CatalogItem, VaultItem, Transaction
from db.onlyfans_engine_models import OfEngineAccessEvidence, OfEngineApproval
from model_transport.pilot_supervised import build_supervised_pilot
from onlyfans_runtime.store import Refused, utcnow
from test_plan_c_supervised_pg import supervised, stage, approve
from test_plan_c_draft_app_pg import configured_draft, signed
from test_engine_real_worker_pg import world, inbound, unlock, tx
from test_onlyfans_foundation_pg import database
from test_onlyfans_text_delivery_pg import ACCOUNT, FAN, TENANT
from test_plan_c_scope_pg import ACTOR

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_durable_profile_is_not_overwritten_by_another_worker(supervised):
    world, env, model, seen, pilot = supervised
    set_active_profiles(StaticCreatorProfiles({ACCOUNT:CreatorProfile(name='Unrelated creator', limits=('different',))}))
    try:
        with Session(world.database[1]) as s:
            assert active_profile(ACCOUNT, session=s).name == 'Fixture creator'
        stage(pilot, 901)
        assert pilot.tick()['dispatched'] == ['accepted']
        assert len(world.fans.posts) == 1
    finally:
        set_active_profiles(None)

@pytest.mark.parametrize('replacement', [None, 'Other creator'])
def test_missing_or_different_profile_refuses_restart_before_any_io(supervised, replacement):
    world, env, model, seen, pilot = supervised
    path = Path(env['ENGINE_MODEL_PROFILES_FILE'])
    data = json.loads(path.read_text())
    if replacement is None: data.pop(ACCOUNT)
    else: data[ACCOUNT]['name'] = replacement
    path.write_text(json.dumps(data))
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed']
    with pytest.raises(Refused):
        build_supervised_pilot(world.store, env, model_transport=model, fans_transport=world.fans.transport)
    assert pilot_scope.describe(world.store, TENANT, ACCOUNT)['providerRequestsUsed'] == before
    assert seen == [] and world.fans.posts == []
    with pytest.raises(Refused): pilot.tick()

@pytest.mark.parametrize('bad', [b'{', b'{"payload":{"fromUser":{"id":"9223372036854775808"}}}',
    b'{"payload":{"fromUser":{"id":true}}}', b'{"payload":{"fromUser":{"id":"01"}}}',
    b'{"payload":{"fromUser":{"id":"101"}},"bad":"\\u0000"}',
    b'{"payload":{"fromUser":{"id":"101"}},"bad":"\\ud800"}'])
def test_malformed_sql_fan_parser_is_total_and_does_not_abort_transaction(supervised, bad):
    world, *_ = supervised
    with world.database[1].begin() as conn:
        assert conn.execute(text("SELECT of_engine_pilot_event_fan(:raw, 'messages.received')"), {'raw':bad}).scalar() is None
        assert conn.execute(text('SELECT 1')).scalar() == 1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_photo_kind_change_fails_closed_before_send(supervised):
    world, _, _, _, pilot = supervised
    stage(pilot, 902)
    config = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']
    with Session(world.database[1]) as s, s.begin():
        s.get(VaultItem, (ACCOUNT,config['product']['media_ids'][0])).kind = 'video'
    with pytest.raises(Refused): pilot.tick()
    assert world.fans.posts == []

def test_raw_newer_correction_blocks_dispatch_before_projection(supervised, monkeypatch):
    world, _, _, _, pilot = supervised
    draft = stage(pilot, 903)
    approve(world, draft)
    def crash(*args): raise RuntimeError('crash before projection')
    monkeypatch.setattr(pilot.worker.intake, '_project', crash)
    with pytest.raises(RuntimeError): stage(pilot, 904, 'stop, that is wrong')
    with Session(world.database[1]) as s:
        decision = s.scalar(select(__import__('db.onlyfans_engine_models',fromlist=['OfEngineDecision']).OfEngineDecision.decision_id))
    pilot.worker.send_approved(TENANT, ACCOUNT, decision)
    assert world.fans.posts == []

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_autonomous_photo_receipt_unlock_and_finance_remain_distinct(supervised):
    from conversation_engine.commerce import RealCatalogueCoverage, approve_catalogue_offer
    from db.onlyfans_fans_intake_models import OfFansEvidence
    world, env, _, _, pilot = supervised
    world.backfilled(FAN)
    pilot.worker.engine.select_simulated(TENANT, ACCOUNT, FAN)
    grant = approve_catalogue_offer(world.store, TENANT, ACCOUNT, FAN, world.item,
        RealCatalogueCoverage(ACCOUNT,FAN,world.item,'vault:offline-qualified',utcnow()+timedelta(minutes=30)))
    def respond(request):
        return httpx.Response(200,json={'model':'m/primary','choices':[{'message':{'content':json.dumps({
            'body':'Here is the photo', 'action':'offer','offer_grant_id':grant,'pitch':True})},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'cost':0.0001},
            'openrouter_metadata':{'endpoints':{'available':[{'provider':'Fixture Provider','selected':True}]}}})
    pilot = build_supervised_pilot(world.store,env,model_transport=httpx.MockTransport(respond),fans_transport=world.fans.transport)
    draft = stage(pilot,905,'I want to buy the photo')
    assert draft['outcome'] == 'awaiting_approval', draft
    assert pilot.tick()['dispatched'] == ['accepted']
    sent = world.fans.posts[0]['body']
    assert sent['price'] == 3.0 and sent['mediaFiles'] == ['11'] and sent['previews'] == []
    assert 'USD 3.00' in sent['text']
    with Session(world.database[1]) as s:
        assert list(s.scalars(select(OfEngineAccessEvidence))) == []
    for i,body in enumerate([unlock(7001,amount=3),unlock(7001,amount=3),tx('fixture-unlinked-transaction',amount=3)]):
        raw,headers = signed(body,key='paid-evidence-'+str(i))
        pilot.handle(raw,headers['Signature'],headers['X-OFAPI-Idempotency-Key'])
    with Session(world.database[1]) as s:
        access = list(s.scalars(select(OfEngineAccessEvidence)))
        assert len(access)==1 and access[0].status=='confirmed' and access[0].applied
        evidence = list(s.scalars(select(OfFansEvidence)))
        assert any(e.kind=='unlock' and e.status=='applied' for e in evidence)
        assert any(e.kind=='transaction' and e.status=='pending' and e.reason=='unlinked_transaction' for e in evidence)
        assert list(s.scalars(select(Transaction))) == []
    from conversation_engine.operator_v2 import list_receipts
    assert list_receipts(world.store,TENANT,ACCOUNT,ACTOR)[0]['messageId']=='7001'
    assert len(world.fans.posts)==1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_other_fan_quarantine_does_not_consult_legacy_policy(supervised, monkeypatch):
    from db.onlyfans_models import OfIngress
    from onlyfans_runtime.text_delivery import TextAuthority
    from test_engine_real_worker_pg import FAN2
    world, _, _, _, pilot = supervised
    stage(pilot,910,fan=FAN2)
    with Session(world.database[1]) as s, s.begin():
        row = s.scalar(select(OfIngress).where(OfIngress.account_id==ACCOUNT,OfIngress.fan_id==FAN2))
        row.status = 'quarantined'
    def legacy(*args,**kwargs): raise AssertionError('engine called legacy Moxy policy')
    monkeypatch.setattr(TextAuthority,'_verified_single_fan_pilot',legacy)
    stage(pilot,911,fan=FAN)
    pilot.tick()
    assert len(world.fans.posts)==1 and '/chats/'+str(FAN)+'/' in world.fans.posts[0]['path']

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_global_journey_identity_and_limits_cannot_change_pilot_context(supervised):
    from conversation_engine.journey import JourneyConfig, configure_journey, identity_line_for, scoped_config
    world, _, _, _, pilot = supervised
    configure_journey(JourneyConfig(identity_line='I am a human.',limit_terms=('hello',)))
    try:
        with Session(world.database[1]) as s:
            assert identity_line_for(ACCOUNT,s=s) is None
            assert scoped_config(s,ACCOUNT).limit_terms == ()
        stage(pilot,912,'hello')
        assert pilot.tick()['dispatched']==['accepted']
    finally: configure_journey(JourneyConfig())
