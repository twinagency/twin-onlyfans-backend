"""Manual provider-correlated billing against the actual runtime/ledger; every HTTP request is mocked."""
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading
import httpx
import pytest
from alembic import command
from sqlalchemy import select,text,func
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from conversation_engine import pilot_scope
from db.schema import HEAD,_config,check_schema,upgrade
from db.onlyfans_engine_models import OfEngineModelCall
from model_transport.pilot_supervised import build_supervised_pilot
from model_transport.reconciliation import reconcile_model_usage
from onlyfans_runtime.launch_operations_models import OfLaunchCommand
from onlyfans_runtime.store import Refused
from test_onlyfans_foundation_pg import database
from test_engine_real_worker_pg import world
from test_plan_c_draft_app_pg import configured_draft
from test_plan_c_supervised_pg import supervised,stage
from test_plan_c_scope_pg import ACTOR,OTHER
from test_onlyfans_text_delivery_pg import TENANT,ACCOUNT,FAN
from test_offline_completion_pg import normal_pilot


def billing_pilot(supervised, *, generation='gen-offline-one', metadata=None, lookup=None, successful=False):
    world,env,_,_,_=supervised
    normal_pilot(world,env,selecting=False)  # approved setup and actual guarded complete history, no seeded call/grant
    posts,reads=[],[]
    def wire(request):
        if request.method=='POST' and request.url.path.endswith('/chat/completions'):
            posts.append(request)
            body={'error':{'message':'synthetic error after provider created a generation'}}
            if successful:
                body={'model':'m/primary','choices':[{'message':{'content':json.dumps({'body':'How was your day?'})},
                    'finish_reason':'stop'}],'openrouter_metadata':{'endpoints':{'available':[
                        {'provider':'Fixture Provider','selected':True}]}}}
            if generation is not None:body['id']=generation
            return httpx.Response(200,json=body)
        if request.method=='GET' and request.url.path.endswith('/generation'):
            reads.append(request)
            assert request.url.params['id']==generation
            assert request.headers.get('authorization','').startswith('Bearer ')
            if lookup:lookup()
            body={'data':{'id':generation,'model':'m/primary','total_cost':0.0001,
                'finish_reason':'error','provider_name':'Fixture Provider','created_at':'2026-11-08T13:00:00Z'}}
            if metadata is not None:body=metadata
            return httpx.Response(200,json=body)
        raise AssertionError('unexpected external operation '+request.method+' '+request.url.path)
    pilot=build_supervised_pilot(world.store,env,model_transport=httpx.MockTransport(wire),fans_transport=world.fans.transport)
    stage(pilot,18500,'hello')
    with Session(world.database[1]) as s:
        row=s.scalar(select(OfEngineModelCall))
        assert row.status=='unknown' and row.outcome==('verified_ok' if successful else 'provider_incomplete') and not row.usage_known
        call_id,day,reserved=row.call_id,row.cost_day,row.reserved_microusd
        assert row.provider_generation_id==generation
    args=dict(tenant=TENANT,account=ACCOUNT,call_id=call_id,actor=ACTOR,command_key='d1'*32,
        expected_pilot_revision=pilot_scope.describe(world.store,TENANT,ACCOUNT)['revision'],
        reason='offline exact provider billing review',environ=env)
    return world,pilot,args,posts,reads,day,reserved


