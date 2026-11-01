import asyncio
from contextlib import asynccontextmanager
import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from test_onlyfans_foundation_pg import database
from test_profile_learning_pg import pg_store
from test_onlyfans_connections_pg import verified,TENANT
from db.models import AccountAiConfig,GrokCall,GrokDailyCost
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.profile_learning.evidence import validate,quality_fact,digest
from onlyfans_runtime.profile_learning.model import extract
from onlyfans_runtime.store import Refused


def facts(answer='Yes',question='Do you enjoy reading?',value=None):
    sources={k:dict(text=t,direction=d,at='2026-01-01',digest=digest(t),fan=1)
        for k,t,d in [('1:1',question,'out'),('1:2',answer,'in')]}
    proposal={'field':'interests','value':answer if value is None else value,'source':'1:2','quote':answer,
        'context_sources':['1:1'],'context_quotes':[question]}
    return validate({'facts':[proposal]},sources)


@pytest.mark.parametrize('answer,value',[('Yes','Enjoys reading'),('No','Does not enjoy reading')])
def test_meaning_retains_both_exact_evidence_messages(answer,value):
    f=facts(answer)[0]
    assert f['value']==value and f['quote']==answer
    assert f['context_evidence'][0]['quote']=='Do you enjoy reading?'


def test_normalized_proposal_requires_matching_context_and_polarity():
    assert facts(value='Enjoys reading')
    assert not facts(answer='No',value='Enjoys reading')
    assert not facts(value='Enjoys swimming')
    assert facts(question='Do you enjoy reading or swimming?')[0]['value']=='Yes'
    assert facts(question="Do you not enjoy reading?")[0]['value']=='Yes'


def test_temporary_work_and_joke_names_preserve_manual_authority():
    f={'field':'occupation','value':'Just at work','quote':'Just at work','at':'2026-01-01'}
    assert quality_fact(f)['field']=='recent_topic'
    f={'field':'name','value':'Bond James Bond'}
    assert quality_fact(f) is None
    assert quality_fact({**f,'manual':True})['value']=='Bond James Bond'


@pytest.mark.parametrize('outcome,expected_status,expected_cost',[
    ('success','done',7),('incomplete','done',7),('bad_json','done',7),
    ('http_error','unknown',None),('reported_zero','done',0),('missing_usage','unknown',None),
    ('timeout','unknown',None),('pre_dispatch','done',0)])
