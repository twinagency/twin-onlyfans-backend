"""Real PostgreSQL locking, rollback, evidence and first-budget-reservation tests.

Requires ONLYFANS_TEST_PG_URL pointing at a disposable local test server.
No OnlyFans messages or paid model requests are sent.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timedelta
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker

from test_onlyfans_foundation_pg import database
from test_profile_learning_queue import add_message,fan_due,extractor,state
from db.models import Account,AccountAiConfig,Fan,FanProfile,Message,OfCreatorBinding,GrokCall,GrokDailyCost
from db.onlyfans_models import OfRuntime
from onlyfans_runtime.store import Store
from onlyfans_runtime.profile_learning import worker
from onlyfans_runtime.profile_learning.storage import load_memories,reply_memory,coverage,profile_view


@pytest.fixture
def pg_store(database):
    url,engine=database
    with engine.begin() as c:
        c.exec_driver_sql((Path(__file__).parents[2]/'deploy/profile-learning/schema.sql').read_text())
        c.exec_driver_sql((Path(__file__).parents[2]/'deploy/profile-learning/notifications-postgresql.sql').read_text())
    with Session(engine) as s,s.begin():
        s.add(Account(id='of-1'));s.flush()
        s.add(OfCreatorBinding(id='binding',account_id='of-1',tenant_id='tenant',creator_id='creator'));s.flush()
        s.add(OfRuntime(account_id='of-1',binding_id='binding',activated_at=datetime(2026,1,1)));s.flush()
        s.add_all(Fan(account_id='of-1',fan_id=i) for i in (1,2,3))
    return SimpleNamespace(engine=engine,_runtime=Store._runtime,url=url)


def run(store,fn=extractor):
    return asyncio.run(worker.step(store,'tenant',threading.Event(),extractor=fn))


def test_postgresql_only_one_worker_can_claim_a_job(pg_store):
    fan_due(pg_store)
    first=worker.claim_job(pg_store,'tenant',threading.Event())
    assert first['fan_id']==1
    assert worker.claim_job(pg_store,'tenant',threading.Event()) is None
    with pg_store.engine.begin() as c:
        c.execute(text("UPDATE of_profile_learning SET lease_until=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE fan_id=1"))
    resumed=worker.claim_job(pg_store,'tenant',threading.Event())
    assert resumed['fan_id']==1 and resumed['lease_token']!=first['lease_token']


def test_two_simultaneous_workers_do_not_call_the_same_fan(pg_store):
    add_message(pg_store);fan_due(pg_store)
    calls=[]
    async def slow(*a,**kw):
        calls.append(a[4]);await asyncio.sleep(.02)
        return await extractor(*a,**kw)
    async def work():
        return await asyncio.gather(*(worker.step(pg_store,'tenant',threading.Event(),extractor=slow) for _ in range(2)))
    results=asyncio.run(work())
    assert sorted(results)==[False,True] and calls==[1]


def test_source_change_rolls_back_all_receipts_and_memories(pg_store):
    add_message(pg_store);fan_due(pg_store)
    async def changed(*args,**kwargs):
        result=await extractor(*args,**kwargs)
        with Session(pg_store.engine) as s,s.begin(): s.get(Message,('of-1',1,1)).body='Edited text'
        return result
    run(pg_store,changed)
    with pg_store.engine.connect() as c:
        assert c.scalar(text('SELECT count(*) FROM of_profile_receipts'))==0
        assert c.scalar(text('SELECT count(*) FROM of_profile_memories'))==0
    assert state(pg_store)['error_code']=='profile-source-changed'


def test_long_message_resumes_each_chunk_and_never_receipts_a_partial_message(pg_store):
    body='a'*8500+' I enjoy tennis'
    add_message(pg_store,body=body);fan_due(pg_store)
    seen=[]
    async def read(*a,**kw):
        source=next(v for v in a[5].values() if not v.get('context_only'))
        seen.append(source['chunk'])
        if 'tennis' in source['text']:
            key=source['id'];return {'facts':[{'field':'interests','value':'tennis','quote':'I enjoy tennis','source':key}]},None
        return {'facts':[]},None
    run(pg_store,read)
    assert state(pg_store)['processed_messages']==0 and not state(pg_store)['document']['history_complete']
    fan_due(pg_store);run(pg_store,read);assert state(pg_store)['processed_messages']==0
    fan_due(pg_store);run(pg_store,read)
    assert seen==[0,1,2] and state(pg_store)['processed_messages']==1
    assert 'tennis' in state(pg_store)['document']['summary']


def test_edited_fact_free_source_is_reprocessed_and_withdrawn_evidence_disappears_on_read(pg_store):
    add_message(pg_store,body='hello');fan_due(pg_store)
    async def no_facts(*a,**kw): return {'facts':[]},None
    run(pg_store,no_facts)
    with Session(pg_store.engine) as s,s.begin(): s.get(Message,('of-1',1,1)).body='I enjoy tennis'
    # The trigger queues the changed source even when it previously had no observations.
    assert run(pg_store)
    if not state(pg_store)['document']['facts']: run(pg_store)
    with Session(pg_store.engine) as s,s.begin():
        assert load_memories(s,'of-1',1)
        s.get(Message,('of-1',1,1)).is_unsent=True
    with Session(pg_store.engine) as s,s.begin(): assert load_memories(s,'of-1',1)==[]


def test_full_roster_not_limited_to_first_batch(pg_store):
    with Session(pg_store.engine) as s,s.begin():
        s.add_all(Fan(account_id='of-1',fan_id=i) for i in range(4,1002))
    for _ in range(3): worker.seed(pg_store,'tenant')
    with pg_store.engine.connect() as c: assert c.scalar(text('SELECT count(*) FROM of_profile_learning WHERE fan_id>0'))==1001
    # Process a fan beyond the first batch, including a silent account record.
    fan_due(pg_store,1001);assert run(pg_store)
    with Session(pg_store.engine) as s,s.begin():
        view=profile_view(s,'tenant','of-1','binding',1001)
        assert view['state']=='no_personal_details' and view['processedMessages']==0
        assert coverage(s,'tenant','of-1','binding')['totalDiscoveredFans']==1001


def test_reads_and_reply_retrieval_share_current_dates_and_account_scope(pg_store,monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED','1')
    add_message(pg_store,body='My brother wedding tomorrow');fan_due(pg_store)
    async def event(*a,**kw):
        return {'facts':[{'field':'event','value':'brother wedding','quote':'My brother wedding tomorrow',
            'source':'1:1','key':'brother wedding','date_text':'tomorrow'}]},None
    run(pg_store,event)
    with Session(pg_store.engine) as s,s.begin():
        assert reply_memory(s,'of-1',1,now='2026-10-29')['memories']==[]
        assert reply_memory(s,'of-1',1,query='brother wedding',now='2026-10-29')['memories'][0]['status']=='expired'
        assert load_memories(s,'of-2',1)==[]
        assert 'wedding' not in profile_view(s,'tenant','of-1','binding',1,now='2026-10-29')['summary']


def test_budget_failure_keeps_chunk_pending_without_resetting_shared_allowance(pg_store):
    from native_pilot.budget import BudgetRefused
    add_message(pg_store);fan_due(pg_store)
    async def exhausted(*a,**kw): raise BudgetRefused('budget-cap-unset')
    run(pg_store,exhausted)
    assert state(pg_store)['processed_messages']==0 and state(pg_store)['error_code']=='profile-budget-exhausted'
    with pg_store.engine.connect() as c: assert c.scalar(text('SELECT count(*) FROM of_profile_receipts'))==0


def test_first_daily_reservations_are_serialized_and_cap_is_not_exceeded(pg_store,monkeypatch):
    import importlib
    db_engine=importlib.import_module("db.engine")
    from native_pilot.budget import DailyBudget
    from native_pilot.scope import AccountScope
    from llm_client import LLMCapExceeded
    with Session(pg_store.engine) as s,s.begin():
        s.add(AccountAiConfig(account_id='of-1',daily_cost_cap_cents=1))
    async def work():
        ae=create_async_engine(pg_store.engine.url.set(drivername='postgresql+asyncpg'))
        factory=async_sessionmaker(ae,expire_on_commit=False)
        @asynccontextmanager
        async def sessions():
            async with factory() as s:
                try:
                    yield s;await s.commit()
                except BaseException:
                    await s.rollback();raise
        monkeypatch.setattr(db_engine,'get_session',sessions)
        b=DailyBudget(AccountScope.parse('of-1'),'grok-4-1-fast-non-reasoning',endpoint='https://api.x.ai/v1/chat/completions')
        calls=await asyncio.gather(*(b.reserve(fan_id=1,request_bytes=5000,max_output_tokens=5000) for _ in range(8)),return_exceptions=True)
        successes=[r for r in calls if not isinstance(r,BaseException)]
        assert successes and any(isinstance(r,LLMCapExceeded) for r in calls)
        with Session(pg_store.engine) as s:
            rows=s.scalars(select(GrokDailyCost).where(GrokDailyCost.account_id=='of-1')).all()
            assert len(rows)==1 and rows[0].cost_cents<=100
            assert rows[0].call_count==len(successes)
            assert rows[0].cost_cents==sum(r.reserved_units for r in successes)
        # Unknown usage retains the reservation, settlement is atomic and account-scoped.
        await b.hold(successes[0])
        with Session(pg_store.engine) as s: assert s.get(GrokCall,successes[0].call_id).status=='unknown'
        await ae.dispose()
    asyncio.run(work())


def test_structured_manual_correction_is_authoritative_idempotent_and_scoped(pg_store):
    from onlyfans_runtime.profile_learning.manual import correct
    from onlyfans_runtime.store import Refused
    add_message(pg_store);fan_due(pg_store);run(pg_store)
    key=state(pg_store)['document']['facts'][0]['memory_key']
    args=dict(expected_revision=0,command_key='operator-1',actor='op-session:'+'a'*64,reason='User supplied correction',
              operation='set',value={'value':'I enjoy surfing'})
    assert correct(pg_store,'tenant','of-1',1,key,**args)=={'revision':1,'replayed':False}
    assert correct(pg_store,'tenant','of-1',1,key,**args)=={'revision':1,'replayed':True}
    with pytest.raises(Refused): correct(pg_store,'other-tenant','of-1',1,key,**args)
    with Session(pg_store.engine) as s,s.begin():
        assert load_memories(s,'of-1',1)[0]['value']=='I enjoy surfing'
        s.get(Message,('of-1',1,1)).is_unsent=True
    with Session(pg_store.engine) as s,s.begin():
        f=load_memories(s,'of-1',1)[0]
        assert f['manual'] and f['protected'] and f['value']=='I enjoy surfing'
    with pytest.raises(Refused): correct(pg_store,'tenant','of-1',1,key,**{**args,'command_key':'stale-command'})


def test_nonmanaging_agency_cannot_change_structured_memory(pg_store):
    from onlyfans_runtime.profile_learning.manual import correct
    from onlyfans_read.agency_scope import current_scope,AgencyScope
    from onlyfans_runtime.store import Refused
    token=current_scope.set(AgencyScope(('creator',),'agency-user:'+'a'*8+'-'+ 'a'*4+'-'+ 'a'*4+'-'+ 'a'*4+'-'+ 'a'*12+':'+'b'*8+'-'+ 'b'*4+'-'+ 'b'*4+'-'+ 'b'*4+'-'+ 'b'*12,False))
    try:
        with pytest.raises(Refused):
            correct(pg_store,'tenant','of-1',1,'a'*64,expected_revision=0,command_key='x',actor=current_scope.get().actor,
                    reason='Attempt',operation='clear')
    finally: current_scope.reset(token)


def test_edit_of_context_question_invalidates_answer_interpretation(pg_store):
    add_message(pg_store,mid=1,body='How old are you?')
    with Session(pg_store.engine) as s,s.begin():s.get(Message,('of-1',1,1)).direction='out'
    add_message(pg_store,mid=2,body='26',created_at=datetime(2026,1,1,0,0,1))
    fan_due(pg_store)
    async def age(*a,**kw):
        return {'facts':[{'field':'age','value':'26','source':'1:2','quote':'26',
            'context_sources':['context:1:1'],'context_quotes':['How old are you?']}]},None
    run(pg_store,age)
    with Session(pg_store.engine) as s,s.begin():
        assert load_memories(s,'of-1',1)
        s.get(Message,('of-1',1,1)).body='What is your shoe size?'
    with Session(pg_store.engine) as s,s.begin():assert load_memories(s,'of-1',1)==[]


def test_actual_native_prompt_and_fastt_hook_use_dated_memory(pg_store,monkeypatch):
    monkeypatch.setenv('ONLYFANS_PROFILE_LEARNING_ENABLED','1')
    from onlyfans_runtime.native_context import versions
    from onlyfans_runtime.native_planning import _native_messages
    from native_pilot.twin_context import _build
    add_message(pg_store);fan_due(pg_store);run(pg_store)
    with Session(pg_store.engine) as s,s.begin():
        s.add(AccountAiConfig(account_id='of-1',persona='Mia likes surfing and chats warmly.',language='en'))
        p=s.get(FanProfile,('of-1',1));p.short_bio='Going to a wedding next week OLD_UNDATED'
    with Session(pg_store.engine) as s,s.begin():
        _,snapshot=versions(s,'of-1',1)
        snapshot['messages']=[{'direction':'in','body':'hello','message_id':999,'created_at':datetime.utcnow().isoformat()}]
        messages=_native_messages(snapshot)
        rendered=json.dumps(messages)
        assert 'I enjoy tennis' in rendered and 'OLD_UNDATED' not in rendered
        hook=_build(s,'of-1',1,'hello')
        assert hook.dated_memory and hook.finish is not None
        finished=hook.finish([{'role':'system','content':'Safe system'},{'role':'user','content':'hello'}])
        assert 'I enjoy tennis' in json.dumps(finished) and finished[-1]['content']=='hello'


def test_stored_history_model_uses_own_credential_even_with_expired_of_session(pg_store,monkeypatch):
    import importlib,httpx
    from db.onlyfans_connection_models import OfProviderConnection
    from onlyfans_runtime.connections import _seal
    from onlyfans_runtime.profile_learning.model import extract
    from onlyfans_runtime.profile_learning.evidence import digest
    monkeypatch.setenv('ONLYFANS_PROCESS_ROLE','worker')
    monkeypatch.setenv('ONLYFANS_CONNECTION_KEY','42'*32)
    expiry=datetime(2025,1,1)
    clear={'ownerId':1,'expiresAt':expiry.isoformat()+'Z','model':{'modelId':'grok-4-1-fast-non-reasoning','apiKey':'synthetic-key-for-this-account'}}
    with Session(pg_store.engine) as s,s.begin():
        s.add(AccountAiConfig(account_id='of-1',model='grok-4-1-fast-non-reasoning',daily_cost_cap_cents=100))
        s.add(OfProviderConnection(account_id='of-1',binding_id='binding',owner_id=1,revision=1,
            sealed_config=_seal(clear,'tenant','of-1','binding',1),expires_at=expiry,status='expired',updated_at=expiry))
    requests=[]
    original_client=httpx.AsyncClient
    async def handler(request):
        requests.append(request)
        assert request.headers['authorization']=='Bearer synthetic-key-for-this-account'
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"facts":[]}'}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original_client(transport=httpx.MockTransport(handler),**kw))
    async def work():
        ae=create_async_engine(pg_store.engine.url.set(drivername='postgresql+asyncpg'))
        factory=async_sessionmaker(ae,expire_on_commit=False)
        @asynccontextmanager
        async def sessions():
            async with factory() as s:
                try: yield s;await s.commit()
                except BaseException: await s.rollback();raise
        monkeypatch.setattr(importlib.import_module('db.engine'),'get_session',sessions)
        sources={'1:1':{'id':'1:1','fan':1,'text':'hello','at':'2026-01-01','direction':'in','digest':digest('hello')}}
        result,call=await extract(pg_store,'tenant','of-1','binding',1,sources)
        assert result=={'facts':[]} and call is not None
        await ae.dispose()
    asyncio.run(work())
    assert len(requests)==1 and str(requests[0].url)=='https://api.x.ai/v1/chat/completions'
    with Session(pg_store.engine) as s:
        assert s.get(OfRuntime,'of-1').enabled is False
        assert s.scalar(select(GrokCall)).status=='done'
        assert s.get(OfProviderConnection,'of-1').status=='expired'