def run(pilot,args):
    runtime=pilot.worker.engine.model
    return reconcile_model_usage(pilot.worker.engine.store,runtime.config,runtime.client,**args)


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
@pytest.mark.parametrize('zero',[False,True])
def test_verified_provider_cost_is_audited_once_without_releasing_unknown_outcome(supervised,zero):
    metadata=None if not zero else {'data':{'id':'gen-offline-one','model':'m/primary','total_cost':0,'finish_reason':'error'}}
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,metadata=metadata)
    ledger=pilot.worker.engine.model.ledger
    assert ledger.committed(day=day,account=ACCOUNT)==reserved>0
    result=run(pilot,args)
    assert result['actualMicrousd']==(0 if zero else 100) and result['outcomeStillUnknown']
    assert not result['retryAuthorized'] and not result['sendAuthorized'] and len(reads)==1
    assert run(pilot,args)['replayed'] and len(reads)==1
    with pytest.raises(Refused,match='key conflict'):run(pilot,{**args,'reason':'changed command'})
    for _ in range(5):pilot.tick()
    assert len(posts)==1 and world.fans.posts==[]
    assert ledger.committed(day=day,account=ACCOUNT)==(0 if zero else 100)
    with Session(world.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,args['call_id']))
        assert row.status=='unknown' and row.outcome=='provider_incomplete' and row.usage_known
        assert row.actual_microusd==(0 if zero else 100) and row.reserved_microusd==reserved
        audit=s.scalar(select(OfLaunchCommand).where(OfLaunchCommand.kind=='plan_c_model_cost_reconcile'))
        evidence=json.loads(audit.before_json)['providerEvidence']
        assert evidence['generationId']==row.provider_generation_id and evidence['model']==row.model
        assert audit.actor==ACTOR and evidence['receiptSha256']==result['providerReceiptSha256']
        assert s.scalar(select(func.count()).select_from(OfLaunchCommand).where(
            OfLaunchCommand.kind=='plan_c_model_cost_reconcile'))==1
    with world.database[1].begin() as c:
        for sql in ["UPDATE of_engine_model_call SET provider_generation_id='another-generation'",
                    "UPDATE of_engine_model_call SET provider_endpoint_sha256='"+'0'*64+"'",
                    "UPDATE of_engine_model_call SET actual_microusd=1",
                    "UPDATE of_engine_model_call SET usage_known=false"]:
            with pytest.raises(DBAPIError):
                with c.begin_nested():c.execute(text(sql))
        with pytest.raises(RuntimeError,match='provider model-call evidence'):
            with c.begin_nested():command.downgrade(_config(c),'0127_offer_grant_lineage')


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
@pytest.mark.parametrize('data',[{'id':'wrong-id','model':'m/primary','total_cost':0,'finish_reason':'error'},
    {'id':'gen-offline-one','model':'wrong-model','total_cost':0,'finish_reason':'error'},
    {'id':'gen-offline-one','model':'m/primary','finish_reason':'error'},
    {'id':'gen-offline-one','model':'m/primary','total_cost':True,'finish_reason':'error'},
    {'id':'gen-offline-one','model':'m/primary','total_cost':-1,'finish_reason':'error'},
    {'id':'gen-offline-one','model':'m/primary','total_cost':0,'finish_reason':None}])