def test_extractor_audits_real_failure_and_retains_or_settles_money(database,monkeypatch,outcome,expected_status,expected_cost):
    import importlib,httpx
    from llm_models import MODELS
    from onlyfans_runtime.profile_learning import model
    modelid=next(k for k,v in MODELS.items() if v.provider=='openrouter')
    store,account,_=verified(database,monkeypatch,model={'modelId':modelid,'apiKey':'synthetic-model-secret'})
    with Session(store.engine) as s,s.begin():
        cfg=s.get(AccountAiConfig,account);cfg.model=modelid;cfg.daily_cost_cap_cents=600
        binding=s.get(OfRuntime,account).binding_id
    original_client=httpx.AsyncClient
    def respond(request):
        if outcome=='timeout': raise httpx.ReadTimeout('synthetic timeout')
        body={'id':'gen-synthetic-123','usage':{'prompt_tokens':100,'completion_tokens':10,'cost':.0007},
              'choices':[{'finish_reason':'length' if outcome=='incomplete' else 'stop',
                'message':{'content':'{' if outcome=='bad_json' else '{"facts":[]}'}}]}
        status=200
        if outcome in {'http_error','reported_zero'}:
            status=429;body={'error':{'code':429,'message':'synthetic rate limit'}}
            if outcome=='reported_zero':body['usage']={'cost':0}
        if outcome=='missing_usage':body.pop('usage')
        return httpx.Response(status,json=body,headers={'x-request-id':'req-synthetic'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original_client(**kw,transport=httpx.MockTransport(respond)))
    async def run():
        ae=create_async_engine(store.engine.url.set(drivername='postgresql+asyncpg'))
        factory=async_sessionmaker(ae,expire_on_commit=False)
        @asynccontextmanager
        async def sessions():
            async with factory() as s:
                try:yield s;await s.commit()
                except BaseException:await s.rollback();raise
        monkeypatch.setattr(importlib.import_module('db.engine'),'get_session',sessions)
        if outcome=='pre_dispatch':
            original=model.DailyBudget.reserve
            async def change(*a,**kw):
                r=await original(*a,**kw)
                from db.onlyfans_connection_models import OfProviderConnection
                with Session(store.engine) as s,s.begin():s.get(OfProviderConnection,account).revision+=1
                return r
            monkeypatch.setattr(model.DailyBudget,'reserve',change)
        try:
            if outcome=='success':await extract(store,TENANT,account,binding,1,{'1:1':{'fan':1,'text':'hello'}})
            else:
                with pytest.raises((Refused,json.JSONDecodeError,httpx.ReadTimeout)):
                    await extract(store,TENANT,account,binding,1,{'1:1':{'fan':1,'text':'hello'}})
        finally:await ae.dispose()
    asyncio.run(run())
    with Session(store.engine) as s:
        call=s.scalar(select(GrokCall));ledger=s.scalar(select(GrokDailyCost))
        assert call.status==expected_status and ledger.cost_cents==call.cost_cents
        if expected_cost is None:assert call.cost_cents>0
        else:assert call.cost_cents==expected_cost
        audit=json.loads(call.response_text)
        assert call.latency_ms is not None
        if outcome!='success':assert call.error_text
        if outcome in {'success','incomplete','bad_json','missing_usage'}:
            assert audit['generation_id']=='gen-synthetic-123'
        if outcome in {'http_error','reported_zero'}:assert audit['http_status']==429
        if outcome=='http_error':assert audit['billing']=='unreconciled'
    store.close()


def test_distinct_yes_answers_keep_distinct_memory_slots():
    from onlyfans_runtime.profile_learning.evidence import merge
    reading=facts()[0];swimming=facts(question='Do you enjoy swimming?')[0]
    swimming['source']='1:3'
    assert len(merge([],[reading,swimming]))==2


@pytest.mark.parametrize('returned_id,expected_units',[('gen-reconcile',7),('wrong-generation',100)])
def test_reconciliation_requires_matching_provider_identity_and_is_idempotent(database,monkeypatch,returned_id,expected_units):
    from datetime import datetime
    import httpx
    from llm_models import MODELS
    from onlyfans_runtime.profile_learning.accounting import reconcile_known,report
    modelid=next(k for k,v in MODELS.items() if v.provider=='openrouter')
    store,account,_=verified(database,monkeypatch,model={'modelId':modelid,'apiKey':'synthetic-model-secret'})
    audit={'schema':'profile-call-audit.v1','generation_id':'gen-reconcile','billing':'unreconciled'}
    with Session(store.engine) as s,s.begin():
        s.add(GrokCall(account_id=account,fan_id=1,model=modelid,provider='openrouter',purpose='gen_info',
            endpoint='https://openrouter.ai/api/v1/chat/completions',prompt_json='{}',status='unknown',
            cost_cents=100,called_at=datetime(2026,11,1),response_text=json.dumps(audit),error_text='original timeout'))
        s.add(GrokDailyCost(account_id=account,provider='openrouter',day='2026-11-01',cost_cents=100,call_count=1,is_capped=False))
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(**kw,transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'data':{'id':returned_id,'total_cost':.0007}}))))
    assert reconcile_known(store,'other-tenant')==[]
    reconcile_known(store,TENANT);reconcile_known(store,TENANT)
    with Session(store.engine) as s:
        call=s.scalar(select(GrokCall));ledger=s.scalar(select(GrokDailyCost))
        assert call.cost_cents==ledger.cost_cents==expected_units
        assert ledger.day=='2026-11-01' and ledger.call_count==1
        assert call.error_text=='original timeout'
    buckets=report(store,TENANT,'2026-11-01')[0]['buckets']
    assert ('provider_reported' if returned_id=='gen-reconcile' else 'unreconciled_reservation') in buckets
    store.close()


def test_existing_repair_preserves_receipts_budget_and_manual_notes(pg_store):
    from test_profile_learning_pg import run
    from test_profile_learning_queue import add_message,fan_due,state
    from onlyfans_runtime.profile_learning.repair import repair_existing
    from sqlalchemy import text
    add_message(pg_store,body='Just at work');fan_due(pg_store)
    async def extract_work(*a,**kw):
        return {'facts':[{'field':'occupation','value':'Just at work','quote':'Just at work','source':next(k for k,v in a[5].items() if not v.get('context_only'))}]},None
    run(pg_store,extract_work)
    # Represent a legacy observation so the repair really has something to rebuild.
    with pg_store.engine.begin() as c:
        raw=c.scalar(text('SELECT document_json FROM of_profile_observations'))
        legacy=json.loads(raw);legacy.update(field='occupation',kind='changing',key='Just at work')
        c.execute(text('UPDATE of_profile_observations SET document_json=:d'),{'d':json.dumps(legacy)})
        receipts=c.scalar(text('SELECT count(*) FROM of_profile_receipts'))
        before=c.execute(text('SELECT next_at,error_code,attempts,processed_messages FROM of_profile_learning WHERE fan_id=1')).first()
    result=repair_existing(pg_store,'tenant')
    assert len(result['repaired'])==1
    assert state(pg_store)['document']['facts'][0]['field']=='recent_topic'
    with pg_store.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM of_profile_receipts'))==receipts
        assert c.execute(text('SELECT next_at,error_code,attempts,processed_messages FROM of_profile_learning WHERE fan_id=1')).first()==before


@pytest.mark.parametrize('value',['Yes at work',"I'm working right now",'Currently working','Yeah just at work'])
def test_temporary_work_variants_never_claim_an_occupation(value):
    f={'field':'occupation','value':value,'quote':value,'at':'2026-01-01'}
    assert quality_fact(f)['field']=='recent_topic'


def test_concrete_occupation_is_preserved():
    f={'field':'occupation','value':'electrician','quote':"I work as an electrician",'at':'2026-01-01'}
    assert quality_fact(f)['field']=='occupation'
