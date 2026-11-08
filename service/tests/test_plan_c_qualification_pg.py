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
def test_photo_kind_change_does_not_block_plain_chat(supervised):
    world, _, _, _, pilot = supervised
    stage(pilot, 902)
    config = pilot_scope.describe(world.store, TENANT, ACCOUNT)['config']
    with Session(world.database[1]) as s, s.begin():
        s.get(VaultItem, (ACCOUNT,config['product']['media_ids'][0])).kind = 'video'
    assert pilot.tick()['dispatched'] == ['accepted']
    assert world.fans.posts[0]['body'].get('price',0)==0

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
@pytest.mark.parametrize('change_kind',[False,True])
def test_autonomous_photo_receipt_unlock_and_finance_remain_distinct(supervised,change_kind):
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
    if change_kind:
        with Session(world.database[1]) as s,s.begin():s.get(VaultItem,(ACCOUNT,11)).kind='video'
        assert pilot.tick()['dispatched']==[] and world.fans.posts==[]
        return
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

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_stopped_pilot_retains_incoming_evidence_without_model_or_sending(supervised):
    from db.onlyfans_fans_intake_models import OfFansProjection
    world, _, _, seen, pilot=supervised
    state=pilot_scope.describe(world.store,TENANT,ACCOUNT)
    pilot_scope.set_status(world.store,TENANT,ACCOUNT,'stopped',expected_revision=state['revision'],
        command_key='e'*64,actor=ACTOR,reason='offline stop')
    raw,headers=signed(inbound(920,'please correct this'),key='stopped-inbound')
    result=pilot.handle(raw,headers['Signature'],headers['X-OFAPI-Idempotency-Key'])
    assert result['planning']=='held'
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansProjection).where(OfFansProjection.event_key=='stopped-inbound')).status=='projected'
    assert seen==[] and world.fans.posts==[]
    with pytest.raises(Refused):pilot.tick()

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_duplicate_json_keys_are_retained_as_a_safety_hold(supervised):
    import hashlib,hmac
    from test_engine_real_worker_pg import SECRET
    from db.onlyfans_engine_models import OfEnginePilotWebhookDigest
    world,_,_,_,pilot=supervised
    stage(pilot,921)
    raw=b'{"event":"messages.received","account_id":"acct_real_worker","payload":{"fromUser":{"id":101},"fromUser":{"id":102}}}'
    signature=hmac.new(SECRET,raw,hashlib.sha256).hexdigest()
    pilot.handle(raw,signature,'ambiguous-json')
    pilot.tick()
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfEnginePilotWebhookDigest).where(OfEnginePilotWebhookDigest.event_key=='ambiguous-json')).safety_hold
    assert world.fans.posts==[]

def test_unsupported_manual_path_refuses_before_creating_a_false_unknown(supervised):
    from db.models import OfConversationState
    from db.onlyfans_delivery_models import OfTextIntent
    world,*_=supervised
    with Session(world.database[1]) as s:revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
    with pytest.raises(Refused,match='manual transport is not qualified'):
        world.authority.prepare(TENANT,ACCOUNT,FAN,command_key='unsupported-manual',expected_revision=revision,
            body='Offline operator text',origin='manual',actor=ACTOR)
    with Session(world.database[1]) as s:assert list(s.scalars(select(OfTextIntent)))==[]
    assert world.fans.posts==[]

@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_paused_restart_retains_contact_stop_and_blocks_later_reply(supervised):
    from db.models import OfConversationState
    from model_transport.pilot_supervised import IntakeOnlyPilot
    world,env,model,seen,pilot=supervised
    before=pilot_scope.describe(world.store,TENANT,ACCOUNT)
    stopped=pilot_scope.set_status(world.store,TENANT,ACCOUNT,'stopped',expected_revision=before['revision'],
        command_key='b'*64,actor=ACTOR,reason='offline stop')
    paused=build_supervised_pilot(world.store,env,model_transport=None,fans_transport=None)
    assert type(paused) is IntakeOnlyPilot
    raw,headers=signed(inbound(931,'stop messaging me'),key='paused-contact-stop')
    assert paused.handle(raw,headers['Signature'],headers['X-OFAPI-Idempotency-Key'])['planning']=='held'
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
    assert seen==[] and world.fans.posts==[]
    pilot_scope.set_status(world.store,TENANT,ACCOUNT,'active',expected_revision=stopped['revision'],
        command_key='c'*64,actor=ACTOR,reason='offline activation')
    resumed=build_supervised_pilot(world.store,env,model_transport=model,fans_transport=world.fans.transport)
    stage(resumed,932,'hello again')
    resumed.tick()
    with Session(world.database[1]) as s:assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
    assert seen==[] and world.fans.posts==[]


def test_intake_itself_enforces_current_durable_fans(supervised):
    from test_engine_real_worker_pg import SECRET
    from db.onlyfans_engine_models import OfFansWebhook
    world,_,_,_,pilot=supervised
    raw,headers=signed(inbound(933,'outside current scope',fan=999),key='direct-intake-outside')
    with pytest.raises(Refused,match='pilot fan scope changed'):
        pilot.worker.intake.accept(raw,headers['Signature'],SECRET,'direct-intake-outside',
            expected_tenant=TENANT,expected_account=ACCOUNT)
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfFansWebhook).where(OfFansWebhook.event_key=='direct-intake-outside')) is None
    assert world.fans.posts==[]