def test_uncorrelated_or_incomplete_provider_billing_keeps_reservation(supervised,data):
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,metadata={'data':data})
    with pytest.raises(Refused):run(pilot,args)
    assert len(reads)==1 and pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==reserved
    with Session(world.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,args['call_id']))
        assert row.status=='unknown' and not row.usage_known and row.actual_microusd is None
        assert not list(s.scalars(select(OfLaunchCommand).where(OfLaunchCommand.kind=='plan_c_model_cost_reconcile')))


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_missing_reference_and_unauthorized_billing_never_lookup_or_guess_cost(supervised):
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,generation=None)
    for patch in [{},{'actor':OTHER},{'environ':{'ONLYFANS_PROCESS_ROLE':'api'}},
                  {'expected_pilot_revision':args['expected_pilot_revision']+1},{'account':'other-account'}]:
        with pytest.raises(Refused):run(pilot,{**args,**patch})
    assert reads==[] and len(posts)==1 and pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==reserved
    # Real populated 0127-to-0128 migration keeps an unprovable old call and all liability unchanged.
    with world.database[1].begin() as c:
        before=c.execute(text('SELECT status,outcome,reserved_microusd,actual_microusd,usage_known FROM of_engine_model_call')).one()
        command.downgrade(_config(c),'0127_offer_grant_lineage')
        check_schema(c,expected='0127_offer_grant_lineage')
        upgrade(c);check_schema(c)
        assert c.scalar(text('SELECT version_num FROM alembic_version'))==HEAD=='0128_model_billing_evidence'
        assert c.execute(text('SELECT status,outcome,reserved_microusd,actual_microusd,usage_known FROM of_engine_model_call')).one()==before
        assert c.scalar(text('SELECT provider_generation_id FROM of_engine_model_call')) is None
    with pytest.raises(Refused):run(pilot,args)
    assert reads==[]


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_concurrent_same_billing_command_settles_once_without_model_retry(supervised):
    barrier=threading.Barrier(2)
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,lookup=lambda:barrier.wait(timeout=10))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:run(pilot,args),range(2)))
    assert sorted(row['replayed'] for row in results)==[False,True]
    assert len(posts)==1 and len(reads)==2
    assert pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==100
    for _ in range(3):pilot.tick()
    assert len(posts)==1 and world.fans.posts==[]


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_scope_change_during_billing_read_applies_no_stale_settlement(supervised):
    state={}
    def stop():
        if not state.get('stopped'):
            state['stopped']=pilot_scope.set_status(state['world'].store,TENANT,ACCOUNT,'stopped',
                expected_revision=state['args']['expected_pilot_revision'],command_key='d2'*32,
                actor=ACTOR,reason='offline stop during billing review')
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,lookup=stop)
    state.update(world=world,args=args)
    with pytest.raises(Refused,match='revision changed'):run(pilot,args)
    assert pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==reserved
    result=run(pilot,{**args,'command_key':'d3'*32,'expected_pilot_revision':state['stopped']['revision']})
    assert result['outcomeStillUnknown'] and result['actualMicrousd']==100
    assert world.fans.posts==[] and len(posts)==1


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_late_completion_cannot_overwrite_verified_cost_or_release_unknown_outcome(supervised):
    from conversation_engine.model_client import Usage
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised)
    assert run(pilot,args)['actualMicrousd']==100
    with Session(world.database[1]) as s:worker=s.get(OfEngineModelCall,(ACCOUNT,args['call_id'])).lease_owner
    with pytest.raises(Refused):
        pilot.worker.engine.model.ledger.settle(ACCOUNT,args['call_id'],worker,
            text_value='{"body":"late output"}',usage=Usage(cost_microusd=0))
    with Session(world.database[1]) as s:
        row=s.get(OfEngineModelCall,(ACCOUNT,args['call_id']))
        assert row.actual_microusd==100 and row.status=='unknown' and row.outcome=='provider_incomplete'
    assert pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==100


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_verified_cost_for_already_verified_output_creates_no_new_send_authority(supervised):
    from db.onlyfans_engine_models import OfEngineApproval,OfEngineDecision
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,successful=True)
    result=run(pilot,args)
    assert not result['outcomeStillUnknown'] and not result['sendAuthorized'] and world.fans.posts==[]
    with Session(world.database[1]) as s:
        assert s.scalar(select(OfEngineModelCall)).status=='settled'
        assert s.scalar(select(OfEngineApproval)).status=='awaiting'
        assert s.scalar(select(OfEngineDecision)).status=='ready'
    pilot.tick()
    assert len(world.fans.posts)==1 and len(posts)==1


@pytest.mark.parametrize('supervised',['autonomous'],indirect=True)
def test_provider_reported_over_bound_cost_remains_accounted_and_blocks_new_model_spend(supervised):
    metadata={'data':{'id':'gen-offline-one','model':'m/primary','total_cost':1000001,
        'finish_reason':'error'}}
    world,pilot,args,posts,reads,day,reserved=billing_pilot(supervised,metadata=metadata)
    result=run(pilot,args)
    assert result['actualMicrousd']==1000001000000
    assert pilot.worker.engine.model.ledger.committed(day=day,account=ACCOUNT)==1000001000000
    with Session(world.database[1]) as s:assert s.scalar(select(OfEngineModelCall)).error_class=='cost_over_bound'
    stage(pilot,18501,'hello from the second fan',fan=102)
    assert len(posts)==1 and world.fans.posts==[]


def test_manual_billing_cli_has_no_attested_cost_input_and_refuses_api_role(monkeypatch):
    from tools import plan_c_reconcile_model as tool
    args=['--tenant',TENANT,'--account',ACCOUNT,'--call-id','00000000-0000-0000-0000-000000000000',
        '--actor',ACTOR,'--command-key','d4'*32,'--reason','offline CLI guard','--expected-pilot-revision','1']
    with pytest.raises(SystemExit) as bad:tool.main(args+['--actual-cost','0'])
    assert bad.value.code==2
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','api')
    def forbidden(*a,**k):raise AssertionError('API role attempted to construct worker credential client')
    monkeypatch.setattr(tool,'OpenRouterClient',forbidden)
    with pytest.raises(Refused,match='worker role'):tool.main(args)
