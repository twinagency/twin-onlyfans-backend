"""Independent normal-path regression probes; all external calls are fixtures."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from conversation_engine.journey import signals
from db.onlyfans_engine_models import OfEngineDecision, OfEngineApproval
from onlyfans_runtime.launch_history_memory.memory import HistoricalMemory
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised, stage
from test_plan_c_scope_pg import ACTOR
from test_onlyfans_text_delivery_pg import TENANT, ACCOUNT, FAN
from test_engine_real_worker_pg import FAN2
from test_plan_c_sales_journey_pg import install_coverage, writer, cards, prepare_scoped_history, stop_for_history, history_client, resume_after_history

@pytest.mark.parametrize('text', ['My travel agency booked the flights.', 'There was lots of chatter at work.'])
def test_unrelated_nouns_do_not_force_ai_identity(text):
    assert 'identity' not in signals(text)

@pytest.mark.parametrize('text', ['Are you an AI?', 'Are you a chatter?',
    'Do you work for an agency?', 'Are you using an agency?', 'Who runs this account?'])
def test_direct_identity_questions_still_require_truthful_identity(text):
    assert 'identity' in signals(text)

@pytest.mark.parametrize('text', ['too expensive', 'No, too expensive', "I think that's too expensive",
    'Your photo is too expensive', "I can't afford it", 'no thanks', 'stop selling to me'])
def test_direct_sales_refusals_remain_protected(text):
    assert 'refusal' in signals(text)

@pytest.mark.parametrize('text', ['What is your Telegram?', 'Can I email you?', 'Text me',
    'your phone number', 'Can we pay via PayPal?'])
def test_direct_offplatform_requests_still_detected(text):
    assert 'offplatform' in signals(text)

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_corrected_unsent_reply_recovers_without_another_fan_message(supervised):
    world, _, _, seen, pilot = supervised
    result=stage(pilot,17001,'hello there')
    assert result['outcome']=='awaiting_approval'
    memory=HistoricalMemory(world.store)
    memory.correct(TENANT,ACCOUNT,FAN,field='home_city',operation='set',value='Brisbane',
        expected_version=memory.read(TENANT,ACCOUNT,FAN)['version'],command_key='audit-correction',
        actor=ACTOR,reason='offline verified correction')
    for _ in range(4):pilot.tick()
    assert len(world.fans.posts)==1, 'valid unanswered request remained stranded after correction'
    assert len(seen)==2, 'old draft must be replaced, not reused'
    assert 'Brisbane' in str(seen[-1]), 'regeneration must use the corrected memory'
    with Session(world.database[1]) as s:
        decisions=list(s.scalars(select(OfEngineDecision).order_by(OfEngineDecision.generation)))
        assert len(decisions)==2 and decisions[0].status=='cancelled'
        assert decisions[1].generation>decisions[0].generation

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('block', ['takeover', 'contact_stop'])
def test_repairable_correction_does_not_bypass_later_control(supervised, block):
    from db.models import OfConversationState
    world, _, _, seen, pilot = supervised
    stage(pilot,17020,'hello there')
    memory=HistoricalMemory(world.store)
    memory.correct(TENANT,ACCOUNT,FAN,field='home_city',operation='set',value='Brisbane',
        expected_version=memory.read(TENANT,ACCOUNT,FAN)['version'],command_key='audit-correction-control',
        actor=ACTOR,reason='offline verified correction')
    if block == 'takeover':
        with Session(world.database[1]) as s:
            revision=s.get(OfConversationState,(ACCOUNT,FAN)).revision
        world.store.control(TENANT,ACCOUNT,FAN,mode='human_controlled',expected_revision=revision,
            command_key='audit-takeover',actor=ACTOR,reason='offline takeover',action='request_takeover')
    else:
        stage(pilot,17021,'stop messaging me')
    for _ in range(4):pilot.tick()
    assert world.fans.posts == [], 'recovery must not reopen contact or human takeover'

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_clear_renewed_request_with_actually_completes(supervised):
    world,env,_,_,_=supervised
    install_coverage(world);prepare_scoped_history(world)
    pilot,seen,_=writer(world,env)
    stage(pilot,17010,'no thanks');pilot.tick();world.no_throttle()
    stage(pilot,17011,'Actually, I want to buy the photo')
    pilot.tick()
    assert len(cards(world))==1, 'clear renewed buying request never reached approved offer'

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_bounded_history_preflight_unblocks_the_same_purchase_request(supervised):
    import httpx
    from datetime import datetime
    from fans_api_adapter.backfill import FansBackfill
    from conversation_engine import pilot_scope
    from test_engine_real_worker_pg import answer, unlock
    from test_plan_c_draft_app_pg import signed
    from db.onlyfans_engine_models import OfEngineOfferGrant, OfEngineAccessEvidence
    from db.onlyfans_fans_intake_models import OfFansBackfill
    world,env,_,_,_=supervised
    install_coverage(world)
    reads=[]
    def provider(request):
        if request.method == 'GET':
            reads.append(str(request.url))
            if '/transactions' in request.url.path:
                return answer(200,{'data':{'list':[],'hasMore':False}})
            if '/chargebacks' in request.url.path:
                return answer(200,{'data':{'list':[]}})
            if request.url.path.endswith('/messages'):
                return answer(200,{'data':[],'_pagination':{'next_page':None}})
        return world.fans.handle(request)
    world.fans.transport=httpx.MockTransport(provider)
    pilot,seen,_=writer(world,env)
    result=stage(pilot,17100,'I want to buy the photo')
    assert result['outcome']=='held' and result['reason']=='offer_history_coverage_missing'
    with Session(world.database[1]) as s:
        assert not list(s.scalars(select(OfEngineOfferGrant)))
        assert not list(s.scalars(select(OfFansBackfill)))
    config=pilot_scope.describe(world.store,TENANT,ACCOUNT)['config']
    pilot_scope.authorize(config,ACTOR,'configure')
    # Preparation only through the existing stopped scope and budgeted authorization (guarded fake provider).
    revision=stop_for_history(world)
    before=pilot_scope.describe(world.store,TENANT,ACCOUNT)['providerRequestsUsed']
    client,calls=history_client(world,revision)
    replay=FansBackfill(world.intake)
    first=replay.run(TENANT,ACCOUNT,client,reason='operator',
        start_date=datetime(2015,1,1),pages_budget=1,history_chats=(FAN,FAN2))
    assert first['status']=='budget_exhausted'
    from conversation_engine.journey import backfill_complete
    from onlyfans_runtime.store import Refused
    with Session(world.database[1]) as s:
        assert not backfill_complete(s,ACCOUNT,FAN),'a partial walk must not open the sale'
    with pytest.raises(Refused):pilot.tick()  # stopped scope: the worker itself refuses to act
    assert not world.fans.posts
    final=replay.run(TENANT,ACCOUNT,client,reason='operator',
        start_date=datetime(2015,1,1),pages_budget=3,history_chats=(FAN,FAN2))
    assert final['run']==first['run'] and final['status']=='complete'
    assert pilot_scope.describe(world.store,TENANT,ACCOUNT)['providerRequestsUsed']-before==len(calls)==5  # accounts, transactions, chargebacks and one chat per scoped fan
    assert all(method=='GET' for method,_ in calls)
    resume_after_history(world,revision)
    pilot,seen,_=writer(world,env)  # fresh worker for the re-activated scope; the original held request is recovered from the database
    for _ in range(4):pilot.tick()
    assert len(cards(world))==1
    assert cards(world)[0]['body']['price']==3.0
    raw,headers=signed(unlock(7001,amount=3),key='audit-history-unlock')
    pilot.handle(raw,headers['Signature'],headers['X-OFAPI-Idempotency-Key'])
    with Session(world.database[1]) as s:
        assert len(list(s.scalars(select(OfEngineAccessEvidence))))==1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
@pytest.mark.parametrize('request_text',['got any pics?','do you have any photos?','show me something'])
def test_normal_content_interest_can_reach_the_one_approved_offer(supervised,request_text):
    from db.onlyfans_engine_models import OfEngineOfferGrant
    world,env,_,_,_=supervised
    install_coverage(world);prepare_scoped_history(world)
    def choose(latest,proposal):
        with Session(world.database[1]) as s:
            grant=s.scalar(select(OfEngineOfferGrant))
            return {'body':'Here is the photo.','action':'offer','offer_grant_id':grant.grant_id,'pitch':True} if grant else proposal
    pilot,seen,_=writer(world,env,choose=choose)
    stage(pilot,17110,request_text);pilot.tick()
    assert 'APPROVED OFFER: id ' in seen[-1]['messages'][-1]['content']
    assert len(cards(world))==1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_queued_but_never_sent_reply_recovers_after_memory_correction(supervised):
    from conversation_engine import approval
    from db.onlyfans_delivery_models import OfTextIntent
    world,_,_,seen,pilot=supervised
    staged=stage(pilot,17120,'hello there')
    info=staged['approval']
    approval.authorize_automatic(world.store,TENANT,ACCOUNT,info['requestId'],info['generation'],
        expected_version=info['version'])
    intent=pilot.worker.engine.queue(TENANT,ACCOUNT,staged['decisionId'])
    memory=HistoricalMemory(world.store)
    memory.correct(TENANT,ACCOUNT,FAN,field='home_city',operation='set',value='Brisbane',
        expected_version=memory.read(TENANT,ACCOUNT,FAN)['version'],command_key='audit-queued-memory',
        actor=ACTOR,reason='offline correction after queue')
    for _ in range(5):pilot.tick()
    with Session(world.database[1]) as s:
        debug={'approvals':[(x.generation,x.status,x.invalidation_reason) for x in s.scalars(select(OfEngineApproval))],
            'decisions':[(x.generation,x.status,x.intent_id) for x in s.scalars(select(OfEngineDecision))],
            'intents':[(x.status,x.reason) for x in s.scalars(select(OfTextIntent))]}
    assert len(world.fans.posts)==1, debug
    assert len(seen)==2 and 'Brisbane' in str(seen[-1])
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent,(ACCOUNT,intent)).status=='cancelled'

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_expired_model_reservation_before_wire_is_recovered(supervised,monkeypatch):
    from datetime import timedelta
    from db.onlyfans_engine_models import OfEngineModelCall
    from onlyfans_runtime.store import utcnow
    world,_,_,seen,pilot=supervised
    ledger=pilot.worker.engine.model.ledger
    original=ledger.mark_calling
    def crash_before_wire(*args,**kwargs):raise RuntimeError('offline pre-wire crash')
    monkeypatch.setattr(ledger,'mark_calling',crash_before_wire)
    with pytest.raises(RuntimeError,match='offline pre-wire crash'):
        stage(pilot,17130,'hello there')
    assert not seen and not world.fans.posts
    monkeypatch.setattr(ledger,'mark_calling',original)
    with Session(world.database[1]) as s,s.begin():
        row=s.scalar(select(OfEngineModelCall))
        assert row.status=='reserved' and row.wire_started_at is None
        row.lease_expires_at=utcnow()-timedelta(seconds=1)
    for _ in range(4):pilot.tick()
    assert len(world.fans.posts)==1, 'an expired pre-wire reservation must not strand the fan'
    assert len(seen)==1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_expired_model_call_after_wire_marker_remains_unknown(supervised,monkeypatch):
    from datetime import timedelta
    from db.onlyfans_engine_models import OfEngineModelCall
    from onlyfans_runtime.store import utcnow
    world,_,_,seen,pilot=supervised
    ledger=pilot.worker.engine.model.ledger
    original=ledger.mark_calling
    def crash_after_marker(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('offline post-marker crash')
    monkeypatch.setattr(ledger,'mark_calling',crash_after_marker)
    with pytest.raises(RuntimeError,match='offline post-marker crash'):
        stage(pilot,17131,'hello there')
    monkeypatch.setattr(ledger,'mark_calling',original)
    with Session(world.database[1]) as s,s.begin():
        row=s.scalar(select(OfEngineModelCall))
        assert row.status=='calling' and row.wire_started_at is not None
        reserved=row.reserved_microusd
        row.lease_expires_at=utcnow()-timedelta(seconds=1)
    for _ in range(4):pilot.tick()
    assert not world.fans.posts and not seen
    with Session(world.database[1]) as s:
        rows=list(s.scalars(select(OfEngineModelCall)))
        assert len(rows)==1 and rows[0].status=='unknown'
        assert rows[0].reserved_microusd==reserved and rows[0].actual_microusd is None

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_parallel_recovery_does_not_duplicate_a_corrected_reply(supervised):
    from concurrent.futures import ThreadPoolExecutor
    from model_transport.pilot_supervised import build_supervised_pilot
    from onlyfans_runtime.store import Refused
    world,env,model,seen,pilot=supervised
    stage(pilot,17140,'hello there')
    memory=HistoricalMemory(world.store)
    memory.correct(TENANT,ACCOUNT,FAN,field='home_city',operation='set',value='Brisbane',
        expected_version=memory.read(TENANT,ACCOUNT,FAN)['version'],command_key='audit-parallel-correction',
        actor=ACTOR,reason='offline correction')
    second=build_supervised_pilot(world.store,env,model_transport=model,fans_transport=world.fans.transport)
    def tick(worker):
        try:return worker.tick()
        except Refused:return 'refused'
    for _ in range(4):
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(tick,(pilot,second)))
    assert len(world.fans.posts)==1
    from db.onlyfans_engine_models import OfEngineModelCall
    with Session(world.database[1]) as s:
        debug=[(x.generation,x.seq,x.status,x.outcome,x.snapshot_sha256,x.prompt_sha256) for x in s.scalars(select(OfEngineModelCall))]
    assert len(seen)==2 and 'Brisbane' in str(seen[-1]),debug

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_incidental_agency_survives_rendering_and_validation(supervised):
    world,_,_,seen,pilot=supervised
    result=stage(pilot,17200,'My travel agency booked the flights.')
    assert result['outcome']=='awaiting_approval',result
    assert 'He is asking what he is talking to:' not in seen[-1]['messages'][-1]['content']
    pilot.tick()
    assert len(world.fans.posts)==1

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_noncommercial_cost_complaint_does_not_pause_sales(supervised):
    from db.onlyfans_engine_models import OfEngineOwner
    world,_,_,_,pilot=supervised
    stage(pilot,17210,'The flight tickets are too expensive.')
    with Session(world.database[1]) as s:
        assert s.get(OfEngineOwner,(ACCOUNT,FAN)).sales_mode=='open'

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_incidental_email_history_does_not_force_human_takeover(supervised):
    from test_engine_real_worker_pg import inbound
    from test_plan_c_draft_app_pg import signed
    from db.onlyfans_engine_models import OfEngineOwner
    world,_,_,_,pilot=supervised
    for mid,body in [(17220,'My work email inbox was busy.'),(17221,'I deleted old email.')]:
        raw,headers=signed(inbound(mid,body),key='audit-incidental-'+str(mid))
        pilot.worker.intake.accept(raw,headers['Signature'],pilot.secret,headers['X-OFAPI-Idempotency-Key'],
            expected_tenant=TENANT,expected_account=ACCOUNT)
    result=stage(pilot,17222,'My work email is finally done.')
    assert result['outcome']=='awaiting_approval',result
    with Session(world.database[1]) as s:
        assert s.get(OfEngineOwner,(ACCOUNT,FAN)).mode=='active'

@pytest.mark.parametrize('supervised', ['autonomous'], indirect=True)
def test_queued_offer_correction_can_finish_without_reusing_sent_authority(supervised):
    from conversation_engine import approval
    from db.onlyfans_delivery_models import OfTextIntent
    world,env,_,_,_=supervised
    install_coverage(world);prepare_scoped_history(world)
    pilot,seen,_=writer(world,env)
    staged=stage(pilot,17230,'I want to buy the photo')
    info=staged['approval']
    approval.authorize_automatic(world.store,TENANT,ACCOUNT,info['requestId'],info['generation'],expected_version=info['version'])
    intent=pilot.worker.engine.queue(TENANT,ACCOUNT,staged['decisionId'])
    memory=HistoricalMemory(world.store)
    memory.correct(TENANT,ACCOUNT,FAN,field='home_city',operation='set',value='Brisbane',
        expected_version=memory.read(TENANT,ACCOUNT,FAN)['version'],command_key='audit-queued-offer-memory',
        actor=ACTOR,reason='offline correction after offer queue')
    for _ in range(5):pilot.tick()
    with Session(world.database[1]) as s:
        assert s.get(OfTextIntent,(ACCOUNT,intent)).status=='cancelled'
        steps=[(x.status,x.next_step) for x in s.scalars(select(__import__('db.onlyfans_engine_models',fromlist=['OfEngineRequest']).OfEngineRequest))]
    assert len(cards(world))==1,steps
    assert len(seen)==2