def _wait_readiness(client, predicate):
    import time
    end = time.monotonic() + 8
    while time.monotonic() < end:
        response = client.get('/readyz')
        if predicate(response):
            return response
        time.sleep(.05)
    raise AssertionError(response.json())


def test_worker_readiness_reports_loop_failure_and_recovery(supervised, monkeypatch):
    from fastapi.testclient import TestClient
    from plan_c_worker import create_app
    _, _, _, seen, pilot = supervised
    fail = [True]
    def tick(self):
        if fail[0]:
            raise RuntimeError('private exception content must stay private')
    monkeypatch.setattr(type(pilot), 'tick', tick)
    with TestClient(create_app(pilot)) as client:
        response = _wait_readiness(client, lambda r: r.json()['code']=='loop_failed')
        assert response.status_code == 503
        assert response.json()['consecutiveFailures'] >= 1
        assert 'private exception' not in response.text
        assert client.get('/healthz').status_code == 200
        fail[0] = False
        response = _wait_readiness(client, lambda r: r.json()['code']=='ready')
        assert response.status_code == 200 and response.json()['sendingReady']
        assert response.json()['failures'] >= 1
    assert seen == []


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_intake_only_readiness_requires_restart_after_activation(supervised):
    from fastapi.testclient import TestClient
    from plan_c_worker import create_app
    world, env, _, seen, _ = supervised
    before = pilot_scope.describe(world.store, TENANT, ACCOUNT)
    stopped = pilot_scope.set_status(world.store,TENANT,ACCOUNT,'stopped',expected_revision=before['revision'],
        command_key='d'*64,actor=ACTOR,reason='offline stop for readiness')
    paused = build_supervised_pilot(world.store, env)
    with TestClient(create_app(paused)) as client:
        response = _wait_readiness(client, lambda r: r.json()['code']=='ready')
        assert response.json()['mode']=='intake_only' and not response.json()['sendingReady']
        pilot_scope.set_status(world.store,TENANT,ACCOUNT,'active',expected_revision=stopped['revision'],
            command_key='f'*64,actor=ACTOR,reason='offline activation for readiness')
        response = client.get('/readyz')
        assert response.status_code==503 and response.json()['code']=='restart_required'
        assert not response.json()['sendingReady']
    assert seen==[] and world.fans.posts==[]


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_contact_review_is_bounded_and_does_not_skip_late_rows(supervised):
    from conversation_engine.pilot_ingress import preserve_contact_stops
    from test_onlyfans_foundation_pg import event
    from db.models import OfConversationState
    from onlyfans_runtime.launch_operations_models import OfLaunchCommand
    world, _, _, seen, _ = supervised
    for mid, body in [(940,'hello'),(941,'another message'),(942,'stop messaging me'),(943,'later chat')]:
        world.store.record(TENANT,ACCOUNT,event(mid=mid,fan=FAN,body=body))
    world.store.drain(TENANT,ACCOUNT)
    assert preserve_contact_stops(world.store,TENANT,ACCOUNT,limit=2)=={'reviewed':2,'pending':True}
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode!='no_contact'
    assert preserve_contact_stops(world.store,TENANT,ACCOUNT,limit=2)=={'reviewed':2,'pending':False}
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
        assert len(list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=='plan_c_ingress_review'))))==4
    # A lower message identity arriving after a completed batch is still reviewed.
    world.store.record(TENANT,ACCOUNT,event(mid=939,fan=FAN,body='a late message'))
    world.store.drain(TENANT,ACCOUNT)
    assert preserve_contact_stops(world.store,TENANT,ACCOUNT,limit=2)=={'reviewed':1,'pending':False}
    assert preserve_contact_stops(world.store,TENANT,ACCOUNT,limit=2)=={'reviewed':0,'pending':False}
    assert seen==[] and world.fans.posts==[]


@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_contact_review_replays_a_committed_stop_after_a_crash(supervised, monkeypatch):
    from conversation_engine import pilot_ingress
    from test_onlyfans_foundation_pg import event
    from db.models import OfConversationState
    from db.onlyfans_models import OfControlAudit
    world, _, _, seen, _ = supervised
    world.store.record(TENANT,ACCOUNT,event(mid=950,fan=FAN,body='please stop messaging me'))
    world.store.drain(TENANT,ACCOUNT)
    record=pilot_ingress._record_review
    def crash(*args):raise RuntimeError('crash after control before review receipt')
    monkeypatch.setattr(pilot_ingress,'_record_review',crash)
    with pytest.raises(RuntimeError,match='crash after control'):
        pilot_ingress.preserve_contact_stops(world.store,TENANT,ACCOUNT)
    with Session(world.database[1]) as s:
        revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
        assert s.get(OfConversationState,(ACCOUNT,FAN)).mode=='no_contact'
    monkeypatch.setattr(pilot_ingress,'_record_review',record)
    assert pilot_ingress.preserve_contact_stops(world.store,TENANT,ACCOUNT)=={'reviewed':1,'pending':False}
    with Session(world.database[1]) as s:
        assert s.get(OfConversationState,(ACCOUNT,FAN)).revision==revision
        assert len(list(s.scalars(select(OfControlAudit).where(OfControlAudit.command_key.like('engine-stop:%')))))==1
    assert seen==[] and world.fans.posts==[]
